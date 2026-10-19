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
from .ingest.normalize import norm_text
from .llm.base import LLMError
from .llm.service import LLMService
from .refs import load_set_irs, resolve_references
from .scenarios import catalog_context, validate_predicate

PROMPT_VERSION = "cross-v1"
PRIMARY_CHARS = 10_000       # budget for the selected section (its sub-sections and tables)
RELATED_CHARS = 8_000        # budget for all connected passages together
PASSAGE_CHARS = 2_000        # budget for one connected passage
CATALOG_CHARS = 9_000
MAX_COMMANDS = 14
MAX_GROUP = 5            # selected sections that still share each other's commands
MAX_ENTRIES_PER_COMMAND = 80

# ----------------------------------------------------------------------------- model output

class AIField(BaseModel):
    dw: int | Literal["nmd0", "nmd1"]
    hi: int
    lo: int
    op: Literal["eq", "ne", "in", "range", "mask"] = "eq"
    value: Any


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
    quotes: list[str] = Field(description="verbatim excerpts of the requirement that justify the scenario")


class AIOutput(BaseModel):
    testable: bool
    reason: str = ""
    scenarios: list[AIScenario] = []


SYSTEM = """You write functional test scenarios for a device specification, the way a test-script developer does.
A scenario is an ordered sequence of SEVERAL commands; it is checked against a log of SUBMITTED commands only
(no completions or responses are logged).

You are given SECTION (the part of the specification the scenarios are about), RELATED passages from elsewhere in the
specification that connect to it, and CATALOG (the only commands, bit ranges and values you may use).

Rules:
- Every scenario MUST use at least two different commands from CATALOG. A scenario about one command alone is useless here.
  Typical shapes: set something up, exercise the behaviour the SECTION describes, then observe it with another command;
  create a resource before using it and release it afterwards; configure, then read the configuration back.
- Take the ordering, preconditions and dependencies from the SECTION and RELATED text. Do not invent behaviour the text
  does not support. If the text implies no multi-command flow, reply testable=false with a reason and no scenarios.
- Never invent opcodes, commands or bit positions. Use only CATALOG. Field values are decimal integers; operators are
  'eq','ne','in' (list),'range' ({"lo","hi"}) or 'mask' ({"mask","value"}).
- Link steps through values: in an earlier step add "capture": {"name": {"dw":..,"hi":..,"lo":..}} and in a later step's
  field use "value": {"var": "name"} with op 'eq' or 'ne' (for example the identifier chosen in one command is the
  identifier used by the next). A captured value is a single integer.
- Give every step a short 'note' naming its role (setup, stimulus, follow-up check).
- order 'strict' when the text requires an order, otherwise 'any'. scope 'test' keeps all steps in one test.
- observability 'submission' = the whole flow is visible in what was submitted; 'stimulus' = only the commands that drive the
  behaviour are visible, the device's response cannot be verified.
- Each scenario needs 1+ quotes copied EXACTLY (character for character) from SECTION or RELATED text.
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
                      "fields": [f.model_dump() for f in st.fields],
                      **({"note": st.note} if st.note else {}),
                      **({"capture": {k: v.model_dump() for k, v in st.capture.items()}} if st.capture else {})})
    pred: dict[str, Any] = {"scope": sc.scope, "order": sc.order, "steps": steps}
    if sc.within is not None:   # 0 must reach validation (it is invalid), not silently mean 'unlimited'
        pred["within"] = sc.within
    return pred


def _record(conn, set_id, key, svc, state, output, error, meta=None):
    cfg = svc.task_cfg("generate")          # the row is labelled "cross"; provider / model are those configured for generation
    conn.execute("INSERT INTO enrichment(set_id,item_key,task,provider,model,prompt_version,state,output_json,error) VALUES(?,?,?,?,?,?,?,?,?)",
                 (set_id, key, "cross", (meta or {}).get("provider", cfg["provider"]), cfg["model"], PROMPT_VERSION, state,
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

def list_sections(conn: sqlite3.Connection, set_id: int, q: str | None = None, limit: int = 400) -> dict[str, Any]:
    """Sections of the set's documents for the picker; `commands` are the catalog commands defined inside each section."""
    ix = _Index(load_set_irs(conn, set_id))
    cmd_sec = {}
    for r in rows(conn, "SELECT key,label,attrs_json FROM catalog_item WHERE set_id=? AND kind='command'", (set_id,)):
        sec = jloads(r["attrs_json"], {}).get("section")
        if sec:
            cmd_sec.setdefault(sec, []).append({"key": r["key"], "name": r["label"]})
    out = []
    needle = (q or "").lower()
    for ir in ix.irs:
        for n in ir["section_order"]:
            s = ir["sections"][n]
            if needle and needle not in f"{s['number']} {s['title']}".lower():
                continue
            out.append({"key": s["key"], "family": ir["family"], "version": ir["version"], "number": s["number"], "title": s["title"],
                        "level": s["level"], "chars": len(s["text"]), "commands": cmd_sec.get(s["key"], [])})
    return {"total": len(out), "sections": out[:limit]}


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
        # commands of the other selected sections join last, and only for a small related group: in a large selection they
        # would bury the real relationships (select the sections that belong together, a few at a time)
        selected_cmds = [c for other in keys if other != key for sk in (s_["key"] for s_ in ix.subtree(other)) for c in cmd_by_sec.get(sk, [])] \
            if len(keys) <= MAX_GROUP else []
        involved = list(dict.fromkeys(unit_cmds + named + in_related +
                                      [m for rk, ms in req_mentions.items() if rk in own_nodes for m in ms] +
                                      [c for r_ in related if r_["why"] != "selected together with this section" for c in cmd_by_sec.get(r_["section"] or "", [])] +
                                      selected_cmds))
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

