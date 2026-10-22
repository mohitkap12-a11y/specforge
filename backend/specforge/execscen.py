"""Scenarios derived from execution: what the test scripts in a command database actually did.

Every test case in an imported command DB becomes one scenario: its submitted commands in order, named from the catalog,
with the value of every described field.  Tests that ran the same flow are merged into one scenario that lists all of them.

  * A *step* is a run of consecutive commands of the same catalog command with the same values in every discrete field
    (a field with enumerated values, or a one-bit flag).  Numeric fields (addresses, counts, sizes) are summarised per step
    as min / max / number of distinct values, so a loop of 10,000 reads is one step, not 10,000.
  * Two tests ran the *same flow* when their steps name the same commands with the same discrete values in the same order;
    repeat counts and numeric values do not matter.

The catalog items a scenario exercised (commands, fields, values, boundaries, reserved bits set, Admin commands tunnelled
over NVMe-MI) are linked to it as 'observed'.  They are read from the log, so they count toward coverage without review.

Requirements (normative statements) are not visible in a log of submissions.  `describe_scenarios` asks the configured model
to describe each flow in words and to pick, from the requirements of the commands the flow used, the ones it exercises.
Those links are 'proposed' and count toward coverage only after a reviewer approves them.

Nothing here changes the command DB or a coverage verdict on its own: `coverage.run_coverage` reads the links.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from . import eventlog
from .coverage import OBSERVED_KINDS
from .core.db import jloads, row, rows
from .llm.base import LLMError
from .llm.service import LLMService

MAX_STEPS = 400            # steps kept per scenario (the flow identity still covers every step)
MAX_TESTS = 500            # test names listed per scenario
EVIDENCE_ROWS = 25         # command rows kept per scenario as evidence
MAX_DISTINCT = 16          # distinct values remembered per numeric field and step
PROGRESS_EVERY = 5000

PROMPT_VERSION = "exec-v1"
MAX_CANDIDATES = 40        # requirements shown to the model per scenario
FLOW_CHARS = 7000
REQ_CHARS = 9000


# ----------------------------------------------------------------------------- catalog view

def _not_preserved(iface: str, dw: Any, hi: int) -> bool:
    """CID (dw0[31:16]) and the pointers (dw4-9) are not part of a command signature; the stored dwords are one row's values."""
    return iface == "pcie" and (dw in range(4, 10) or (dw == 0 and hi >= 16))


class _Catalog:
    def __init__(self, conn: sqlite3.Connection, set_id: int):
        self.cmds: dict[str, dict] = {}
        self.fields: dict[str, list[dict]] = defaultdict(list)
        self.reserved: dict[str, list[dict]] = defaultdict(list)
        self.tunnel: dict[str, str] = {}
        self.items: dict[str, dict] = {}
        kids: dict[str, list[dict]] = defaultdict(list)
        for r in rows(conn, "SELECT key,kind,parent_key,label,observability,attrs_json FROM catalog_item WHERE set_id=?", (set_id,)):
            a = jloads(r["attrs_json"], {})
            self.items[r["key"]] = {"kind": r["kind"], "label": r["label"], "observability": r["observability"], "attrs": a}
            if r["kind"] == "command":
                self.cmds[r["key"]] = {**a, "key": r["key"]}
            elif r["kind"] == "field":
                self.fields[a["command"]].append({**a, "key": r["key"], "label": r["label"]})
            elif r["kind"] == "reserved":
                self.reserved[a["command"]].append({**a, "key": r["key"]})
            elif r["kind"] == "mi_tunnel":
                self.tunnel[a["command"]] = r["key"]
            elif r["kind"] in ("value", "boundary") and r["parent_key"]:
                kids[r["parent_key"]].append({**a, "key": r["key"], "kind": r["kind"]})
        for fl in self.fields.values():
            for f in fl:
                f["values"] = [k for k in kids.get(f["key"], []) if k["kind"] == "value"]
                f["bounds"] = [k for k in kids.get(f["key"], []) if k["kind"] == "boundary"]
                f["discrete"] = bool(f["values"])
            fl.sort(key=lambda f: (str(type(f["dw"])), str(f["dw"]) if isinstance(f["dw"], str) else f"{f['dw']:02d}", -f["hi"]))

    @staticmethod
    def command_key(iface: str, queue: str, cs: str, opcode: int) -> str:
        if iface == "mi" and queue == "mi":
            return f"cmd:mi:{opcode:02X}"
        if queue == "admin":
            return f"cmd:admin:{opcode:02X}"
        return f"cmd:{queue}:{cs}:{opcode:02X}"


