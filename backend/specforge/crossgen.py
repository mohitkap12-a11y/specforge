"""Cross-command scenario generation, driven by the specification text itself.

The user selects commands and/or sections.  Each selected section is one *unit* and one model call: the model is shown
the section (with its sub-sections, tables and normative statements) plus every passage elsewhere in the spec set that
is connected to it, and drafts scenarios that need SEVERAL commands to reach the behaviour the section describes.

Connected passages are found structurally, never by protocol knowledge:
  * references written in the section ("see section 5.2", "Figure 103", "defined in <other spec>") that resolved to a target;
  * references elsewhere in the set that point INTO the section;
  * commands the section's text names (catalog command names), whose own sections are pulled in;
  * normative statements elsewhere that name a command defined in the section.
Opcodes, bit ranges and values come only from the catalog, so nothing here is specific to one specification family.

Every draft is schema-checked, grounded (quotes must occur verbatim in the text the model was shown), validated against
the catalog slice it was shown, required to involve at least two distinct commands, de-duplicated, and stored as a draft
for human approval.  Drafts never count toward coverage.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import eventlog
from .catalog import _mention_patterns
from .core.db import jloads, rows
from .ingest.normalize import norm_text, split_sentences
from .llm.base import LLMError
from .llm.service import LLMService
from .refs import load_set_irs, resolve_references
from .scenarios import catalog_context, validate_predicate

PROMPT_VERSION = "cross-v2"
PRIMARY_CHARS = 10_000       # budget for the selected section (its sub-sections and tables)
RELATED_CHARS = 8_000        # budget for all connected passages together
PASSAGE_CHARS = 2_000        # budget for one connected passage
CATALOG_CHARS = 16_000       # shared out between the commands in scope, so every one of them is shown
MIN_PER_COMMAND = 600
MAX_COMMANDS = 24

# ----------------------------------------------------------------------------- model output

class AIField(BaseModel):
    dw: int | Literal["nmd0", "nmd1"]
    hi: int
    lo: int
    op: Literal["eq", "ne", "in", "range", "mask"] = "eq"
    value: Any
    vary: bool = Field(default=False, description="repeat the scenario for the catalog's values of this field (the tool expands it)")


class AICapture(BaseModel):
    dw: int | Literal["nmd0", "nmd1"]
    hi: int
    lo: int


class AIStep(BaseModel):
    command: str = Field(description="catalog command key, e.g. cmd:admin:06")
    note: str = Field(default="", description="what this step is for in the test flow (setup / stimulus / follow-up check)")
    fields: list[AIField] = []
    capture: dict[str, AICapture] = {}


class AIScenario(BaseModel):
    title: str
    description: str = ""
    observability: Literal["submission", "stimulus"]
    steps: list[AIStep]
    order: Literal["strict", "any"] = "strict"
    scope: Literal["global", "test", "script"] = "test"
    within: int | None = None
    evidence: list[str] = Field(default=[], description="ids of the numbered sentences that justify the scenario, e.g. ['S12', 'S40']")
    quotes: list[str] = Field(default=[], description="alternative to evidence: verbatim excerpts that justify the scenario")


class AIOutput(BaseModel):
    testable: bool
    reason: str = ""
    scenarios: list[AIScenario] = []


# Each pass over a section looks for a different kind of scenario.  Spec text rarely states a multi-command flow outright, so a
# single "what does the text say" pass yields one or two; the later passes walk the field descriptions, the preconditions and
# the value space, which is where a test developer finds most scenarios.
ANGLES = [
    {"id": "explicit", "title": "Flows the text states",
     "focus": "Multi-command flows, orderings, preconditions and dependencies that the SECTION or RELATED text states."},
    {"id": "fields", "title": "Field behaviours across commands",
     "focus": "Walk the fields in the SECTION's tables one by one. For each field whose description says what the device does with data, "
              "state, limits or ordering, write a scenario: the section's command submitted with that field set, then a partner command from "
              "CATALOG that observes or depends on the effect (reads back, verifies, compares, queries, releases, undoes). One scenario per field "
              "that gives you something to check; use different partner commands where several fit."},
    {"id": "negative", "title": "Misuse, preconditions and limits",
     "focus": "What happens when a precondition is skipped or an ordering rule is broken (the dependent command before its setup, the release "
              "before the dependent command, a limit exceeded, a reserved value used) and the error cases the text describes, each using two or "
              "more commands."},
    {"id": "variants", "title": "Value and boundary variants",
     "focus": "The flows above repeated across the values of the fields the text defines: flags set and cleared, each enumerated value, size "
              "minimum / maximum. Mark such a field condition with \"vary\": true instead of listing the variants."},
]
DEFAULT_PASSES = 3
MAX_VARIANTS = 8                # values tried per varied field
MAX_PER_SECTION = 60            # drafts kept per section across all passes and variants

SYSTEM = """You write functional test scenarios for a device specification, the way a test-script developer does.
A scenario is an ordered sequence of SEVERAL commands; it is checked against a log of SUBMITTED commands only
(no completions or responses are logged).