def _messages(conn: sqlite3.Connection, set_id: int, u: dict, focus: str, limit: int) -> tuple[list[dict], str]:
    catalog: list[dict] = []
    for ck in u["commands"]:
        catalog += _catalog_for(conn, set_id, ck)[:MAX_ENTRIES_PER_COMMAND]
    related = "\n\n".join(f"[{r['title']} - {r['why']}]\n{r['text']}" for r in u["related"]) or "(none found)"
    user = (f"SECTION §{u['number']} {u['title']}:\n{u['primary']}\n\nRELATED:\n{related}\n\n"
            f"CATALOG (the only commands/fields you may use):\n{json.dumps(catalog, indent=1)[:CATALOG_CHARS]}\n\n"
            + (f"FOCUS requested by the user: {focus}\n\n" if focus.strip() else "") + f"Write at most {limit} scenarios.")
    corpus = u["primary"] + "\n" + "\n".join(r["text"] for r in u["related"])
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], corpus


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def generate_cross_scenarios(conn: sqlite3.Connection, set_id: int, svc: LLMService, section_keys: list[str] | None = None,
                             command_keys: list[str] | None = None, focus: str = "", max_per_section: int = 6, progress=None) -> dict[str, int]:
    if not isinstance(max_per_section, int) or isinstance(max_per_section, bool) or not 1 <= max_per_section <= 20:
        raise ValueError("max_per_section must be an integer from 1 to 20")
    units = build_units(conn, set_id, section_keys, command_keys)
    cmds, fields = catalog_context(conn, set_id)
    req_text = {r["key"]: norm_text(jloads(r["attrs_json"], {}).get("text", "")) for r in rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=? AND kind='requirement'", (set_id,))}
    existing = {r["predicate_json"] for r in rows(conn, "SELECT predicate_json FROM scenario WHERE set_id=?", (set_id,))}
    stats = {"sections": len(units), "created": 0, "not_testable": 0, "rejected": 0, "failed": 0, "duplicates": 0}
    cfg = svc.task_cfg("generate")
    eventlog.emit("info", "agent", f"Cross-command agent starting: {len(units)} section(s), model {cfg['provider']}/{cfg['model'] or '(unset)'}",
                  sections=[u["section_key"] for u in units], provider=cfg["provider"], model=cfg["model"], focus=focus or None)
    for n, u in enumerate(units):
        if progress:
            progress(n, len(units))
        tag = f"[{n + 1}/{len(units)}] §{u['number']} {u['title'][:60]}"
        eventlog.emit("info", "agent", f"{tag}: reading {len(u['primary'])} chars plus {len(u['related'])} connected passage(s), {len(u['commands'])} command(s) in scope",
                      section=u["section_key"], related=[{"key": r["key"], "why": r["why"]} for r in u["related"]], commands=u["commands"])
        msgs, corpus = _messages(conn, set_id, u, focus, max_per_section)
        try:
            out, meta = svc.generate_model("generate", msgs, AIOutput, PROMPT_VERSION)
        except LLMError as e:
            stats["failed"] += 1
            eventlog.emit("error", "agent", f"{tag}: model failed: {str(e)[:200]}", section=u["section_key"], error=str(e))
            _record(conn, set_id, u["section_key"], svc, "failed", None, str(e))
            continue
        if not out.testable or not out.scenarios:
            stats["not_testable"] += 1
            eventlog.emit("info", "agent", f"{tag}: no multi-command flow found: {(out.reason or 'no reason given')[:200]}", section=u["section_key"])
            _record(conn, set_id, u["section_key"], svc, "not_testable", out.model_dump(), out.reason, meta)
            continue
        # a successful answer replaces this section's earlier unreviewed drafts; approved / rejected scenarios are never touched
        conn.execute("DELETE FROM scenario WHERE set_id=? AND origin='cross' AND state='draft' AND sid LIKE ? ESCAPE '\\'",
                     (set_id, _like(u["section_key"]) + "#%"))
        existing = {r["predicate_json"] for r in rows(conn, "SELECT predicate_json FROM scenario WHERE set_id=?", (set_id,))}
        scoped_cmds = {k: v for k, v in cmds.items() if k in u["commands"]}
        scoped_fields = {k: v for k, v in fields.items() if k in u["commands"]}
        created, problems = 0, []
        for sc in out.scenarios[:max_per_section]:
            errs: list[str] = []
            if not sc.quotes or not all(_grounded(q, corpus) for q in sc.quotes):
                errs.append("ungrounded quote(s)")
            if len({(s.command) for s in sc.steps}) < 2:
                errs.append("not cross-command: needs at least two different commands")
            try:
                pred = to_predicate(sc, scoped_cmds)
                errs += validate_predicate(pred, scoped_cmds, scoped_fields)
            except ValueError as e:
                pred, errs = None, errs + [str(e)]
            if errs:
                stats["rejected"] += 1
                problems.append(f"{sc.title}: {'; '.join(errs)}")
                eventlog.emit("warn", "agent", f"{tag}: rejected draft '{sc.title[:100]}': {'; '.join(errs)[:200]}", section=u["section_key"], errors=errs, quotes=sc.quotes)
                continue
            pj = json.dumps(pred)
            if pj in existing:
                stats["duplicates"] += 1
                continue
            existing.add(pj)
            taken = [int(r[0].rsplit("#", 1)[1]) for r in conn.execute("SELECT sid FROM scenario WHERE set_id=? AND sid LIKE ? ESCAPE '\\'", (set_id, _like(u["section_key"]) + "#%"))
                     if r[0].rsplit("#", 1)[1].isdigit()]
            sid = f"{u['section_key']}#{max(taken, default=0) + 1}"          # never collides with an approved / rejected scenario of the section
            cur = conn.execute("INSERT OR IGNORE INTO scenario(set_id,sid,origin,kind,title,description,observability,predicate_json,state,provenance_json)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (set_id, sid, "cross", "cross", sc.title, sc.description, sc.observability, pj, "draft",
                                json.dumps({**meta, "prompt_version": PROMPT_VERSION, "quotes": sc.quotes, "section": u["section_key"],
                                            "related": [r["key"] for r in u["related"]], "focus": focus or None})))
            if not cur.rowcount:
                continue
            created += 1
            stats["created"] += 1
            # linked to the commands it drives and to the statements it quotes: those items are judged by it
            links = {s.command for s in sc.steps}
            for q in sc.quotes:
                nq = norm_text(q)
                links |= {k for k, t in req_text.items() if t and (nq in t or t in nq)}
            conn.executemany("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key) VALUES(?,?)", [(cur.lastrowid, k) for k in sorted(links)])
            eventlog.emit("info", "agent", f"{tag}: drafted '{sc.title[:120]}' ({len(sc.steps)} steps, awaiting review)", section=u["section_key"], sid=sid, predicate=pred)
        _record(conn, set_id, u["section_key"], svc, "ok" if created else "needs_review", out.model_dump(), "; ".join(problems) or None, meta)
        conn.commit()
    eventlog.emit("info" if not stats["failed"] else "warn", "agent", "Cross-command agent finished: " + ", ".join(f"{v} {k}" for k, v in stats.items()), **stats)
    return stats
