"""Scenario predicate DSL, validator, and deterministic template generators.

Predicate (JSON):
  {"scope": "global|test|script", "order": "strict|any", "within": null | N,
   "steps": [{"command": "cmd:admin:06",            # catalog key (traceability)
              "interface": "pcie|mi", "queue": "admin|io|mi", "cs": "nvm"|null, "opcode": 6,
              "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 1}],
              "capture": {"fid": {"dw": 10, "hi": 7, "lo": 0}}}]}
Operators: eq, ne, in, range{lo,hi}, mask{mask,value}, absent.  A field value may be {"var": "fid"}.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .core.config import TEMPLATE_VERSION
from .core.db import jloads, rows

OPS = {"eq", "ne", "in", "range", "mask", "absent"}
INTERFACES = {"pcie", "mi"}
QUEUES = {"admin", "io", "mi"}
SCOPES = {"global", "test", "script"}


class PredicateError(ValueError):
    pass


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _dw_ok(dw: Any) -> bool:
    return (_is_int(dw) and 0 <= dw <= 15) or (isinstance(dw, str) and dw in ("nmd0", "nmd1"))


def _in(v: Any, allowed: set) -> bool:
    return isinstance(v, str) and v in allowed          # scalar check first: lists / dicts are unhashable


def _not_preserved(step: dict, dw: Any, hi: int) -> bool:
    """PCIe commands are de-duplicated on a signature that ignores CID (dw0[31:16]), MPTR and data pointers (dw4-9)."""
    return step.get("interface") == "pcie" and (dw in range(4, 10) or (dw == 0 and hi >= 16))


def _routes(cmd: dict) -> set[tuple[str, str]]:
    """(interface, queue) pairs under which a catalog command can legitimately appear in the log."""
    if cmd.get("interface") == "mi":
        return {("mi", "mi")}
    if cmd.get("queue") == "admin":
        return {("pcie", "admin"), ("mi", "admin")}       # an Admin command may also be tunnelled in an NVMe-MI message
    return {("pcie", cmd.get("queue", "io"))}


def validate_predicate(pred: dict, commands: dict[str, dict] | None = None, fields: dict[str, list[dict]] | None = None) -> list[str]:
    """Return a list of problems (empty == valid). Never raises on malformed input.

    commands: catalog command attrs by key; fields: described fields (attrs) by command key.  When given, the
    step's opcode / queue / command set must match the catalog command and each field must lie inside a described bit range.
    """
    errs: list[str] = []
    if not isinstance(pred, dict):
        return ["predicate must be an object"]
    steps = pred.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["'steps' must be a non-empty list"]
    if not _in(pred.get("scope", "global"), SCOPES):
        errs.append(f"scope must be one of {sorted(SCOPES)}")
    if not _in(pred.get("order", "strict"), {"strict", "any"}):
        errs.append("order must be 'strict' or 'any'")
    w = pred.get("within")
    if w is not None and not (_is_int(w) and w > 0):
        errs.append("'within' must be a positive integer")

    def bitrange(q: str, o: dict) -> bool:
        hi, lo = o.get("hi"), o.get("lo")
        if not (_is_int(hi) and _is_int(lo) and 0 <= lo <= hi <= 31):
            errs.append(f"{q}: need integers 0 <= lo <= hi <= 31")
            return False
        return True

    caps: set[str] = set()
    for i, st in enumerate(steps):
        p = f"steps[{i}]"
        if not isinstance(st, dict):
            errs.append(f"{p} must be an object")
            continue
        if not _in(st.get("interface"), INTERFACES):
            errs.append(f"{p}.interface must be one of {sorted(INTERFACES)}")
        if not _in(st.get("queue"), QUEUES):
            errs.append(f"{p}.queue must be one of {sorted(QUEUES)}")
        if not _is_int(st.get("opcode")) or not 0 <= st["opcode"] <= 0xFF:
            errs.append(f"{p}.opcode must be an integer 0..255")
        cmd = None
        if commands is not None and isinstance(st.get("command"), str):
            cmd = commands.get(st["command"])
            if cmd is None:
                errs.append(f"{p}.command '{st['command']}' is not in the catalog")
            else:
                if cmd.get("opcode") != st.get("opcode"):
                    errs.append(f"{p}.opcode {st.get('opcode')} does not match catalog opcode {cmd.get('opcode')} for {st['command']}")
                if (st.get("interface"), st.get("queue")) not in _routes(cmd):
                    errs.append(f"{p}: interface/queue {st.get('interface')}/{st.get('queue')} is not a valid route for {st['command']}")
                if cmd.get("queue") == "io" and st.get("cs") != cmd.get("cs"):
                    errs.append(f"{p}.cs must be '{cmd.get('cs')}' for {st['command']}")
        flist = st.get("fields", [])
        if not isinstance(flist, list):
            errs.append(f"{p}.fields must be a list")
            flist = []
        for j, f in enumerate(flist):
            q = f"{p}.fields[{j}]"
            if not isinstance(f, dict):
                errs.append(f"{q} must be an object")
                continue
            if not _dw_ok(f.get("dw")):
                errs.append(f"{q}.dw must be 0..15 or 'nmd0'/'nmd1'")
            if not bitrange(q, f):
                continue
            hi, lo = f["hi"], f["lo"]
            if _not_preserved(st, f.get("dw"), hi):
                errs.append(f"{q}: {f.get('dw')}[{hi}:{lo}] (CID, MPTR, data pointers) is not preserved when commands are de-duplicated and cannot be matched")
                continue
            op = f.get("op", "eq")
            if not _in(op, OPS):
                errs.append(f"{q}.op {op!r} not in {sorted(OPS)}")
                continue
            width = hi - lo + 1
            val = f.get("value")
            if isinstance(val, dict) and "var" in val:
                if op not in ("eq", "ne"):
                    errs.append(f"{q}: a captured variable can only be used with 'eq' or 'ne' (captures are single integers)")
                elif not isinstance(val["var"], str) or val["var"] not in caps:
                    errs.append(f"{q}: variable {val['var']!r} is not captured by an earlier step")
            elif op in ("eq", "ne"):
                if not _is_int(val) or not 0 <= val < (1 << width):
                    errs.append(f"{q}.value {val!r} does not fit {width} bits")
            elif op == "in":
                if not isinstance(val, list) or not val or any(not _is_int(v) or not 0 <= v < (1 << width) for v in val):
                    errs.append(f"{q}.value must be a list of integers fitting {width} bits")
            elif op == "range":
                if not (isinstance(val, dict) and _is_int(val.get("lo")) and _is_int(val.get("hi")) and 0 <= val["lo"] <= val["hi"] < (1 << width)):
                    errs.append(f"{q}.value must be {{lo,hi}} within {width} bits")
            elif op == "mask":
                if not (isinstance(val, dict) and _is_int(val.get("mask")) and _is_int(val.get("value"))):
                    errs.append(f"{q}.value must be {{mask,value}}")
            if fields is not None and cmd is not None and not errs:
                ok = any(a["dw"] == f["dw"] and a["lo"] <= lo and hi <= a["hi"] for a in fields.get(st["command"], []))
                if not ok:
                    errs.append(f"{q}: {f['dw']}[{hi}:{lo}] is not inside any field described for {st['command']}")
        capture = st.get("capture") or {}
        if not isinstance(capture, dict):
            errs.append(f"{p}.capture must be an object")
            capture = {}
        for name, c in capture.items():
            q = f"{p}.capture.{name}"
            if not isinstance(c, dict) or not _dw_ok(c.get("dw")):
                errs.append(f"{q} needs dw/hi/lo")
                continue
            if not bitrange(q, c):
                continue
            if _not_preserved(st, c.get("dw"), c["hi"]):
                errs.append(f"{q}: {c.get('dw')}[{c['hi']}:{c['lo']}] (CID, MPTR, data pointers) is not preserved when commands are de-duplicated and cannot be captured")
                continue
            caps.add(str(name))
    return errs


# ----------------------------------------------------------------------------- templates

def _step(cmd: dict, fields: list[dict] | None = None) -> dict:
    a = cmd
    return {"command": a["key"], "interface": a["interface"], "queue": a["queue"],
            "cs": a["cs"] if a["queue"] == "io" else None, "opcode": a["opcode"], "fields": fields or []}


def _fmt_cmd(a: dict) -> str:
    where = {"admin": "Admin", "io": "I/O", "mi": "MI"}.get(a["queue"], a["queue"])
    return f"{a['name']} ({where} {a['opcode']:02X}h)"


def generate_templates(conn: sqlite3.Connection, set_id: int) -> dict[str, int]:
    items = rows(conn, "SELECT * FROM catalog_item WHERE set_id=?", (set_id,))
    for i in items:
        i["attrs"] = jloads(i.pop("attrs_json"), {})
    cmds = {i["key"]: {**i["attrs"], "key": i["key"]} for i in items if i["kind"] == "command"}
    out: list[tuple] = []
    links: list[tuple[str, str]] = []

    def put(sid: str, kind: str, title: str, obs: str, pred: dict, item_key: str, desc: str = "") -> None:
        out.append((set_id, sid, "template", kind, title, desc, obs, json.dumps(pred), "approved",
                    json.dumps({"template_version": TEMPLATE_VERSION, "item": item_key})))
        links.append((sid, item_key))

    for i in items:
        a, k = i["attrs"], i["kind"]
        if k == "command":
            put(f"T:{i['key']}", "command", f"Issue {_fmt_cmd(a)}", i["observability"], {"scope": "global", "order": "strict", "steps": [_step(cmds[i["key"]])]}, i["key"],
                f"At least one {a['name']} command is submitted.")
        elif k in ("value", "boundary", "reserved"):
            cmd = cmds.get(a["command"])
            if cmd is None:
                continue
            if k == "reserved":
                cond = {"dw": a["dw"], "hi": a["hi"], "lo": a["lo"], "op": "ne", "value": 0}
                title = f"{_fmt_cmd(cmd)}: reserved {a['dw']}[{a['hi']}:{a['lo']}] set to a non-zero value"
            else:
                if a["vlo"] == a["vhi"]:
                    cond = {"dw": a["dw"], "hi": a["hi"], "lo": a["lo"], "op": "eq", "value": a["vlo"]}
                else:
                    cond = {"dw": a["dw"], "hi": a["hi"], "lo": a["lo"], "op": "range", "value": {"lo": a["vlo"], "hi": a["vhi"]}}
                title = f"{_fmt_cmd(cmd)}: {i['label']}"
            put(f"T:{i['key']}", k, title, i["observability"], {"scope": "global", "order": "strict", "steps": [_step(cmd, [cond])]}, i["key"])
        elif k == "mi_tunnel":
            cmd = cmds.get(a["command"])
            if cmd is None:
                continue
            step = {"command": cmd["key"], "interface": "mi", "queue": "admin", "cs": None, "opcode": cmd["opcode"], "fields": []}
            put(f"T:{i['key']}", "mi_tunnel", f"{cmd['name']} carried in an NVMe-MI Admin message", "submission",
                {"scope": "global", "order": "strict", "steps": [step]}, i["key"])
    # Upsert by sid: ids and reviewer decisions (state) of existing template scenarios are preserved, so
    # old coverage results keep pointing at the same scenarios; templates that no longer apply are removed.
    existing = {r["sid"]: r["id"] for r in rows(conn, "SELECT id,sid FROM scenario WHERE set_id=? AND origin='template'", (set_id,))}
    keep = {o[1] for o in out}
    for sid_old, id_old in existing.items():
        if sid_old not in keep:
            conn.execute("DELETE FROM scenario WHERE id=?", (id_old,))
    for o in out:
        if o[1] in existing:
            conn.execute("UPDATE scenario SET kind=?, title=?, description=?, observability=?, predicate_json=?, provenance_json=? WHERE id=?",
                         (o[3], o[4], o[5], o[6], o[7], o[9], existing[o[1]]))
        else:
            conn.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,description,observability,predicate_json,state,provenance_json)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", o)
    ids = {r["sid"]: r["id"] for r in rows(conn, "SELECT id,sid FROM scenario WHERE set_id=?", (set_id,))}
    conn.execute("DELETE FROM scenario_item WHERE scenario_id IN (SELECT id FROM scenario WHERE set_id=? AND origin='template')", (set_id,))
    conn.executemany("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key) VALUES(?,?)", [(ids[s_], k) for s_, k in links if s_ in ids])
    conn.commit()
    kinds: dict[str, int] = {}
    for o in out:
        kinds[o[3]] = kinds.get(o[3], 0) + 1
    return {"generated": len(out), **kinds}


def catalog_context(conn: sqlite3.Connection, set_id: int) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Command attrs and described-field attrs, used by validate_predicate."""
    cmds: dict[str, dict] = {}
    fields: dict[str, list[dict]] = {}
    for r in rows(conn, "SELECT key,kind,attrs_json FROM catalog_item WHERE set_id=? AND kind IN ('command','field','reserved')", (set_id,)):
        a = json.loads(r["attrs_json"])
        if r["kind"] == "command":
            cmds[r["key"]] = {**a, "key": r["key"]}
        else:
            fields.setdefault(a["command"], []).append(a)
    return cmds, fields