You are given SECTION (the part of the specification the scenarios are about) and RELATED passages from elsewhere in the
specification, both as numbered sentences like [S12]; CATALOG (the only commands, bit ranges and values you may use);
and an ANGLE: the kind of scenario to write in this pass.

Rules:
- Every scenario MUST use at least two different commands from CATALOG. A scenario about one command alone is useless here.
  Typical shapes: set something up, exercise the behaviour the SECTION describes, then observe it with another command;
  create a resource before using it and release it afterwards; write, then read back or verify.
- Mine the text. The SECTION's tables define each field and what it does. A field description that says what the device does
  with data, state, limits or ordering is enough for a scenario even when the text names no second command: pair the command
  with a partner from CATALOG that can observe or depend on the effect. Do not invent behaviour the text does not support.
- Write as many DISTINCT scenarios as the text supports for the ANGLE, up to the stated limit. A different field, precondition,
  partner command or ordering is a different scenario; the same flow with cosmetic changes is not. Do not stop at one when more
  fit. Reply testable=false only when the text gives nothing at all for this ANGLE.
- Never invent opcodes, commands or bit positions. Use only CATALOG. Field values are decimal integers; operators are
  'eq','ne','in' (list),'range' ({"lo","hi"}) or 'mask' ({"mask","value"}).
- When a scenario should be repeated for the values of one field (a flag, an enumerated value, a size boundary), put
  "vary": true on that field condition; the tool expands it from the catalog. Do not write the variants yourself.
- Link steps through values: in an earlier step add "capture": {"name": {"dw":..,"hi":..,"lo":..}} and in a later step's
  field use "value": {"var": "name"} with op 'eq' or 'ne' (for example the identifier chosen in one command is the
  identifier used by the next). A captured value is a single integer.
- Give every step a short 'note' naming its role (setup, stimulus, follow-up check).
- order 'strict' when the text requires an order, otherwise 'any'. scope 'test' keeps all steps in one test.
- observability 'submission' = the whole flow is visible in what was submitted; 'stimulus' = only the commands that drive the
  behaviour are visible, the device's response cannot be verified.
