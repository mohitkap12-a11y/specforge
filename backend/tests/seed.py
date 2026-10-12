"""Test-only seeding of single-command scenarios, so coverage-engine tests have approved scenarios to match.

The product does not generate these (its scenarios are cross-command, see specforge.crossgen); the matcher still has to
handle single-step predicates, and this builds them from the catalog.
"""
from __future__ import annotations

import json
import sqlite3

from specforge.core.db import jloads, rows



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
                    json.dumps({"template_version": "1", "item": item_key})))
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
        elif k == "workflow":
            put(f"T:{i['key']}", "workflow", i["label"], i["observability"], a["predicate"], i["key"], a.get("description", ""))
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


