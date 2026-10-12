"""L3: the coverage denominator. Built deterministically from IR + resolved references.

Item kinds
  command     one per opcode-table row                      (direct scenarios)
  field       one per described bit range of a command dword (aggregate of its children)
  value       one per enumerated value / implicit flag value (direct scenarios)
  reserved    one per reserved bit range                     (direct, 'stimulus')
  boundary    min / max of an un-enumerated numeric field    (direct)
  mi_tunnel   admin command carried in an NVMe-MI message    (direct)
  requirement one per normative statement                    (linked scenarios; 'unobservable' unless linked)
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .core.config import CATALOG_VERSION
from .core.db import rows
from .ingest.normalize import (
    classify_table,
    clean,
    extract_references,
    find_col,
    norm_title,
    parse_bit_range,
    parse_value_range,
    slug,
)
from .refs import load_set_irs

CS_BY_FAMILY = {"nvme-nvm-cs": "nvm", "nvme-zns-cs": "zns", "nvme-kv-cs": "kv", "nvme-slm-cs": "slm", "nvme-base": "base"}
_CAP_CMD = re.compile(r"^(?P<name>.+?)\s*-\s*(?:command|request)\s+dword\s+(?P<dw>\d+)(?:\s*\((?P<tok>[^)]*)\))?", re.I)
_CAP_NMD = re.compile(r"^(?P<name>.+?)\s*-\s*.*\bNMD(?P<n>[01])\b", re.I)
_FIELD_DESC = re.compile(r"^(?P<name>[^:(]+?)(?:\s*\((?P<abbr>[A-Za-z0-9_/ -]{1,14})\))?\s*:\s*(?P<rest>.*)$", re.S)
_STIMULUS_WORDS = re.compile(r"\b(abort|status|error|invalid|fail|reject|return(?:s|ed)? (?:an )?error)\b", re.I)


def _dash(s: str) -> str:
    return norm_title(s)


def _cmd_norm(name: str) -> str:
    n = norm_title(name)
    n = re.sub(r"\s+command$", "", n)
    return n


def _item(key: str, kind: str, parent: str | None, label: str, obs: str, **attrs: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "parent_key": parent, "label": label, "observability": obs, "attrs": attrs}


def _split_field(desc: str) -> tuple[str, str, str]:
    d = clean(desc)
    m = _FIELD_DESC.match(d)
    if m and len(m.group("name")) <= 80:
        return clean(m.group("name")), clean(m.group("abbr") or ""), clean(m.group("rest"))
    return "", "", d


def _is_reserved(desc: str) -> bool:
    return bool(re.match(r"^\s*(reserved|rsvd)\b", desc, re.I))


def _opcode_queue(ir: dict, t: dict) -> tuple[str, str, str]:
    cap = norm_title(t["caption"])
    fam = ir["family"]
    if fam == "nvme-mi":
        return "mi", "mi", "mi"
    if "admin" in cap:
        return "pcie", "admin", "base"
    return "pcie", "io", CS_BY_FAMILY.get(fam, "base")


def build_catalog(conn: sqlite3.Connection, set_id: int) -> dict[str, Any]:
    irs = load_set_irs(conn, set_id)
    items: dict[str, dict] = {}
    issues: list[dict] = []

    def add(it: dict) -> None:
        if it["key"] in items:
            issues.append({"severity": "warning", "kind": "duplicate_item", "key": it["key"], "message": f"Duplicate catalog key {it['key']}"})
            return
        items[it["key"]] = it

    # ---- commands from opcode tables
    commands: dict[str, dict] = {}
    for ir in irs:
        for t in ir["tables"].values():
            if classify_table(t["header"], t["caption"]) != "opcode":
                continue
            ci = find_col(t["header"], "opcode")
            ni = find_col(t["header"], "command")
            oi = find_col(t["header"], "o/m", "mandatory")
            iface, queue, cs = _opcode_queue(ir, t)
            for r in t["rows"]:
                if ci is None or ni is None or ci >= len(r) or ni >= len(r):
                    continue
                rg = parse_value_range(r[ci])
                name = clean(r[ni])
                if not rg or not name or _is_reserved(name):
                    continue
                if re.match(r"^(reserved|vendor)", norm_title(name)):
                    continue            # reserved / vendor-specific ranges are not catalogued commands (unknown opcodes are reported)
                for opc in range(rg[0], min(rg[1], rg[0] + 255) + 1):
                    if iface == "mi":
                        key = f"cmd:mi:{opc:02X}"
                    elif queue == "admin":
                        key = f"cmd:admin:{opc:02X}"
                    else:
                        key = f"cmd:{queue}:{cs}:{opc:02X}"
                    if key in commands:
                        issues.append({"severity": "warning", "kind": "duplicate_command", "key": key,
                                       "message": f"{key} appears in more than one opcode table"})
                        continue
                    commands[key] = {"key": key, "name": name, "opcode": opc, "interface": iface, "queue": queue, "cs": cs,
                                     "family": ir["family"], "version": ir["version"], "om": clean(r[oi]) if oi is not None and oi < len(r) else "",
                                     "table": t["key"], "ir": ir, "section": None, "fields": []}
    # ---- command -> section
    for c in commands.values():
        ir = c["ir"]
        want = _cmd_norm(c["name"])
        cands = [s for s in ir["sections"].values() if _cmd_norm(s["title"]) == want]
        if not cands:
            cands = [s for s in ir["sections"].values() if _cmd_norm(s["title"]).startswith(want) and want]
        if cands:
            c["section"] = cands[0]
        else:
            issues.append({"severity": "warning", "kind": "command_without_section", "key": c["key"],
                           "message": f"No section found for command '{c['name']}' ({c['key']})"})

    # ---- field tables -> command
    by_name: dict[str, list[dict]] = {}
    for c in commands.values():
        by_name.setdefault(_cmd_norm(c["name"]), []).append(c)

    def subtree(ir: dict, number: str) -> set[str]:
        out = {number}
        changed = True
        while changed:
            changed = False
            for s in ir["sections"].values():
                if s["parent"] in out and s["number"] not in out:
                    out.add(s["number"])
                    changed = True
        return out

    for ir in irs:
        for t in ir["tables"].values():
            if classify_table(t["header"], t["caption"]) != "cdw":
                continue
            cap = _dash(t["caption"]).replace(" - ", " - ")
            dw: str | int | None = None
            name = None
            m = _CAP_NMD.match(cap)
            if m:
                name, dw = m.group("name"), f"nmd{m.group('n')}"
            else:
                m = _CAP_CMD.match(cap)
                if m:
                    name, dw = m.group("name"), int(m.group("dw"))
            cmd = None
            if name is not None:
                cands = by_name.get(_cmd_norm(name), [])
                same = [c for c in cands if c["ir"] is ir]
                if not same:    # defined in another spec: only when it is unambiguous, never across revisions of one family
                    same = [c for c in cands if c["ir"]["family"] != ir["family"]]
                    same = same if len(same) == 1 else []
                if len(same) > 1:
                    # one opcode-table row can stand for a whole opcode range (e.g. 11h to 14h): every command gets the table
                    for other in same:
                        other["fields"].append({"table": t, "dw": dw, "ir": ir})
                    continue
                if same:
                    cmd = same[0]
            if cmd is None:
                # section ancestry fallback
                for c in commands.values():
                    if c["ir"] is ir and c["section"] and t.get("section") in subtree(ir, c["section"]["number"]):
                        cmd = c
                        break
                if cmd is not None and dw is None:
                    mm = re.search(r"dword\s+(\d+)", cap)
                    dw = int(mm.group(1)) if mm else None
            if cmd is None or dw is None:
                issues.append({"severity": "warning", "kind": "unlinked_field_table", "key": t["key"],
                               "message": f"Field table '{t['caption']}' could not be linked to a command"})
                continue
            cmd["fields"].append({"table": t, "dw": dw, "ir": ir})

    # ---- helpers for value tables
    choices = {(r["src_key"], r["raw"]): r["chosen_key"] for r in rows(conn, "SELECT * FROM ref_choice WHERE set_id=?", (set_id,))}

    def find_table(ir: dict, label: str, family: str | None, src_key: str = "", raw: str = "") -> dict | None:
        if family and family != ir["family"]:
            cands = [o["tables"][label] for o in irs if o["family"] == family and label in o["tables"]]
            if len(cands) > 1:     # several revisions of the family: only the user's choice may decide, never "first wins"
                picked = [t for t in cands if t["key"] == choices.get((src_key, raw))]
                if not picked:
                    issues.append({"severity": "warning", "kind": "ambiguous_value_table", "key": src_key,
                                   "message": f"'{raw}' matches {len(cands)} revisions of {family}; resolve the reference to use its value table"})
                    return None
                return picked[0]
            return cands[0] if cands else None
        return ir["tables"].get(label)

    def value_rows(t: dict) -> list[tuple[tuple[int, int], dict]]:
        out = []
        desc_i = find_col(t["header"], "description")        # header text is matched case-insensitively
        if desc_i is None:
            desc_i = 2 if len(t["header"]) > 2 and find_col(t["header"], "o/m") is not None else 1
        for r in t["rows"]:
            if not r:
                continue
            rg = parse_value_range(r[0])
            if rg is None:
                continue
            out.append((rg, {"desc": clean(r[desc_i]) if desc_i < len(r) else "", "row": r}))
        return out

    # ---- commands, fields, values
    for c in commands.values():
        cmd_item = _item(c["key"], "command", None, c["name"], "submission", name=c["name"], opcode=c["opcode"],
                         interface=c["interface"], queue=c["queue"], cs=c["cs"], family=c["family"], version=c["version"],
                         om=c["om"], section=c["section"]["key"] if c["section"] else None, table=c["table"],
                         tables=[f["table"]["key"] for f in c["fields"]])
        add(cmd_item)
        for f in sorted(c["fields"], key=lambda f: (str(type(f["dw"])), f["dw"])):
            t, ir, dw = f["table"], f["ir"], f["dw"]
            for r in t["rows"]:
                if len(r) < 2:
                    continue
                rg = parse_bit_range(r[0])
                if rg is None:
                    continue
                hi, lo = rg
                width = hi - lo + 1
                name, abbr, rest = _split_field(r[1])
                reserved = _is_reserved(r[1])
                dwtag = f"dw{dw}" if isinstance(dw, int) else dw
                fkey = f"{c['key']}/{dwtag}[{hi:02d}:{lo:02d}]"
                if reserved:
                    add(_item(fkey + ":rsvd", "reserved", c["key"], f"{dwtag}[{hi}:{lo}] reserved", "stimulus", dw=dw, hi=hi, lo=lo,
                              width=width, table=t["key"], command=c["key"]))
                    continue
                label = f"{name} ({abbr})" if abbr else (name or f"{dwtag}[{hi}:{lo}]")
                fattrs = dict(dw=dw, hi=hi, lo=lo, width=width, name=name or f"{dwtag}[{hi}:{lo}]", abbr=abbr, desc=clean(r[1]),
                              table=t["key"], command=c["key"], slug=slug(abbr or name or f"{dwtag}_{hi}_{lo}"))
                # value table
                vt = None
                for ref in extract_references(r[1], ir["family"]):
                    if ref.kind == "figure":
                        cand = find_table(ir, ref.label, ref.family, t["key"], ref.raw)
                        if cand and classify_table(cand["header"], cand["caption"]) == "values" and value_rows(cand):
                            vt = cand
                            break
                if vt is None and abbr:
                    for cand in ir["tables"].values():
                        capn = norm_title(cand["caption"])
                        if classify_table(cand["header"], cand["caption"]) == "values" and _cmd_norm(c["name"]) in capn \
                                and abbr.lower() in capn and cand["label"] != t["label"]:
                            vt = cand
                            break
                fattrs["value_table"] = vt["key"] if vt else None
                add(_item(fkey, "field", c["key"], label, "submission", **fattrs))
                if vt is not None:
                    for (vlo, vhi), meta in value_rows(vt):
                        if vhi >= (1 << width):
                            issues.append({"severity": "warning", "kind": "value_exceeds_width", "key": fkey,
                                           "message": f"Value {vhi:#x} does not fit in {width} bits ({vt['key']})"})
                            continue
                        res = _is_reserved(meta["desc"])
                        vkey = f"{fkey}={vlo:02X}h" + (f"-{vhi:02X}h" if vhi != vlo else "")
                        add(_item(vkey, "value", fkey, f"{label} = {vlo:#04x}" + (f"..{vhi:#04x}" if vhi != vlo else "") +
                                  f" {meta['desc'][:60]}", "stimulus" if res else "submission", dw=dw, hi=hi, lo=lo, vlo=vlo, vhi=vhi,
                                  reserved_value=res, desc=meta["desc"], table=vt["key"], command=c["key"], field=fkey))
                elif width == 1:
                    for v in (0, 1):
                        add(_item(f"{fkey}={v:02X}h", "value", fkey, f"{label} = {v}", "submission", dw=dw, hi=hi, lo=lo, vlo=v, vhi=v,
                                  implicit=True, reserved_value=False, desc="cleared" if v == 0 else "set", table=t["key"],
                                  command=c["key"], field=fkey))
                else:
                    top = (1 << width) - 1
                    for tag, v in (("min", 0), ("max", top)):
                        add(_item(f"{fkey}={tag}", "boundary", fkey, f"{label} {tag} ({v:#x})", "submission", dw=dw, hi=hi, lo=lo,
                                  vlo=v, vhi=v, boundary=tag, width_based=True, table=t["key"], command=c["key"], field=fkey))
        if not c["fields"]:
            issues.append({"severity": "info", "kind": "command_without_field_tables", "key": c["key"],
                           "message": f"No command dword tables linked to {c['name']} ({c['key']})"})

    # ---- MI tunnelled admin commands
    if any(ir["family"] == "nvme-mi" for ir in irs):
        for c in list(commands.values()):
            if c["queue"] == "admin":
                add(_item(f"{c['key']}/mi-tunnel", "mi_tunnel", c["key"], f"{c['name']} over NVMe-MI", "submission",
                          opcode=c["opcode"], command=c["key"]))

    # ---- requirements
    cmd_by_section: dict[tuple[str, str], dict] = {}
    for c in commands.values():
        if c["section"]:
            for n in subtree(c["ir"], c["section"]["number"]):
                cmd_by_section.setdefault((c["ir"]["prefix"], n), c)
    field_items = [i for i in items.values() if i["kind"] == "field"]
    for ir in irs:
        for rq in ir["requirements"]:
            cmd = None
            if rq.get("table"):
                tkey = f"{ir['prefix']}/fig:{rq['table']}"
                cmd = next((c for c in commands.values() if any(f["table"]["key"] == tkey for f in c["fields"])), None)
            if cmd is None and rq.get("section"):
                cmd = cmd_by_section.get((ir["prefix"], rq["section"]))
            linked_fields = []
            if cmd:
                for fi in field_items:
                    a = fi["attrs"]
                    if a["command"] == cmd["key"] and a["abbr"] and re.search(rf"\b{re.escape(a['abbr'])}\b", rq["text"]):
                        linked_fields.append(fi["key"])
                    elif rq.get("table") and a["table"] == f"{ir['prefix']}/fig:{rq['table']}" and rq["text"] in a["desc"]:
                        linked_fields.append(fi["key"])
            if cmd and _STIMULUS_WORDS.search(rq["text"]):
                obs, basis = "stimulus", "heuristic: names an error/abort outcome"
            elif cmd and linked_fields:
                obs, basis = "stimulus", "heuristic: mentions a field of the command"
            else:
                obs, basis = "unobservable", "heuristic: no command-level stimulus identified"
            add(_item(rq["key"], "requirement", cmd["key"] if cmd else None, rq["text"][:140], obs, text=rq["text"],
                      strength=rq["strength"], word=rq.get("word"), command=cmd["key"] if cmd else None, fields=linked_fields,
                      page=rq.get("page"), basis=basis, family=ir["family"], version=ir["version"]))

    # ---- persist (preserving reviewer overrides)
    over = {r["key"]: r for r in rows(conn, "SELECT * FROM item_override WHERE set_id=?", (set_id,))}
    conn.execute("DELETE FROM catalog_item WHERE set_id=?", (set_id,))
    out = []
    for it in items.values():
        o = over.get(it["key"])
        obs = (o["observability"] if o and o["observability"] else it["observability"])
        out.append((set_id, it["key"], it["kind"], it["parent_key"], it["label"], obs, o["na_reason"] if o else None,
                    json.dumps(it["attrs"])))
    conn.executemany("INSERT INTO catalog_item(set_id,key,kind,parent_key,label,observability,na_reason,attrs_json) VALUES(?,?,?,?,?,?,?,?)", out)
    conn.commit()
    kinds: dict[str, int] = {}
    for it in items.values():
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    return {"version": CATALOG_VERSION, "items": len(items), "kinds": kinds, "issues": issues,
            "commands": sorted(commands), "documents": [f"{i['prefix']}" for i in irs]}


def set_override(conn: sqlite3.Connection, set_id: int, key: str, observability: str | None, na_reason: str | None) -> None:
    if observability is None and na_reason is None:      # clearing an override: regenerate so the generated value comes back
        conn.execute("DELETE FROM item_override WHERE set_id=? AND key=?", (set_id, key))
        conn.commit()
        build_catalog(conn, set_id)
        return
    conn.execute("INSERT INTO item_override(set_id,key,observability,na_reason) VALUES(?,?,?,?) "
                 "ON CONFLICT(set_id,key) DO UPDATE SET observability=excluded.observability, na_reason=excluded.na_reason",
                 (set_id, key, observability, na_reason))
    conn.execute("UPDATE catalog_item SET observability=COALESCE(?,observability), na_reason=? WHERE set_id=? AND key=?",
                 (observability, na_reason, set_id, key))
    conn.commit()