- Cite the sentences that justify each scenario in "evidence": ["S12", "S40"], copying the ids exactly. Do not quote text.
- Reply with one JSON object only."""


# ----------------------------------------------------------------------------- helpers

def _catalog_for(conn: sqlite3.Connection, set_id: int, cmd_key: str) -> list[dict]:
    out: list[dict] = []
    for r in rows(conn, "SELECT key,kind,label,attrs_json FROM catalog_item WHERE set_id=? AND (key=? OR parent_key=? OR "
                        "parent_key IN (SELECT key FROM catalog_item WHERE set_id=? AND parent_key=?))",
                  (set_id, cmd_key, cmd_key, set_id, cmd_key)):
        x = jloads(r["attrs_json"], {})
        if r["kind"] == "command":
            out.append({"command": r["key"], "name": x["name"], "interface": x["interface"], "queue": x["queue"]})
        elif r["kind"] in ("field", "reserved"):
            out.append({"command": cmd_key, "field": r["label"], "dw": x["dw"], "hi": x["hi"], "lo": x["lo"],
                        "reserved": r["kind"] == "reserved"})
        elif r["kind"] == "value" and not x.get("reserved_value"):
            out.append({"command": cmd_key, "value_of": x["field"], "value": x["vlo"] if x["vlo"] == x["vhi"] else [x["vlo"], x["vhi"]],
                        "meaning": x.get("desc", "")[:60]})
    return out


def _grounded(quote: str, corpus: str) -> bool:
    q = norm_text(quote)     # case-sensitive: "the controller shall" must not match "The Controller SHALL"
    return len(q) >= 8 and q in norm_text(corpus)


def to_predicate(sc: AIScenario, cmds: dict[str, dict]) -> dict:
    steps = []
    for st in sc.steps:
        c = cmds.get(st.command)
        if c is None:
            raise ValueError(f"unknown command {st.command}")
        steps.append({"command": st.command, "interface": c["interface"], "queue": c["queue"],
                      "cs": c["cs"] if c["queue"] == "io" else None, "opcode": c["opcode"],
                      "fields": [f.model_dump(exclude={"vary"}) for f in st.fields],
                      **({"note": st.note} if st.note else {}),
                      **({"capture": {k: v.model_dump() for k, v in st.capture.items()}} if st.capture else {})})
    pred: dict[str, Any] = {"scope": sc.scope, "order": sc.order, "steps": steps}
    if sc.within is not None:   # 0 must reach validation (it is invalid), not silently mean 'unlimited'
        pred["within"] = sc.within
    return pred


def _record(conn, set_id, key, svc, state, output, error, meta=None, task="cross"):
    cfg = svc.task_cfg("generate")          # the row is labelled "cross"; provider / model are those configured for generation
    conn.execute("INSERT INTO enrichment(set_id,item_key,task,provider,model,prompt_version,state,output_json,error) VALUES(?,?,?,?,?,?,?,?,?)",
                 (set_id, key, task, (meta or {}).get("provider", cfg["provider"]), cfg["model"], PROMPT_VERSION, state,
                  json.dumps(output) if output is not None else None, error))
    conn.commit()


# ----------------------------------------------------------------------------- reading the spec set

class _Index:
    """Everything needed to look up sections, tables and requirements across the set by node key."""

    def __init__(self, irs: list[dict]):
        self.irs = irs
        self.sec: dict[str, tuple[dict, dict]] = {}        # section key -> (ir, section)
        self.tab: dict[str, tuple[dict, dict]] = {}        # table key -> (ir, table)
        self.req: dict[str, tuple[dict, dict]] = {}        # requirement key -> (ir, requirement)
        for ir in irs:
            for s in ir["sections"].values():
                self.sec[s["key"]] = (ir, s)
            for t in ir["tables"].values():
                self.tab[t["key"]] = (ir, t)
            for r in ir["requirements"]:
                self.req[r["key"]] = (ir, r)

    def subtree(self, key: str) -> list[dict]:
        ir, root = self.sec[key]
        keep = {root["number"]}
        out = [root]
        for n in ir["section_order"]:                       # document order: parents precede children
            s = ir["sections"][n]
            if s["parent"] in keep and n not in keep:
                keep.add(n)
                out.append(s)
        return out

    def section_of(self, node_key: str) -> str | None:
        """Section key that contains any node key (section, requirement, table)."""
        if node_key in self.sec:
            return node_key
        if node_key in self.req:
            ir, r = self.req[node_key]
            if r.get("section") and r["section"] in ir["sections"]:
                return ir["sections"][r["section"]]["key"]
            if r.get("table") and f"{ir['prefix']}/fig:{r['table']}" in self.tab:
                return self.section_of(f"{ir['prefix']}/fig:{r['table']}")
            return None
        if node_key in self.tab:
            ir, t = self.tab[node_key]
            sec = t.get("section")
            return ir["sections"][sec]["key"] if sec and sec in ir["sections"] else None
        return None

    def tables_in(self, sec_keys: set[str]) -> list[tuple[dict, dict]]:
        return [(ir, t) for ir, t in self.tab.values() if self.section_of(t["key"]) in sec_keys]


def _table_text(t: dict, max_rows: int = 60) -> str:
    head = " | ".join(str(c) for c in (t.get("header") or []))
    body = "\n".join(" | ".join(str(c) for c in r) for r in (t.get("rows") or [])[:max_rows])
    more = len(t.get("rows") or []) - max_rows
    return f"[{t['label']}: {t['caption']}]\n{head}\n{body}" + (f"\n... {more} more row(s)" if more > 0 else "")


def _section_block(ix: _Index, key: str, limit: int, with_tables: bool = True) -> str:
    keys = {s["key"] for s in ix.subtree(key)}
    parts = []
    for k in [s["key"] for s in ix.subtree(key)]:
        _, s = ix.sec[k]
        if s["text"].strip():
            parts.append(f"§{s['number']} {s['title']}\n{s['text'].strip()}")
        else:
            parts.append(f"§{s['number']} {s['title']}")
    text = "\n\n".join(parts)
    if with_tables:
        tabs = [_table_text(t) for _, t in ix.tables_in(keys)]
        if tabs:
            text += "\n\n" + "\n\n".join(tabs)
    return text[:limit]


# ----------------------------------------------------------------------------- planning

def list_sections(conn: sqlite3.Connection, set_id: int, q: str | None = None, limit: int = 400, family: str | None = None,
                  offset: int = 0, version: str | None = None) -> dict[str, Any]:
    """Sections of the set's documents for the picker, optionally of one spec (family, and version when a set holds several revisions) and paged; `commands` are the catalog commands
    defined inside each section and `specs` lists every document of the set (so a picker can offer a per-spec filter)."""
    ix = _Index(load_set_irs(conn, set_id))
    cmd_sec = {}
    for r in rows(conn, "SELECT key,label,attrs_json FROM catalog_item WHERE set_id=? AND kind='command'", (set_id,)):
        sec = jloads(r["attrs_json"], {}).get("section")
        if sec:
            cmd_sec.setdefault(sec, []).append({"key": r["key"], "name": r["label"]})
    out = []
    needle = (q or "").lower()
    for ir in ix.irs:
        if (family and ir["family"] != family) or (version and ir["version"] != version):
            continue
        for n in ir["section_order"]:
            s = ir["sections"][n]
            if needle and needle not in f"{s['number']} {s['title']}".lower():
                continue
            out.append({"key": s["key"], "family": ir["family"], "version": ir["version"], "number": s["number"], "title": s["title"],
                        "level": s["level"], "chars": len(s["text"]), "commands": cmd_sec.get(s["key"], [])})
    specs = [{"family": ir["family"], "version": ir["version"], "sections": len(ir["sections"])} for ir in ix.irs]
    return {"total": len(out), "sections": out[offset:offset + limit], "specs": specs}


def build_units(conn: sqlite3.Connection, set_id: int, section_keys: list[str] | None = None, command_keys: list[str] | None = None) -> list[dict]:
    """One unit per selected section (a selected command contributes its own section).  Raises ValueError on a bad selection."""
    ix = _Index(load_set_irs(conn, set_id))
    cat = {r["key"]: {**jloads(r["attrs_json"], {}), "key": r["key"]} for r in rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=? AND kind='command'", (set_id,))}
    keys: list[str] = []
    for k in section_keys or []:
        if k not in ix.sec:
            raise ValueError(f"unknown section '{k}'")
        keys.append(k)
    for ck in command_keys or []:
        c = cat.get(ck)
        if c is None:
            raise ValueError(f"unknown command '{ck}'")
        if not c.get("section") or c["section"] not in ix.sec:
            raise ValueError(f"command '{c.get('name', ck)}' has no section in the parsed specs; select its section directly")
        keys.append(c["section"])
    keys = list(dict.fromkeys(keys))
    if not keys:
        raise ValueError("select at least one command or section")

    # commands by the section that defines them, mention patterns, and the catalog's requirement mentions
    cmd_by_sec: dict[str, list[str]] = {}
    for k, c in cat.items():
        if c.get("section"):
            cmd_by_sec.setdefault(c["section"], []).append(k)
    patterns = _mention_patterns({k: {"name": c["name"]} for k, c in cat.items()})
    req_items = rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=? AND kind='requirement'", (set_id,))
    req_mentions = {r["key"]: jloads(r["attrs_json"], {}).get("mentions", []) for r in req_items}
    refs = [r for r in resolve_references(conn, set_id, ix.irs) if r["state"] == "resolved" and r["target_key"]]

    # the commands the user selected, plus those defined in the sections they selected: one group, shown whole to the model
    selected_all = list(dict.fromkeys([k for k in (command_keys or []) if k in cat] +
                                      [c for key in keys for sk in (s_["key"] for s_ in ix.subtree(key)) for c in cmd_by_sec.get(sk, [])]))
    if len(selected_all) > MAX_COMMANDS:
        raise ValueError(f"{len(selected_all)} commands are selected (including those in the selected sections); at most {MAX_COMMANDS} "
                         "can be tested together. Select fewer, or generate in smaller related groups.")

    units = []
    for key in keys:
        ir, root = ix.sec[key]
        subs = ix.subtree(key)
        sub_keys = {s["key"] for s in subs}
        own_nodes = set(sub_keys) | {t["key"] for _, t in ix.tables_in(sub_keys)} | {k for k in ix.req if ix.section_of(k) in sub_keys}
        unit_cmds = [c for sk in sub_keys for c in cmd_by_sec.get(sk, [])]
        primary = _section_block(ix, key, PRIMARY_CHARS)
        cands: list[dict] = []          # connected passages, best first

        def add(node: str, why: str, text: str | None = None) -> None:
            sec = ix.section_of(node) or node
            if sec in sub_keys or node in own_nodes:
                return
            if text is None:
                if node in ix.tab:
                    text, title = _table_text(ix.tab[node][1]), ix.tab[node][1]["caption"]
                elif sec in ix.sec:
                    text, title = _section_block(ix, sec, PASSAGE_CHARS, with_tables=False), ix.sec[sec][1]["title"]
                else:
                    return
            else:
                title = ix.sec[sec][1]["title"] if sec in ix.sec else node
            if text.strip():
                cands.append({"key": node, "section": sec if sec in ix.sec else None, "title": title, "why": why, "text": text[:PASSAGE_CHARS]})

        # 1. what the section points at
        for r in refs:
            if r["src_key"] in own_nodes and r["target_key"] not in own_nodes:
                add(r["target_key"], f"referenced from the section ('{r['raw']}')")
        # 2. commands the section names
        text_all = primary
        named = [k for k, rx in patterns.items() if rx.search(text_all) and k not in unit_cmds]
        for k in named:
            c = cat[k]
            if c.get("section") in ix.sec:
                add(c["section"], f"defines the command '{c['name']}' named in the section")
        # 3. what points into the section
        for r in refs:
            if r["target_key"] in own_nodes and r["src_key"] not in own_nodes:
                add(r["src_key"] if r["src_key"] in ix.sec else (ix.section_of(r["src_key"]) or r["src_key"]), f"refers to the section ('{r['raw']}')")
        # 4. statements elsewhere that name a command defined in the section
        for rk, ms in req_mentions.items():
            if rk in ix.req and rk not in own_nodes and any(m in unit_cmds for m in ms):
                _, rq = ix.req[rk]
                sec = ix.section_of(rk)
                add(rk, "normative statement elsewhere naming a command of the section", rq["text"])
                if sec in ix.sec:
                    cands[-1]["title"] = ix.sec[sec][1]["title"]

        # 5. the other sections selected in the same request: weakest signal, so last and short (their commands still join the scope)
        for other in keys:
            if other != key:
                add(other, "selected together with this section", _section_block(ix, other, 700, with_tables=False))

        related, used, seen = [], 0, set()
        for c in cands:
            if c["key"] in seen or used + len(c["text"]) > RELATED_CHARS:
                continue
            seen.add(c["key"])
            used += len(c["text"])
            related.append(c)

        in_related = [k for k, rx in patterns.items() if k not in unit_cmds and any(rx.search(r_["text"]) for r_ in related)]
        selected_cmds = selected_all          # all of them rank right after this section's own commands (never truncated: see the check above)
        involved = list(dict.fromkeys(unit_cmds + selected_cmds + named + in_related +
                                      [m for rk, ms in req_mentions.items() if rk in own_nodes for m in ms] +
                                      [c for r_ in related if r_["why"] != "selected together with this section" for c in cmd_by_sec.get(r_["section"] or "", [])]))
        involved = [c for c in involved if c in cat][:MAX_COMMANDS]
        units.append({"section_key": key, "family": ir["family"], "version": ir["version"], "number": root["number"], "title": root["title"],
                      "commands": involved, "own_commands": unit_cmds, "primary": primary, "related": related})
    return units


def plan(conn: sqlite3.Connection, set_id: int, section_keys: list[str] | None = None, command_keys: list[str] | None = None) -> list[dict]:
    """What the model would read for each unit, without calling it (texts are summarised to sizes)."""
    return [{"section_key": u["section_key"], "title": f"§{u['number']} {u['title']}", "family": u["family"], "version": u["version"],
             "primary_chars": len(u["primary"]), "own_commands": u["own_commands"], "commands": u["commands"],
             "related": [{"key": r["key"], "title": r["title"], "why": r["why"], "chars": len(r["text"])} for r in u["related"]]}
            for u in build_units(conn, set_id, section_keys, command_keys)]


# ----------------------------------------------------------------------------- generation

_KIND_ORDER = {"name": 0, "field": 1, "value_of": 2}


def _catalog_text(conn: sqlite3.Connection, set_id: int, commands: list[str]) -> str:
    """The catalog slice, one compact JSON object per line.  Each command gets an equal share of the budget (its command line and
    fields first, enumerated values last), so a long command can never push another one out of the prompt."""
    per = max(MIN_PER_COMMAND, CATALOG_CHARS // max(1, len(commands)))
    out: list[str] = []
    for ck in commands:
        entries = sorted(_catalog_for(conn, set_id, ck), key=lambda e: next((v for k, v in _KIND_ORDER.items() if k in e), 3))
        used = 0
        for e in entries:
            line = json.dumps(e, separators=(",", ":"))
            if used + len(line) > per:
                out.append(json.dumps({"command": ck, "note": "further fields / values omitted for space"}, separators=(",", ":")))
                break
            out.append(line)
            used += len(line) + 1
    return "\n".join(out)


def _numbered(text: str, n: int, cap: int = 500) -> tuple[str, dict[str, str], int]:
    """Text with each sentence on its own line as '[S12] ...'; returns (rendered, id -> sentence, last number used).  The model cites
    ids, so its evidence is exact by construction instead of depending on it copying text character for character."""
    lines, ev = [], {}
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            lines.append("")
            continue
        for part in split_sentences(ln) or [ln]:
            n += 1
            ev[f"S{n}"] = part[:cap]
            lines.append(f"[S{n}] {part[:cap]}")
    return "\n".join(lines), ev, n


def _messages(conn: sqlite3.Connection, set_id: int, u: dict, focus: str, limit: int, group: list[str] | None = None,
              angle: dict | None = None) -> tuple[list[dict], str, dict[str, str]]:
    section, ev, n = _numbered(u["primary"], 0)
    parts = []
    for r in u["related"]:
        text, e, n = _numbered(r["text"], n)
        ev.update(e)
        parts.append(f"[{r['title']} - {r['why']}]\n{text}")
    related = "\n\n".join(parts) or "(none found)"
    user = (f"SECTION §{u['number']} {u['title']}:\n{section}\n\nRELATED:\n{related}\n\n"
            f"CATALOG (the only commands/fields you may use; one JSON object per line):\n{_catalog_text(conn, set_id, u['commands'])}\n\n"
            + (f"SELECTED TOGETHER: the user chose these commands to be tested as a group, so scenarios should combine them where the text supports it: "
               f"{', '.join(group)}.\n\n" if group and len(group) > 1 else "")
            + (f"FOCUS requested by the user: {focus}\n\n" if focus.strip() else "")
            + (f"ANGLE for this pass - {angle['title']}: {angle['focus']}\n\n" if angle else "")
            + f"Write up to {limit} scenarios; reach that number when the text supports it.")
    corpus = u["primary"] + "\n" + "\n".join(r["text"] for r in u["related"])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], corpus, ev


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _evidence(sc: AIScenario, ev: dict[str, str], corpus: str) -> tuple[list[str], list[str]]:
    """(evidence sentences, problems): cited ids must exist; quoted text must occur verbatim in what the model was shown."""
    out: list[str] = []
    errs: list[str] = []
    for i in sc.evidence:
        t = ev.get(i.strip().strip("[]").upper())
        if t is None:
            errs.append(f"unknown evidence id {i!r}")
        else:
            out.append(t)
    for q in sc.quotes:
        if _grounded(q, corpus):
            out.append(q)
        else:
            errs.append("ungrounded quote")
    if not out and not errs:
        errs.append("no evidence cited")
    return out, errs


def _field_values(conn: sqlite3.Connection, set_id: int, cmd_key: str, f: dict) -> list[int]:
    """Values the catalog lists for a field (enumerated values, then its min / max; 0 and 1 for a single bit)."""
    vals: set[int] = set()
    for r in rows(conn, "SELECT attrs_json FROM catalog_item WHERE set_id=? AND kind IN ('value','boundary') AND key LIKE ? ESCAPE '\\'",
                  (set_id, _like(cmd_key) + "/%")):
        a = jloads(r["attrs_json"], {})
        if a.get("command") == cmd_key and (a.get("dw"), a.get("hi"), a.get("lo")) == (f["dw"], f["hi"], f["lo"]) \
                and not a.get("reserved_value") and a.get("vlo") == a.get("vhi"):
            vals.add(a["vlo"])
    if not vals and f["hi"] == f["lo"]:
        vals = {0, 1}
    return sorted(vals)[:MAX_VARIANTS]


def _variants(conn: sqlite3.Connection, set_id: int, sc: AIScenario, pred: dict, scoped_fields: dict[str, list[dict]]) -> list[tuple[str, dict]]:
    """One variant per catalog value of every field the model marked `vary` (one field at a time, not the cross product)."""
    out: list[tuple[str, dict]] = []
    for si, st in enumerate(sc.steps):
        for fi, f in enumerate(st.fields):
            cur = pred["steps"][si]["fields"][fi]
            if not f.vary or f.op != "eq" or isinstance(f.value, dict):
                continue
            label = next((a.get("abbr") or a.get("name") for a in scoped_fields.get(st.command, [])
                          if (a["dw"], a["hi"], a["lo"]) == (cur["dw"], cur["hi"], cur["lo"])), f"dw{cur['dw']}[{cur['hi']}:{cur['lo']}]")
            for v in _field_values(conn, set_id, st.command, cur):
                if v == cur["value"]:
                    continue
                p2 = json.loads(json.dumps(pred))
                p2["steps"][si]["fields"][fi]["value"] = v
                out.append((f"{label}={v}", p2))
    return out


def generate_cross_scenarios(conn: sqlite3.Connection, set_id: int, svc: LLMService, section_keys: list[str] | None = None,
                             command_keys: list[str] | None = None, focus: str = "", max_per_section: int = 6, progress=None,
                             passes: int = DEFAULT_PASSES) -> dict[str, int]:
    """One model call per section per pass (`passes` of ANGLES, in order); `max_per_section` is the limit asked of each call."""
    if not isinstance(max_per_section, int) or isinstance(max_per_section, bool) or not 1 <= max_per_section <= 20:
        raise ValueError("max_per_section must be an integer from 1 to 20")
    if not isinstance(passes, int) or isinstance(passes, bool) or not 1 <= passes <= len(ANGLES):
        raise ValueError(f"passes must be an integer from 1 to {len(ANGLES)}")
    units = build_units(conn, set_id, section_keys, command_keys)
    cmds, fields = catalog_context(conn, set_id)
    angles = ANGLES[:passes]
    group = [cmds[k]["name"] for k in dict.fromkeys([*(command_keys or []), *(c for u in units for c in u["own_commands"])]) if k in cmds]
    req_text = {r["key"]: norm_text(jloads(r["attrs_json"], {}).get("text", "")) for r in rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=? AND kind='requirement'", (set_id,))}
    stats = {"sections": len(units), "created": 0, "variants": 0, "not_testable": 0, "rejected": 0, "failed": 0, "duplicates": 0}
    cfg = svc.task_cfg("generate")
    eventlog.emit("info", "agent", f"Cross-command agent starting: {len(units)} section(s) x {len(angles)} pass(es), model {cfg['provider']}/{cfg['model'] or '(unset)'}",
                  sections=[u["section_key"] for u in units], passes=[a["id"] for a in angles], provider=cfg["provider"], model=cfg["model"], focus=focus or None)
    step = 0
    for n, u in enumerate(units):
        scoped_cmds = {k: v for k, v in cmds.items() if k in u["commands"]}
        scoped_fields = {k: v for k, v in fields.items() if k in u["commands"]}
        cleared, kept = False, 0
        for angle in angles:
            if progress:                 # first thing in every pass: a failed or empty pass still checks for cancellation
                progress(step, len(units) * len(angles))
            step += 1
            tag = f"[{n + 1}/{len(units)}] §{u['number']} {u['title'][:50]} ({angle['id']})"
            if angle is angles[0]:
                eventlog.emit("info", "agent", f"{tag}: reading {len(u['primary'])} chars plus {len(u['related'])} connected passage(s), {len(u['commands'])} command(s) in scope",
                              section=u["section_key"], related=[{"key": r["key"], "why": r["why"]} for r in u["related"]], commands=u["commands"])
            msgs, corpus, ev = _messages(conn, set_id, u, focus, max_per_section, group, angle)
            task = f"cross:{angle['id']}"
            try:
                out, meta = svc.generate_model("generate", msgs, AIOutput, PROMPT_VERSION)
            except LLMError as e:
                stats["failed"] += 1
                eventlog.emit("error", "agent", f"{tag}: model failed: {str(e)[:200]}", section=u["section_key"], error=str(e))
                _record(conn, set_id, u["section_key"], svc, "failed", None, str(e), task=task)
                continue
            if not out.testable or not out.scenarios:
                stats["not_testable"] += 1
                eventlog.emit("info", "agent", f"{tag}: nothing for this angle: {(out.reason or 'no reason given')[:200]}", section=u["section_key"])
                _record(conn, set_id, u["section_key"], svc, "not_testable", out.model_dump(), out.reason, meta, task=task)
                continue
            if not cleared:      # a successful answer replaces this section's earlier unreviewed drafts; approved / rejected ones are never touched
                conn.execute("DELETE FROM scenario WHERE set_id=? AND origin='cross' AND state='draft' AND sid LIKE ? ESCAPE '\\'", (set_id, _like(u["section_key"]) + "#%"))
                cleared = True
            existing = {r["predicate_json"] for r in rows(conn, "SELECT predicate_json FROM scenario WHERE set_id=?", (set_id,))}
            created, problems = 0, []

            def store(title: str, desc: str, obs: str, pred: dict, evidence: list[str], steps: list[str], extra: dict) -> bool:
                nonlocal kept
                pj = json.dumps(pred)
                if pj in existing:
                    stats["duplicates"] += 1
                    return False
                if kept >= MAX_PER_SECTION:
                    return False
                taken = [int(r[0].rsplit("#", 1)[1]) for r in conn.execute("SELECT sid FROM scenario WHERE set_id=? AND sid LIKE ? ESCAPE '\\'", (set_id, _like(u["section_key"]) + "#%"))
                         if r[0].rsplit("#", 1)[1].isdigit()]
                sid = f"{u['section_key']}#{max(taken, default=0) + 1}"          # never collides with an approved / rejected scenario of the section
                cur = conn.execute("INSERT OR IGNORE INTO scenario(set_id,sid,origin,kind,title,description,observability,predicate_json,state,provenance_json)"
                                   " VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (set_id, sid, "cross", "cross", title, desc, obs, pj, "draft",
                                    json.dumps({**meta, "prompt_version": PROMPT_VERSION, "angle": angle["id"], "quotes": evidence, "section": u["section_key"],
                                                "related": [r["key"] for r in u["related"]], "focus": focus or None, **extra})))
                if not cur.rowcount:
                    return False
                existing.add(pj)
                kept += 1
                # linked to the commands it drives and to the statements it cites: those items are judged by it
                links = set(steps)
                for q in evidence:
                    nq = norm_text(q)
                    links |= {k for k, t in req_text.items() if t and (nq in t or t in nq)}
                conn.executemany("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key) VALUES(?,?)", [(cur.lastrowid, k) for k in sorted(links)])
                eventlog.emit("info", "agent", f"{tag}: drafted '{title[:120]}' ({len(steps)} steps, awaiting review)", section=u["section_key"], sid=sid, predicate=pred)
                return True

            for sc in out.scenarios[:max_per_section]:
                evidence, errs = _evidence(sc, ev, corpus)
                if len({s_.command for s_ in sc.steps}) < 2:
                    errs.append("not cross-command: needs at least two different commands")
                try:
                    pred = to_predicate(sc, scoped_cmds)
                    errs += validate_predicate(pred, scoped_cmds, scoped_fields)
                except ValueError as e:
                    pred, errs = None, errs + [str(e)]
                if errs:
                    stats["rejected"] += 1
                    problems.append(f"{sc.title}: {'; '.join(errs)}")
                    eventlog.emit("warn", "agent", f"{tag}: rejected draft '{sc.title[:100]}': {'; '.join(errs)[:200]}", section=u["section_key"], errors=errs, evidence=sc.evidence, quotes=sc.quotes)
                    continue
                if not store(sc.title, sc.description, sc.observability, pred, evidence, [s_.command for s_ in sc.steps], {}):
                    continue
                created += 1
                stats["created"] += 1
                for label, p2 in _variants(conn, set_id, sc, pred, scoped_fields):
                    if validate_predicate(p2, scoped_cmds, scoped_fields):
                        continue
                    if store(f"{sc.title} [{label}]", sc.description, sc.observability, p2, evidence, [s_.command for s_ in sc.steps], {"variant": label, "variant_of": sc.title}):
                        created += 1
                        stats["created"] += 1
                        stats["variants"] += 1
            _record(conn, set_id, u["section_key"], svc, "ok" if created else "needs_review", out.model_dump(), "; ".join(problems) or None, meta, task=task)
            conn.commit()
    eventlog.emit("info" if not stats["failed"] else "warn", "agent", "Cross-command agent finished: " + ", ".join(f"{v} {k}" for k, v in stats.items()), **stats)
    return stats
