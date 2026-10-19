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

from . import eventlog
from .core.config import CATALOG_VERSION
from .core.db import rows
from .ingest.normalize import (
    classify_table,
    clean,
    compact_table,
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


def _mention_patterns(commands: dict[str, dict]) -> dict[str, re.Pattern]:
    """How a command is named in running text.  Multi-word names ('Get Log Page') are distinctive; a one-word name
    ('Read', 'Write', 'Identify') only counts when followed by 'command', so ordinary prose is not mistaken for a reference."""
    out = {}
    for k, c in commands.items():
        n = re.sub(r"\s+[Cc]ommand$", "", c["name"].strip())
        if len(n) >= 4:
            out[k] = re.compile(rf"\b{re.escape(n)}\b" + (r"(?:\s+[Cc]ommand)?" if " " in n else r"\s+[Cc]ommand\b"))
    return out


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


_RESPONSE_TABLE = re.compile(r"completion queue entry|\bresponse\b|status values?|\bCQE\b", re.I)
_HEX = re.compile(r"^[0-9A-Fa-f]{1,2}h(?:\s*(?:to|-)\s*[0-9A-Fa-f]{1,2}h)?$", re.I)
_NOT_A_NAME = re.compile(r"^(yes|no|n/a|note\b.*|notes?:.*|\d+(\.\d+)*)$", re.I)
_FOOTNOTE = re.compile(r"^\d{1,2}\s+(?=[A-Za-z])|(?<=[A-Za-z])\s+\d{1,2}$")


def _opcode_cols(rows: list[list[str]]) -> tuple[int, int] | None:
    """(opcode column, command-name column) of an opcode table, chosen by cell content: PDF opcode tables have multi-row
    headers, binary 'opcode by field' columns, footnote markers and merged cells, so header names are not reliable."""
    width = max((len(r) for r in rows), default=0)
    cell = lambda r, i: clean(r[i]) if i < len(r) else ""  # noqa: E731
    hexn = [sum(1 for r in rows if _HEX.match(cell(r, i))) for i in range(width)]
    if not hexn or max(hexn) < 1:
        return None
    oc = hexn.index(max(hexn))

    def names(i: int) -> int:
        return sum(1 for r in rows if _HEX.match(cell(r, oc)) and len(re.findall(r"[A-Za-z]", cell(r, i))) >= 3 and not _NOT_A_NAME.match(cell(r, i)))

    scores = [(names(i), i) for i in range(width) if i != oc]
    best = max(scores, default=(0, 0))
    return (oc, best[1]) if best[0] >= 1 else None


def _opcode_queue(ir: dict, t: dict) -> tuple[str, str, str] | None:
    """(interface, queue, command set) an opcode table belongs to, from its caption; None for tables that list no commands."""
    cap = norm_title(t["caption"])
    if "control primitive" in cap or "out-of-band" in cap or "pcie" in cap:
        return None
    if "admin" in cap:
        return "pcie", "admin", "base"
    if ir["family"] == "nvme-mi":
        return "mi", "mi", "mi"
    return "pcie", "io", CS_BY_FAMILY.get(ir["family"], "base")


def build_catalog(conn: sqlite3.Connection, set_id: int) -> dict[str, Any]:
    irs = load_set_irs(conn, set_id)
    for ir in irs:        # merged-cell padding from PDF extraction is removed once, before anything reads a table
        for t in ir["tables"].values():
            t["header"], t["rows"] = compact_table(t["header"], t["rows"])
    eventlog.emit("info", "catalog", "Building catalog from " + ", ".join(f"{i['family']} {i['version']}" for i in irs), set_id=set_id)
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
            cols = _opcode_cols(t["rows"])
            if cols is None or not (classify_table(t["header"], t["caption"]) == "opcode" or re.search(r"\bopcodes?\b", norm_title(t["caption"]))):
                continue
            ci, ni = cols
            oi = find_col(t["header"], "o/m", "mandatory")
            route = _opcode_queue(ir, t)
            if route is None:
                continue
            iface, queue, cs = route
            for r in t["rows"]:
                if ci >= len(r) or ni >= len(r) or not _HEX.match(clean(r[ci])):
                    continue
                rg = parse_value_range(r[ci])
                name = _FOOTNOTE.sub("", clean(r[ni]))
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
            if _RESPONSE_TABLE.search(t["caption"]):
                continue            # completion / response layouts are not part of what the host submits
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
        """(value range, {desc,row}) of an enumeration table.  A table the PDF split over pages (key 'x#2', 'x#3') is read whole,
        and the description is the wordiest column: merged cells make header positions unreliable."""
        base = t["key"].split("#")[0]
        parts = [p for i in irs for p in i["tables"].values() if p["key"].split("#")[0] == base] or [t]
        out = []
        for part in parts:
            valued = [r for r in part["rows"] if r and parse_value_range(r[0]) is not None]
            width = max((len(r) for r in valued), default=0)
            desc_i = max(range(1, width), key=lambda i: sum(len(clean(r[i])) if i < len(r) else 0 for r in valued), default=1)
            for r in valued:
                out.append((parse_value_range(r[0]), {"desc": clean(r[desc_i]) if desc_i < len(r) else "", "row": r}))
        return out

    def split_wide(fields: list[dict]) -> list[dict]:
        """A field wider than one dword (e.g. a 64-bit address in Command Dwords 10-11, written '63:00') is addressed per dword by
        predicates, so it becomes one field per dword, each with bit positions inside its own dword."""
        out = []
        for f in fields:
            t, dw = f["table"], f["dw"]
            spans = [parse_bit_range(r[0]) if r else None for r in t["rows"]]
            if not isinstance(dw, int) or not any(sp and sp[0] > 31 for sp in spans):
                out.append(f)
                continue
            for k in range(max(sp[0] for sp in spans if sp) // 32 + 1):
                part = []
                for r, sp in zip(t["rows"], spans):
                    if sp and sp[1] <= 32 * k + 31 and sp[0] >= 32 * k:
                        part.append([f"{min(sp[0], 32 * k + 31) - 32 * k:02d}:{max(sp[1], 32 * k) - 32 * k:02d}", *r[1:]])
                if part:
                    out.append({**f, "table": {**t, "rows": part}, "dw": dw + k})
        return out

    for c in commands.values():
        if c["interface"] == "mi":       # request dwords of a management-interface message are NMD0 / NMD1 (as decoded), not CDW numbers
            c["fields"] = [{**f, "dw": f"nmd{f['dw']}"} if isinstance(f["dw"], int) and f["dw"] in (0, 1) else f for f in c["fields"]]
        c["fields"] = split_wide(c["fields"])

    def fits(cand: dict, width: int) -> bool:
        """A table that merely happens to be cited in a field's description (an unrelated enumeration) is not the field's value
        list: most of its values would not fit the field."""
        vr = value_rows(cand)
        return bool(vr) and sum(1 for (_, hi), _m in vr if hi < (1 << width)) * 2 >= len(vr)

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
                        if cand and classify_table(cand["header"], cand["caption"]) == "values" and fits(cand, width):
                            vt = cand
                            break
                if vt is None and (abbr or name):
                    for cand in ir["tables"].values():
                        capn = norm_title(cand["caption"])
                        if classify_table(cand["header"], cand["caption"]) == "values" and fits(cand, width) and _cmd_norm(c["name"]) in capn \
                                and ((abbr and abbr.lower() in capn) or (name and norm_title(name) in capn)) and cand["label"] != t["label"]:
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
    mention_rx = _mention_patterns(commands)
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
            mentions = [k for k, rx in mention_rx.items() if k != (cmd["key"] if cmd else None) and rx.search(rq["text"])]
            if cmd and _STIMULUS_WORDS.search(rq["text"]):
                obs, basis = "stimulus", "heuristic: names an error/abort outcome"
            elif cmd and linked_fields:
                obs, basis = "stimulus", "heuristic: mentions a field of the command"
            elif mentions and (cmd or len(mentions) > 1):
                obs, basis = "stimulus", "heuristic: relates several commands"
            else:
                obs, basis = "unobservable", "heuristic: no command-level stimulus identified"
            add(_item(rq["key"], "requirement", cmd["key"] if cmd else None, rq["text"][:140], obs, text=rq["text"],
                      strength=rq["strength"], word=rq.get("word"), command=cmd["key"] if cmd else None, fields=linked_fields,
                      mentions=mentions, page=rq.get("page"), basis=basis, family=ir["family"], version=ir["version"]))

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
    by_sev: dict[str, int] = {}
    for it_ in issues:
        by_sev[it_["severity"]] = by_sev.get(it_["severity"], 0) + 1
    eventlog.emit("warn" if by_sev.get("error") else "info", "catalog", f"Catalog built: {len(items)} items "
                  f"({', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}), {len(commands)} commands, {len(issues)} issue(s)", set_id=set_id, kinds=kinds, issues=by_sev)
    for it_ in issues[:15]:
        eventlog.emit("warn", "catalog", it_["message"], set_id=set_id, key=it_.get("key"), kind=it_["kind"])
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
