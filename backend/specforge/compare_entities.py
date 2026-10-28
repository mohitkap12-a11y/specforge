"""Commands and data structures affected by a spec comparison.

The change list of compare.py is per section and per table. Engineers also want it per *thing*: which commands are new, which
existing commands changed, which data structures are new and which changed. This module derives those four lists (plus the
commands and structures that were removed) deterministically from the two parsed versions and the change list:

  commands     named by the opcode tables and by sections titled "... command"; a command owns its section subtree and the
               tables captioned "<Command> - Command Dword N" (field layouts, value tables such as CNS or Feature Identifiers)
  structures   byte-layout tables, and tables / sections titled as a data structure, log page, descriptor, entry, header ...

Each row lists the changes it is built from (their ids in the change list), so the tables stay traceable to the evidence.
compare_ai.py may add a one-line description per row; the rows themselves never depend on the model.
"""
from __future__ import annotations

import re
from typing import Any

from .catalog import _CAP_CMD, _FOOTNOTE, _HEX, _cmd_norm, _is_reserved, _opcode_cols, _opcode_queue
from .ingest.normalize import classify_table, clean, compact_table, norm_title, parse_value_range

GROUPS = ("commands_added", "commands_modified", "commands_removed", "structures_added", "structures_modified", "structures_removed")
MAX_ROWS = 300          # rows kept per group (the count is always complete)
MAX_REFS = 12           # changes listed per row
_CAPTION_LABEL = re.compile(r"^(?:figure|table|fig\.?)\s*[\w.\-]+\s*[:.–—-]?\s*", re.I)
_CMD_SECTION = re.compile(r"^(?P<name>.+?)\s+command$", re.I)
_STRUCT = re.compile(r"\b(data structures?|log pages?|descriptors?|entry|entries|headers?|payloads?|records?|data buffers?|list format|"
                     r"(?:message|response|request) format|capsule)\b", re.I)
_IMPACT_ORDER = ("breaking", "new", "changed", "clarification", "editorial")


def _name_of(caption: str) -> str:
    return clean(_CAPTION_LABEL.sub("", (caption or "").strip()))


def _key(name: str) -> str:
    return re.sub(r"[\W_]+", " ", (name or "").lower()).strip()


def _ck(name: str) -> str:
    """Command identity: its name without 'command', case and punctuation ('Namespace Mgmt.' is 'Namespace Mgmt')."""
    return _key(_cmd_norm(name))


def _route_label(iface: str, queue: str, cs: str) -> str:
    if iface == "mi":
        return "MI"
    return "Admin" if queue == "admin" else ("I/O" if cs in ("base", "nvm") else f"I/O ({cs.upper()})")


# ----------------------------------------------------------------------------- what each version contains