def _bits(dwords: dict[str, int], dw: Any, hi: int, lo: int) -> int | None:
    v = dwords.get(str(dw))
    return None if v is None else (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def _decode_signature(cat: _Catalog, sig: dict) -> dict:
    """What one distinct submitted command means in catalog terms (computed once per signature, reused for every row)."""
    iface, queue, opc = sig["interface"], sig["queue"], sig["opcode"]
    key = cat.command_key(iface, queue, sig["cs"], opc)
    cmd = cat.cmds.get(key)
    out: dict[str, Any] = {"command": key if cmd else None, "interface": iface, "queue": queue, "opcode": opc, "cs": sig["cs"],
                           "tunnel": iface == "mi" and queue == "admin", "discrete": [], "numeric": [], "reserved_set": [],
                           "items": set(), "undescribed": []}
    if cmd is None:
        out["name"] = f"Unknown {'MI' if queue == 'mi' else queue.capitalize()} opcode {opc:02X}h"
        return out
    out["name"] = cmd["name"]
    items = out["items"]
    items.add(key)
    if out["tunnel"] and key in cat.tunnel:
        items.add(cat.tunnel[key])
    dws = sig["dwords"]
    for f in cat.fields.get(key, []):
        if _not_preserved(iface, f["dw"], f["hi"]):
            continue
        v = _bits(dws, f["dw"], f["hi"], f["lo"])
        if v is None:
            continue
        items.add(f["key"])
        name = f.get("abbr") or f.get("name") or f["label"]
        if f["discrete"]:
            hit = [x for x in f["values"] if x["vlo"] <= v <= x["vhi"]]
            items.update(x["key"] for x in hit)
            meaning = hit[0].get("desc", "")[:80] if hit else ""
            out["discrete"].append({"key": f["key"], "name": name, "dw": f["dw"], "hi": f["hi"], "lo": f["lo"], "value": v,
                                    "meaning": meaning, **({} if hit else {"unlisted": True}),
                                    **({"flag": True} if all(x.get("implicit") for x in f["values"]) else {}),
                                    **({"reserved_value": True} if any(x.get("reserved_value") for x in hit) else {})})
            if not hit:
                out["undescribed"].append(f["key"])
        else:
            items.update(b["key"] for b in f["bounds"] if b["vlo"] == v)
            out["numeric"].append({"key": f["key"], "name": name, "dw": f["dw"], "hi": f["hi"], "lo": f["lo"], "value": v})
    for r_ in cat.reserved.get(key, []):
        if _not_preserved(iface, r_["dw"], r_["hi"]):
            continue
        v = _bits(dws, r_["dw"], r_["hi"], r_["lo"])
        if v:
            items.add(r_["key"])
            out["reserved_set"].append({"key": r_["key"], "dw": r_["dw"], "hi": r_["hi"], "lo": r_["lo"], "value": v})
    return out


def _step_key(d: dict) -> tuple:
    who = d["command"] or f"?{d['interface']}:{d['queue']}:{d['cs']}:{d['opcode']}"
    return (who, d["tunnel"], tuple((x["key"], x["value"]) for x in d["discrete"]), tuple((x["key"], x["value"]) for x in d["reserved_set"]))


def _new_step(d: dict, r: dict) -> dict:
    return {"command": d["command"], "name": d["name"], "interface": d["interface"], "queue": d["queue"], "opcode": d["opcode"],
            "cs": d["cs"] if d["queue"] == "io" else None, "tunnel": d["tunnel"], "count": 0,
            "first": {"test": r["test_id"], "seq": r["seq"], "row": r["src_row_id"]},
            "fields": [dict(x) for x in d["discrete"]],
            "ranges": {x["key"]: {"key": x["key"], "name": x["name"], "dw": x["dw"], "hi": x["hi"], "lo": x["lo"],
                                  "min": x["value"], "max": x["value"], "values": [x["value"]]} for x in d["numeric"]},
            "reserved_set": [dict(x) for x in d["reserved_set"]]}


def _add_to_step(st: dict, d: dict) -> None:
    st["count"] += 1
    for x in d["numeric"]:
        v = x["value"]
        rg = st["ranges"].get(x["key"])
        if rg is None:       # the dword was absent (not recorded) in the step's first command
            st["ranges"][x["key"]] = {"key": x["key"], "name": x["name"], "dw": x["dw"], "hi": x["hi"], "lo": x["lo"], "min": v, "max": v, "values": [v]}
            continue
        rg["min"], rg["max"] = min(rg["min"], v), max(rg["max"], v)
        if isinstance(rg["values"], list) and v not in rg["values"]:
            rg["values"] = rg["values"] + [v] if len(rg["values"]) < MAX_DISTINCT else f">{MAX_DISTINCT}"


def _merge_steps(into: list[dict], other: list[dict]) -> None:
    """Steps of two tests that ran the same flow line up one to one; counts and numeric ranges are combined."""
    for a, b in zip(into, other):
        a["count"] += b["count"]
        for k, rb in b["ranges"].items():
            ra = a["ranges"].get(k)
            if ra is None:
                a["ranges"][k] = json.loads(json.dumps(rb))
                continue
            ra["min"], ra["max"] = min(ra["min"], rb["min"]), max(ra["max"], rb["max"])
            if isinstance(ra["values"], list) and isinstance(rb["values"], list):
                vals = ra["values"] + [v for v in rb["values"] if v not in ra["values"]]
                ra["values"] = vals if len(vals) <= MAX_DISTINCT else f">{MAX_DISTINCT}"
            else:
                ra["values"] = f">{MAX_DISTINCT}"


# ----------------------------------------------------------------------------- titles

def fmt_value(v: int) -> str:
    return f"{v:02X}h" if v < 0x100 else f"{v:X}h"


def flow_summary(steps: list[dict], limit: int = 110) -> str:
    """'Identify (CNS 00h, 01h) → Get Log Page → Read ×200': consecutive steps of one command are named once."""
    groups: list[tuple[str, list[str], int]] = []
    for st in steps:
        label = st["name"] + (" via MI" if st["tunnel"] else "")
        # the first enumerated field names the step (FID, CNS, LID ...); otherwise the flags that are set
        main = next((f for f in st["fields"] if not f.get("flag")), None)
        tag = f"{main['name']} {fmt_value(main['value'])}" if main else "+".join(f["name"] for f in st["fields"] if f["value"])
        if groups and groups[-1][0] == label:
            name, tags, n = groups[-1]
            groups[-1] = (name, tags + ([tag] if tag and tag not in tags else []), n + st["count"])
        else:
            groups.append((label, [tag] if tag else [], st["count"]))
    parts = []
    for name, tags, n in groups:
        t = name
        if tags:
            t += f" ({', '.join(tags[:3])}{', …' if len(tags) > 3 else ''})"
        if n > 1 and len(tags) <= 1:
            t += f" ×{n}"
        parts.append(t)
    out = ""
    for i, p in enumerate(parts):
        nxt = (out + " → " if out else "") + p
        if len(nxt) > limit and out:
            return out + f" → … (+{len(parts) - i} more)"
        out = nxt
    return out or "(no commands)"


def _title(tests: list[str], n_tests: int, steps: list[dict]) -> str:
    who = tests[0] if n_tests == 1 else f"{tests[0]} (+{n_tests - 1} with the same flow)"
    return f"{who}: {flow_summary(steps)}"


def _description(n_tests: int, commands: int, steps: list[dict], truncated: bool) -> str:
    names = list(dict.fromkeys(st["name"] for st in steps))
    unknown = sum(1 for st in steps if st["command"] is None)
    return (f"Executed by {n_tests} test{'s' if n_tests != 1 else ''}: {commands} command{'s' if commands != 1 else ''} in "
            f"{len(steps)}{'+' if truncated else ''} step{'s' if len(steps) != 1 else ''}, {len(names)} distinct command type"
            f"{'s' if len(names) != 1 else ''} ({', '.join(names[:8])}{', …' if len(names) > 8 else ''})."
            + (f" {unknown} step(s) use opcodes that are not in the catalog." if unknown else ""))


# ----------------------------------------------------------------------------- analysis

def _group_of(r: dict) -> tuple[str, str]:
    """(kind, name): a test named 't' and a script named 't' in rows without a test name are different groups."""
    if r["test_id"]:
        return "test", r["test_id"]
    return ("script", r["script"]) if r["script"] else ("all", "(all commands)")


def analyze_execution(conn: sqlite3.Connection, set_id: int, source_id: int, progress=None) -> dict[str, Any]:
    """Derive one scenario per test case of an imported command DB and link it to the catalog items it exercised.

    Re-running is safe: scenarios are keyed by their flow, so a flow seen before keeps its id, its AI description and the
    review of its requirement links; observed links are recomputed; flows no longer in the DB are removed."""
    src = row(conn, "SELECT * FROM cmd_source WHERE id=?", (source_id,))
    if not src or src["status"] != "imported":
        raise ValueError("command source must be imported before it can be analysed")
    if not row(conn, "SELECT 1 AS x FROM catalog_item WHERE set_id=? LIMIT 1", (set_id,)):
        raise ValueError("build the catalog of this spec set before analysing a command DB")
    cat = _Catalog(conn, set_id)
    decoded: dict[str, dict] = {}
    for s in conn.execute("SELECT signature,interface,queue,cs,opcode,dwords_json FROM cmd_signature WHERE source_id=?", (source_id,)):
        decoded[s["signature"]] = _decode_signature(cat, {"interface": s["interface"], "queue": s["queue"], "cs": s["cs"],
                                                          "opcode": s["opcode"], "dwords": json.loads(s["dwords_json"])})
    eventlog.emit("info", "exec", f"Analysing '{src['name'] or src['path']}': {src['decoded_count']} commands, {len(decoded)} distinct, "
                  f"against the catalog of set #{set_id}", set_id=set_id, source_id=source_id)

    flows: dict[str, dict] = {}        # fingerprint -> merged scenario
    unknown: dict[tuple, int] = defaultdict(int)
    stats = {"tests": 0, "commands": 0, "unknown_commands": 0}

    def finish(test: str, t: dict) -> None:
        fp = t["hash"].hexdigest()[:20]
        stats["tests"] += 1
        f = flows.get(fp)
        if f is None:
            flows[fp] = {"tests": [test], "n_tests": 1, "steps": t["steps"], "truncated": t["truncated"], "n_steps": t["n_steps"],
                         "commands": t["commands"], "items": t["items"], "evidence": t["evidence"]}
            return
        f["n_tests"] += 1
        if len(f["tests"]) < MAX_TESTS:
            f["tests"].append(test)
        f["commands"] += t["commands"]
        _merge_steps(f["steps"], t["steps"])
        for k, n in t["items"].items():
            f["items"][k] = f["items"].get(k, 0) + n
        if len(f["evidence"]) < EVIDENCE_ROWS:
            f["evidence"] += t["evidence"][:EVIDENCE_ROWS - len(f["evidence"])]

    cur_test, t = None, None
    n = 0
    for r in conn.execute("SELECT id,src_row_id,test_id,script,seq,signature FROM decoded_command WHERE source_id=? "
                          "ORDER BY test_id, CASE WHEN test_id='' THEN script ELSE '' END, seq, id", (source_id,)):   # a test may span scripts
        r = dict(r)
        n += 1
        if progress and n % PROGRESS_EVERY == 0:
            progress(n, src["decoded_count"] or 0)
        g = _group_of(r)
        if g != cur_test:
            if t is not None:
                finish(cur_test[1], t)
            cur_test = g
            t = {"steps": [], "last": None, "open": False, "hash": hashlib.sha1(), "truncated": False, "n_steps": 0, "commands": 0,
                 "items": {}, "evidence": []}
        d = decoded[r["signature"]]
        t["commands"] += 1
        stats["commands"] += 1
        if d["command"] is None:
            stats["unknown_commands"] += 1
            unknown[(d["interface"], d["queue"], d["opcode"])] += 1
        for k in d["items"]:
            t["items"][k] = t["items"].get(k, 0) + 1
        if len(t["evidence"]) < EVIDENCE_ROWS:
            t["evidence"].append({"src_row_id": r["src_row_id"], "test_id": r["test_id"], "script": r["script"], "seq": r["seq"],
                                  "command": d["name"]})
        sk = _step_key(d)
        if sk != t["last"]:                 # a new step starts; past MAX_STEPS it still counts toward the flow's identity
            t["last"] = sk
            t["n_steps"] += 1
            t["hash"].update(json.dumps(sk, default=str).encode() + b"\n")
            t["open"] = len(t["steps"]) < MAX_STEPS
            if t["open"]:
                t["steps"].append(_new_step(d, r))
            else:
                t["truncated"] = True
        if t["open"]:
            _add_to_step(t["steps"][-1], d)
    if t is not None:
        finish(cur_test[1], t)

    res = _store(conn, set_id, source_id, cat, flows)
    by_kind: dict[str, int] = defaultdict(int)
    observed = set()
    for f in flows.values():
        observed |= set(f["items"])
    for k in observed:
        by_kind[cat.items[k]["kind"]] += 1
    totals: dict[str, int] = defaultdict(int)
    for it in cat.items.values():
        if it["kind"] in OBSERVED_KINDS:
            totals[it["kind"]] += 1
    summary = {**stats, "scenarios": len(flows), **res,
               # a field is part of every command sent (even as 0): how much of it was exercised is told by its values and boundaries
               "observed": {k: {"observed": by_kind.get(k, 0), "total": totals[k]} for k in OBSERVED_KINDS if totals.get(k) and k != "field"},
               "unknown": sorted(({"interface": i, "queue": q, "opcode": o, "commands": c} for (i, q, o), c in unknown.items()),
                                 key=lambda u: -u["commands"])}
    conn.execute("INSERT INTO exec_analysis(set_id,source_id,stats_json) VALUES(?,?,?) ON CONFLICT(set_id,source_id) "
                 "DO UPDATE SET stats_json=excluded.stats_json, created=CURRENT_TIMESTAMP", (set_id, source_id, json.dumps(summary)))
    conn.commit()
    eventlog.emit("warn" if unknown else "info", "exec",
                  f"Analysis done: {stats['tests']} test(s), {stats['commands']} commands -> {len(flows)} executed scenario(s) "
                  f"({res['new']} new, {res['updated']} updated, {res['removed']} removed); "
                  + ", ".join(f"{v['observed']}/{v['total']} {k}" for k, v in summary["observed"].items())
                  + (f"; {stats['unknown_commands']} command(s) with opcodes not in the catalog" if unknown else ""),
                  set_id=set_id, source_id=source_id, **{k: v for k, v in summary.items() if k != "unknown"}, unknown=summary["unknown"][:10])
    return summary


def _observability(cat: _Catalog, items: dict[str, int]) -> str:
    """'stimulus' when the flow only drives behaviour the log cannot confirm (reserved bits, reserved values)."""
    for k in items:
        it = cat.items.get(k)
        if it and it["kind"] in ("command", "value", "field", "boundary") and it["observability"] == "submission":
            return "submission"
    return "stimulus"


def _store(conn: sqlite3.Connection, set_id: int, source_id: int, cat: _Catalog, flows: dict[str, dict]) -> dict[str, int]:
    old = {r["sid"]: r for r in rows(conn, "SELECT id,sid,title,description,provenance_json FROM scenario WHERE set_id=? AND origin='exec' AND source_id=?",
                                     (set_id, source_id))}
    new = updated = reopened = 0
    keep = set()
    for fp, f in flows.items():
        sid = f"exec:{source_id}:{fp}"
        keep.add(sid)
        steps = [{**st, "ranges": list(st["ranges"].values())} for st in f["steps"]]
        # what was executed, kept with the scenario so a coverage run's snapshot (and its report) shows the tests too
        pred = {"observed": True, "tests": f["tests"], "test_count": f["n_tests"], "commands": f["commands"], "truncated": f["truncated"], "n_steps": f["n_steps"], "steps": steps}
        title, desc = _title(f["tests"], f["n_tests"], f["steps"]), _description(f["n_tests"], f["commands"], f["steps"], f["truncated"])
        prov = {"tests": f["tests"], "test_count": f["n_tests"], "commands": f["commands"], "steps": f["n_steps"], "truncated": f["truncated"],
                "fingerprint": fp, "evidence": f["evidence"], "auto_title": title, "auto_description": desc}
        o = old.get(sid)
        obs = _observability(cat, f["items"])
        if o is None:
            cur = conn.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,description,observability,predicate_json,state,provenance_json,source_id)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (set_id, sid, "exec", "executed", title, desc, obs, json.dumps(pred), "observed",
                                                                  json.dumps(prov), source_id))
            scen_id = cur.lastrowid
            new += 1
        else:
            scen_id = o["id"]
            op = jloads(o["provenance_json"], {})
            for k in ("ai", "edited"):                    # an AI or reviewer description survives re-analysis
                if k in op:
                    prov[k] = op[k]
            described = "ai" in prov or "edited" in prov
            conn.execute("UPDATE scenario SET title=?, description=?, observability=?, predicate_json=?, provenance_json=? WHERE id=?",
                         (o["title"] if described else title, o["description"] if described else desc, obs, json.dumps(pred), json.dumps(prov), scen_id))
            before = {r[0] for r in conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=? AND state='observed'", (scen_id,))}
            if before - set(f["items"]):
                # the flow is the same but no longer exercises everything it did (other numeric values): approved requirement links were
                # judged against the old evidence, so they go back to review. A flow that only gains items keeps its reviews.
                reopened += conn.execute("UPDATE scenario_item SET state='proposed' WHERE scenario_id=? AND state='approved'", (scen_id,)).rowcount
            conn.execute("DELETE FROM scenario_item WHERE scenario_id=? AND state='observed'", (scen_id,))
            updated += 1
        conn.executemany("INSERT OR REPLACE INTO scenario_item(scenario_id,item_key,state,detail_json) VALUES(?,?,'observed',?)",
                         [(scen_id, k, json.dumps({"commands": c})) for k, c in sorted(f["items"].items())])
    gone = [o["id"] for s, o in old.items() if s not in keep]
    conn.executemany("DELETE FROM scenario WHERE id=?", [(i,) for i in gone])
    conn.commit()
    return {"new": new, "updated": updated, "removed": len(gone), "links_reopened": reopened}


def analysis(conn: sqlite3.Connection, set_id: int, source_id: int) -> dict | None:
    r = row(conn, "SELECT stats_json,created FROM exec_analysis WHERE set_id=? AND source_id=?", (set_id, source_id))
    return {**jloads(r["stats_json"], {}), "created": r["created"]} if r else None


def set_link(conn: sqlite3.Connection, scenario_id: int, item_key: str, state: str) -> None:
    """Review one AI-proposed requirement link: 'approved', 'rejected' or back to 'proposed'.  Observed links are facts and cannot change."""
    if state not in ("proposed", "approved", "rejected"):
        raise ValueError("state must be 'proposed', 'approved' or 'rejected'")
    cur = row(conn, "SELECT state FROM scenario_item WHERE scenario_id=? AND item_key=?", (scenario_id, item_key))
    if cur is None:
        raise KeyError(item_key)
    if cur["state"] not in ("proposed", "approved", "rejected"):
        raise ValueError("only requirement links proposed for an executed scenario can be reviewed")
    conn.execute("UPDATE scenario_item SET state=? WHERE scenario_id=? AND item_key=?", (state, scenario_id, item_key))
    conn.commit()


def links(conn: sqlite3.Connection, scenario_id: int) -> list[dict]:
    s = row(conn, "SELECT set_id FROM scenario WHERE id=?", (scenario_id,))
    if not s:
        raise KeyError(scenario_id)
    out = rows(conn, """SELECT si.item_key AS key, si.state, si.detail_json, c.kind, c.label, c.observability, c.attrs_json
                        FROM scenario_item si LEFT JOIN catalog_item c ON c.set_id=? AND c.key=si.item_key
                        WHERE si.scenario_id=? ORDER BY si.item_key""", (s["set_id"], scenario_id))
    for r in out:
        r["detail"] = jloads(r.pop("detail_json"), {})
        a = jloads(r.pop("attrs_json"), {})
        if r["kind"] == "requirement":
            r["text"] = a.get("text", "")
            r["strength"] = a.get("strength")
    return out


# ----------------------------------------------------------------------------- AI: describe + propose requirement links

class AIReqLink(BaseModel):
    id: str = Field(description="id of a REQUIREMENTS entry, e.g. 'R3'")
    why: str = Field(default="", description="which step(s) exercise it, in a few words")


class AIDescription(BaseModel):
    title: str = Field(description="what the test does, in at most 12 words")
    description: str = Field(description="2-4 sentences: the purpose of the flow and what each phase of it does")
    requirements: list[AIReqLink] = []


SYSTEM = """You read the command log of one automated test of a storage device and explain what it tested.

You are given FLOW: the commands the test submitted, in order, with the values of their fields (numeric fields as ranges;
'×N' means the step repeated N times), and REQUIREMENTS: numbered statements from the specification about the commands
the flow used.

Reply with one JSON object:
- "title": what the test does, in at most 12 words, in the words of a test engineer (not a list of command names).
- "description": 2-4 sentences on the purpose of the flow and what each phase does (setup, exercise, check, clean-up).
- "requirements": the REQUIREMENTS entries this flow actually exercises: a step submits the command with the field values
  the statement is about, or the order of steps is what the statement requires. Copy the ids exactly (e.g. "R3") and say in
  "why" which step(s) do it. Leave out statements the flow does not exercise; an empty list is a valid answer.
Only the submitted commands are logged, never the device's responses: do not claim the device's behaviour was checked."""


def _flow_text(steps: list[dict], truncated: bool) -> str:
    lines = []
    for i, st in enumerate(steps, 1):
        where = {"admin": "Admin", "io": "I/O", "mi": "MI"}.get(st["queue"], st["queue"]) + (" over NVMe-MI" if st["tunnel"] else "")
        parts = [f"{f['name']}={fmt_value(f['value'])}" + (f" ({f['meaning'][:50]})" if f.get("meaning") else "") for f in st["fields"]]
        parts += [f"{r_['name']}={fmt_value(r_['min'])}" if r_["min"] == r_["max"] else f"{r_['name']}={fmt_value(r_['min'])}..{fmt_value(r_['max'])}"
                  for r_ in st["ranges"]]
        parts += [f"reserved {r_['dw']}[{r_['hi']}:{r_['lo']}]={fmt_value(r_['value'])}" for r_ in st["reserved_set"]]
        lines.append(f"{i}. {st['name']} ({where} {st['opcode']:02X}h)" + (f" ×{st['count']}" if st["count"] > 1 else "")
                     + (": " + ", ".join(parts) if parts else ""))
    out, used = [], 0
    for ln in lines:
        if used + len(ln) > FLOW_CHARS:
            out.append(f"... {len(lines) - len(out)} more step(s)")
            break
        out.append(ln)
        used += len(ln) + 1
    if truncated:
        out.append("... the log continues beyond the steps shown")
    return "\n".join(out)


def _candidates(conn: sqlite3.Connection, set_id: int, scen_id: int, steps: list[dict]) -> list[dict]:
    """Observable requirements of the commands the flow used, best first: fields it set, then its commands, then mentions."""
    linked = rows(conn, "SELECT si.item_key,si.state,c.kind FROM scenario_item si LEFT JOIN catalog_item c ON c.set_id=? AND c.key=si.item_key "
                        "WHERE si.scenario_id=?", (set_id, scen_id))
    have = {r["item_key"]: r["state"] for r in linked}
    cmds = {st["command"] for st in steps if st["command"]}
    fields = {r["item_key"] for r in linked if r["state"] == "observed" and r["kind"] == "field"}
    out = []
    for r in rows(conn, "SELECT key,observability,attrs_json FROM catalog_item WHERE set_id=? AND kind='requirement' AND observability<>'unobservable'", (set_id,)):
        if have.get(r["key"]) == "rejected":
            continue          # a reviewer said no: never proposed again for this scenario
        a = jloads(r["attrs_json"], {})
        score = 3 * len(fields & set(a.get("fields") or [])) + (2 if a.get("command") in cmds else 0) + len(cmds & set(a.get("mentions") or []))
        if score:
            out.append({"key": r["key"], "text": a.get("text", ""), "score": score})
    out.sort(key=lambda c: (-c["score"], c["key"]))
    return out[:MAX_CANDIDATES]


def _req_text(cands: list[dict]) -> tuple[str, int]:
    """The numbered requirements that fit the prompt budget, and how many of the candidates that is."""
    out, used = [], 0
    for i, c in enumerate(cands, 1):
        ln = f"[R{i}] {c['text'][:400]}"
        if used + len(ln) > REQ_CHARS:
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out) or "(none)", len(out)