class _Version:
    """Commands and structures of one parsed version, and which command or structure each section and table belongs to."""

    def __init__(self, ir: dict):
        self.ir = ir
        self.cmds: dict[str, dict] = {}             # normalised command name -> {name, opcode, route}
        self.by_opcode: dict[tuple, str] = {}       # (route, opcode) -> normalised name
        self.opcode_tables: dict[str, tuple[int, int, str]] = {}   # table label -> (opcode col, name col, route label)
        self.opcode_rows: dict[tuple, dict] = {}    # (route, opcode) -> {name, table, header, cells}: every command row, compacted
        self.structs: dict[str, str] = {}           # key -> display name
        for label, t in ir["tables"].items():
            if not self._is_opcode_table(t):
                continue
            header, data = compact_table(t.get("header") or [], t.get("rows") or [])
            cols = _opcode_cols(data, header)
            route = _opcode_queue(ir, t)
            if cols is None or route is None:
                continue
            oc, nc = cols
            rl = _route_label(*route)
            self.opcode_tables[label] = (oc, nc, rl)
            for r in data:
                name, opc = self._opcode_row(r, oc, nc)
                if name:
                    k = _ck(name)
                    self.cmds.setdefault(k, {"name": name, "opcode": f"{rl} {opc}", "route": rl})
                    self.by_opcode[(rl, opc)] = k
                    self.opcode_rows.setdefault((rl, opc), {"name": name, "table": label, "header": header, "cells": [clean(x) for x in r],
                                                            "cols": (oc, nc)})
        self.cmd_section: dict[str, str] = {}       # section number -> normalised command name that section is about
        for n, s in ir["sections"].items():
            m = _CMD_SECTION.match(clean(s["title"]))
            if m and not re.search(r"\b(?:command\s+sets?|overview)\b", m.group("name"), re.I):
                k = _ck(s["title"])
                self.cmds.setdefault(k, {"name": clean(m.group("name")), "opcode": "", "route": ""})
                self.cmds[k].setdefault("section", n)
                self.cmd_section[n] = k
        for n, s in ir["sections"].items():
            if n not in self.cmd_section and self.is_struct_section(n):
                self.structs[_key(s["title"])] = clean(s["title"])
        for t in ir["tables"].values():
            if self.is_struct_table(t):
                self.structs[_key(_name_of(t.get("caption")))] = _name_of(t.get("caption"))

    @staticmethod
    def _is_opcode_table(t: dict) -> bool:
        return classify_table(t.get("header") or [], t.get("caption") or "") == "opcode" or bool(re.search(r"\bopcodes?\b", norm_title(t.get("caption") or "")))

    @staticmethod
    def _opcode_row(r: list, oc: int, nc: int) -> tuple[str, str]:
        if oc >= len(r) or nc >= len(r) or not _HEX.match(clean(r[oc])):
            return "", ""
        name = _FOOTNOTE.sub("", clean(r[nc]))
        rg = parse_value_range(r[oc])
        if not rg or not name or _is_reserved(name) or re.match(r"^(reserved|vendor)", norm_title(name)):
            return "", ""
        return name, (f"{rg[0]:02X}h" if rg[0] == rg[1] else f"{rg[0]:02X}h–{rg[1]:02X}h")

    def command_of_section(self, number: str | None) -> str | None:
        n, guard = number, 0
        while n and guard < 24:
            if n in self.cmd_section:
                return self.cmd_section[n]
            s = self.ir["sections"].get(n)
            n, guard = (s["parent"] if s else None), guard + 1
        return None

    def is_struct_section(self, number: str) -> bool:
        s = self.ir["sections"].get(number)
        return bool(s) and number not in self.cmd_section and bool(_STRUCT.search(s["title"] or ""))

    def is_struct_table(self, t: dict) -> bool:
        cap = _name_of(t.get("caption"))
        kind = classify_table(t.get("header") or [], t.get("caption") or "")
        if kind in ("opcode", "values") or self._is_opcode_table(t) or _CAP_CMD.match(norm_title(cap)):
            return False
        return kind == "bytes" or bool(_STRUCT.search(cap))

    def command_of_table(self, t: dict) -> str | None:
        m = _CAP_CMD.match(norm_title(_name_of(t.get("caption"))))
        if m and _ck(m.group("name")) in self.cmds:
            return _ck(m.group("name"))
        return self.command_of_section(t.get("section"))

    def has_command(self, k: str, opcode_key: tuple | None = None) -> bool:
        return k in self.cmds or (opcode_key is not None and opcode_key in self.by_opcode)


# ----------------------------------------------------------------------------- one-line description of a change

def _rows_text(rows: list, n: int = 4) -> str:
    keys = [clean(r[0]) if isinstance(r, list) and r else clean(r.get("key", "")) if isinstance(r, dict) else "" for r in rows[:n]]
    keys = [k for k in keys if k]
    more = len(rows) - n
    return ", ".join(keys) + (f" +{more} more" if more > 0 else "")


def describe(c: dict) -> str:
    """A short factual line for one change, used in the entity tables when there is no AI description."""
    d = c.get("details") or {}
    where = (f"Fig {c.get('number_to') or c.get('number_from')}" if c["kind"] == "table" else f"§{c.get('number_to') or c.get('number_from')}")
    name = _name_of(c["title"]) if c["kind"] == "table" else c["title"]
    if c["change"] == "added":
        return f"{where} {name} added"
    if c["change"] == "removed":
        return f"{where} {name} removed"
    if c["change"] == "renamed":
        return f"{where} renamed from “{_name_of(c.get('title_from') or '')}”"
    if c["change"] == "moved":
        return f"{where} renumbered from {c.get('number_from')}"
    if c["kind"] == "section":
        cnt, parts = d.get("counts") or {}, []
        norm = c.get("norm") or {}
        if cnt.get("added"):
            parts.append(f"{cnt['added']} sentence(s) added" + (f" ({norm['added']} normative)" if norm.get("added") else ""))
        if cnt.get("removed"):
            parts.append(f"{cnt['removed']} removed" + (f" ({norm['removed']} normative)" if norm.get("removed") else ""))
        if cnt.get("modified"):
            parts.append(f"{cnt['modified']} reworded" + (f" ({norm['strength_changed']} with a changed shall/should/may)" if norm.get("strength_changed") else ""))
        return f"{where} {name}: " + (", ".join(parts) or "text changed")
    parts = []
    if d.get("header_changed"):
        parts.append("columns changed")
    if d.get("rows_added_count", len(d.get("rows_added") or [])):
        parts.append(f"rows added: {_rows_text(d.get('rows_added') or [])}" if d.get("rows_added") else f"{d['rows_added_count']} row(s) added")
    if d.get("rows_removed_count", len(d.get("rows_removed") or [])):
        parts.append(f"rows removed: {_rows_text(d.get('rows_removed') or [])}" if d.get("rows_removed") else f"{d['rows_removed_count']} row(s) removed")
    if d.get("rows_changed_count", len(d.get("rows_changed") or [])):
        parts.append(f"rows changed: {_rows_text(d.get('rows_changed') or [])}" if d.get("rows_changed") else f"{d['rows_changed_count']} row(s) changed")
    return f"{where} {name}: " + ("; ".join(parts) or "changed")


# ----------------------------------------------------------------------------- the tables

def _worst(impacts: list[str]) -> str:
    return min(impacts, key=_IMPACT_ORDER.index) if impacts else "changed"


def build(ira: dict, irb: dict, changes: list[dict]) -> dict[str, Any]:
    """{group: {"total": n, "items": [...]}} for every group in GROUPS. `changes` carry their list id as "ord" (or "id")."""
    A, B = _Version(ira), _Version(irb)
    rows: dict[str, dict[str, dict]] = {g: {} for g in GROUPS}

    def entry(group: str, key: str, **base: Any) -> dict:
        e = rows[group].get(key)
        if e is None:
            e = rows[group][key] = {"key": key, "name": "", "opcode": "", "where": [], "impact": "", "impacts": [], "changes": [], "notes": [], **base}
        for k, v in base.items():
            if v and not e.get(k):
                e[k] = v
        return e

    def cite(e: dict, c: dict, text: str | None = None) -> None:
        cid = c.get("ord", c.get("id"))
        if c["impact"] != "editorial":
            e["impacts"].append(c["impact"])
        if all(x["id"] != cid or x["text"] != (text or describe(c)) for x in e["changes"]):
            e["changes"].append({"id": cid, "kind": c["kind"], "number": c.get("number_to") or c.get("number_from"), "impact": c["impact"],
                                 "text": text or describe(c)})
        where = ("Fig " if c["kind"] == "table" else "§") + str(c.get("number_to") or c.get("number_from"))
        if where not in e["where"]:
            e["where"].append(where)

    def command(k: str, c: dict, text: str | None = None, opcode_key: tuple | None = None, name: str = "", opcode: str = "") -> None:
        info = B.cmds.get(k) or A.cmds.get(k) or {}
        in_a, in_b = A.has_command(k, opcode_key), B.has_command(k, opcode_key)
        group = "commands_added" if in_b and not in_a else "commands_removed" if in_a and not in_b else "commands_modified"
        if group == "commands_modified" and c["impact"] == "editorial":
            return              # renumbering and renaming of a section is not a change to the command
        e = entry(group, k, name=name or info.get("name", ""), opcode=opcode or info.get("opcode", ""))
        cite(e, c, text)

    def structure(k: str, name: str, c: dict) -> None:
        in_a, in_b = k in A.structs, k in B.structs
        if c["change"] == "added":
            group = "structures_added" if not in_a else "structures_modified"
        elif c["change"] == "removed":
            group = "structures_removed" if not in_b else "structures_modified"
        else:
            group = "structures_modified"
        if group == "structures_modified" and c["impact"] == "editorial":
            return
        e = entry(group, k, name=name)
        cite(e, c)

    opcode_changes: list[dict] = []
    for c in changes:
        side_b = c["change"] != "removed"
        V, num = (B, c.get("number_to")) if side_b else (A, c.get("number_from"))
        if c["kind"] == "table":
            t = V.ir["tables"].get(num)
            if t is None:
                continue
            if num in V.opcode_tables:
                opcode_changes.append(c)
                continue
            if V.is_struct_table(t):
                if c["change"] in ("renamed", "modified") and c.get("title_from"):
                    k_old = _key(_name_of(c["title_from"]))
                    k = _key(_name_of(t.get("caption")))
                    if k_old != k:      # a renamed structure is the same structure: keep it under its new name
                        A.structs.setdefault(k, A.structs.get(k_old, ""))
                structure(_key(_name_of(t.get("caption"))), _name_of(t.get("caption")), c)
                continue
            k = V.command_of_table(t)
            if k:
                command(k, c)
            continue
        # sections
        if num in V.cmd_section:
            k = V.cmd_section[num]
            if c["change"] in ("renamed", "modified") and c.get("title_from"):
                k_old = _ck(c["title_from"])
                if k_old in A.cmds and k not in A.cmds:
                    A.cmds[k] = A.cmds[k_old]
            command(k, c)
            continue
        if V.is_struct_section(num):
            s = V.ir["sections"][num]
            inner = [t for t in V.ir["tables"].values() if t.get("section") == num and V.is_struct_table(t)]
            if inner:        # the section documents the structure its table defines: one row, named after the table
                structure(_key(_name_of(inner[0].get("caption"))), _name_of(inner[0].get("caption")), c)
            else:
                structure(_key(s["title"]), clean(s["title"]), c)
            continue
        k = V.command_of_section(num)
        if k:
            command(k, c)

    if opcode_changes:
        _opcode_rows(A, B, opcode_changes, command)

    out: dict[str, Any] = {}
    for g in GROUPS:
        items = list(rows[g].values())
        for e in items:
            e["impact"] = "new" if g.endswith("_added") else "breaking" if g.endswith("_removed") else _worst(e.pop("impacts"))
            e.pop("impacts", None)
            e["changes"].sort(key=lambda x: (_IMPACT_ORDER.index(x["impact"]), x["id"]))
            e["change_count"] = len(e["changes"])
            e["change_ids"] = sorted({x["id"] for x in e["changes"]})[:500]
            e["changes"] = e["changes"][:MAX_REFS]
            if not e["name"]:
                e["name"] = e["key"]
        items.sort(key=lambda e: (_IMPACT_ORDER.index(e["impact"]), -e["change_count"], e["name"].lower()))
        out[g] = {"total": len(items), "items": items[:MAX_ROWS]}
    return out