def describe_scenarios(conn: sqlite3.Connection, set_id: int, source_id: int, svc: LLMService, scenario_ids: list[int] | None = None,
                       redo: bool = False, progress=None) -> dict[str, int]:
    """One model call per executed scenario: a readable title/description and proposed requirement links (for review).

    Without `scenario_ids`, every executed scenario of the command DB that has no AI description yet (all of them with `redo`)."""
    sql, args = "SELECT * FROM scenario WHERE set_id=? AND origin='exec' AND source_id=?", [set_id, source_id]
    if scenario_ids:
        sql += f" AND id IN ({','.join('?' * len(scenario_ids))})"
        args += scenario_ids
    scen = [s for s in rows(conn, sql + " ORDER BY id", args) if redo or scenario_ids or "ai" not in jloads(s["provenance_json"], {})]
    cfg = svc.task_cfg("generate")
    stats = {"scenarios": len(scen), "described": 0, "proposed": 0, "failed": 0, "dropped_ids": 0}
    eventlog.emit("info", "agent", f"Execution describer starting: {len(scen)} executed scenario(s), model {cfg['provider']}/{cfg['model'] or '(unset)'}",
                  set_id=set_id, source_id=source_id)
    for n, s in enumerate(scen):
        if progress:
            progress(n, len(scen))
        pred = jloads(s["predicate_json"], {})
        prov = jloads(s["provenance_json"], {})
        steps = pred.get("steps", [])
        cands = _candidates(conn, set_id, s["id"], steps)
        req_text, shown = _req_text(cands)
        user = (f"FLOW ({prov.get('test_count', 1)} test(s), e.g. {', '.join(prov.get('tests', [])[:3])}):\n{_flow_text(steps, prov.get('truncated', False))}\n\n"
                f"REQUIREMENTS:\n{req_text}")
        tag = f"[{n + 1}/{len(scen)}] {s['title'][:70]}"
        try:
            out, meta = svc.generate_model("generate", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], AIDescription, PROMPT_VERSION)
        except LLMError as e:
            stats["failed"] += 1
            eventlog.emit("error", "agent", f"{tag}: model failed: {str(e)[:200]}", scenario_id=s["id"], error=str(e))
            continue
        by_id = {f"R{i}": c for i, c in enumerate(cands[:shown], 1)}       # only ids the model actually saw may be cited
        picked = []
        for ln in out.requirements:
            c = by_id.get(ln.id.strip().upper())
            if c is None:
                stats["dropped_ids"] += 1
                continue
            picked.append((c["key"], ln.why))
        for key, why in picked:
            cur = conn.execute("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key,state,detail_json) VALUES(?,?,'proposed',?)",
                               (s["id"], key, json.dumps({"why": why[:300], "model": meta.get("model")})))
            stats["proposed"] += cur.rowcount
        prov["ai"] = {**meta, "prompt_version": PROMPT_VERSION, "candidates": len(cands), "proposed": [k for k, _ in picked]}
        title = re.sub(r"\s+", " ", out.title).strip()[:160] or s["title"]
        conn.execute("UPDATE scenario SET title=?, description=?, provenance_json=? WHERE id=?",
                     (title, out.description.strip()[:2000], json.dumps(prov), s["id"]))
        conn.commit()
        stats["described"] += 1
        eventlog.emit("info", "agent", f"{tag}: described as '{title[:80]}', {len(picked)} requirement link(s) proposed of {len(cands)} candidate(s)",
                      scenario_id=s["id"], proposed=[k for k, _ in picked])
    eventlog.emit("info" if not stats["failed"] else "warn", "agent", "Execution describer finished: " + ", ".join(f"{v} {k}" for k, v in stats.items()), **stats)
    return stats