def _opcode_rows(A: _Version, B: _Version, changes: list[dict], command) -> None:
    """Commands added to, removed from or changed in the opcode tables. Compared from the full tables of both versions (the change
    details are capped for display, and an opcode table can be added or removed as a whole); each finding cites the change of the
    opcode table it is in."""
    by_table: dict[tuple[str, str], dict] = {}
    for c in changes:           # a matched table may carry a different number in each version
        if c.get("number_from"):
            by_table[("a", c["number_from"])] = c
        if c.get("number_to"):
            by_table[("b", c["number_to"])] = c

    def change_for(side: str, label: str) -> dict | None:
        return by_table.get((side, label))

    for key in sorted(set(A.opcode_rows) | set(B.opcode_rows)):
        ra, rb = A.opcode_rows.get(key), B.opcode_rows.get(key)
        rl, opc = key
        if rb and not ra:
            c = change_for("b", rb["table"])
            if c:
                command(_ck(rb["name"]), {**c, "impact": "new"}, f"Fig {rb['table']}: opcode {opc} added", key, rb["name"], f"{rl} {opc}")
        elif ra and not rb:
            c = change_for("a", ra["table"])
            if c:
                command(_ck(ra["name"]), {**c, "impact": "breaking"}, f"Fig {c.get('number_to') or ra['table']}: opcode {opc} removed",
                        key, ra["name"], f"{rl} {opc}")
        elif ra and rb and ra["cells"] != rb["cells"]:
            c = change_for("b", rb["table"])
            if not c:
                continue
            diffs = []
            for i in range(max(len(ra["cells"]), len(rb["cells"]))):
                x = ra["cells"][i] if i < len(ra["cells"]) else ""
                y = rb["cells"][i] if i < len(rb["cells"]) else ""
                if x != y:
                    col = (rb["header"][i] if i < len(rb["header"]) else "") or f"col {i + 1}"
                    diffs.append(f"{clean(col)}: {x or '∅'} → {y or '∅'}")
            k = _ck(rb["name"])
            info = B.cmds.get(k, {})
            command(k, {**c, "impact": "changed"}, f"Fig {rb['table']}: opcode {opc} " + "; ".join(diffs[:3]), key, rb["name"], info.get("opcode") or f"{rl} {opc}")
