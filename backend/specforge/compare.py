"""Compare two parsed versions of a specification and list what changed.

Code decides what changed; AI (compare_ai.py) only summarises. The comparison works on the parsed model of each version (sections,
tables, normative statements) rather than on flat text, because spec revisions renumber sections and figures constantly and a text
diff would drown in that noise:

  1. sections   matched by their title path first (so a renumbered section is "moved", not "removed + added"), then by number,
                then by title; matched pairs are compared sentence by sentence, with the word-level edit of each modified sentence
  2. tables     matched by caption inside matched sections, then by caption, then by header; rows are compared by their first column
                (bit range, opcode, value ...) so a changed field description shows as one modified row
  3. impact     every change gets a deterministic impact: breaking / new / changed / clarification / editorial, derived from the
                normative words (shall, should, may), reserved bits, removed rows and numeric edits it contains

The result is stored (tables spec_compare / compare_change) so it can be revisited, filtered and exported.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
from typing import Any

from .core.db import jloads, row, rows
from .ingest import store
from .ingest.normalize import normative_info, split_sentences

ALGO_VERSION = "1"
IMPACTS = ("breaking", "new", "changed", "clarification", "editorial")
_SCORE = {"breaking": 100, "changed": 60, "new": 50, "clarification": 20, "editorial": 5}
MAX_DETAIL_ITEMS = 40      # sentences / rows kept per change (counts always cover everything)
MAX_CELL = 400
FUZZY_CAP = 2_000_000      # title comparisons allowed in the fuzzy matching pass
_CAPTION_LABEL = re.compile(r"^(?:figure|table|fig\.?)\s*[\w.\-]+\s*[:.–—-]?\s*", re.I)
_WORD = re.compile(r"\w+|[^\w\s]", re.U)


class CompareError(ValueError):
    pass


def _nt(s: str | None) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def _cap(s: str | None) -> str:
    return _nt(_CAPTION_LABEL.sub("", (s or "").strip()))


def _chapter(number: str | None) -> str:
    n = (number or "").strip()
    if not n or n == "front":
        return "front"
    m = re.match(r"(?i)annex\s+([a-z])", n)
    return ("Annex " + m.group(1).upper()) if m else n.split(".")[0]


# ----------------------------------------------------------------------------- section matching

def _path(sections: dict[str, dict], num: str) -> tuple[str, ...]:
    parts, n, guard = [], num, 0
    while n and n in sections and guard < 24:
        parts.append(_nt(sections[n]["title"]))
        n, guard = sections[n]["parent"], guard + 1
    return tuple(reversed(parts))


def match_sections(a: dict[str, dict], b: dict[str, dict]) -> list[tuple[str, str, str]]:
    """(number in A, number in B, how) for every section present in both versions."""
    pairs: list[tuple[str, str, str]] = []
    ua, ub = set(a), set(b)

    def take(x: str, y: str, how: str) -> None:
        pairs.append((x, y, how))
        ua.discard(x)
        ub.discard(y)

    # 1. identical title path (a renumbered section keeps its place in the outline)
    pa: dict[tuple, list[str]] = {}
    pb: dict[tuple, list[str]] = {}
    for n in a:
        pa.setdefault(_path(a, n), []).append(n)
    for n in b:
        pb.setdefault(_path(b, n), []).append(n)
    for path, xs in pa.items():
        ys = pb.get(path, [])
        for x, y in zip(xs, ys):
            take(x, y, "path")
    # 2. same number, similar title
    for x in sorted(ua):
        if x in b and x in ub and difflib.SequenceMatcher(None, _nt(a[x]["title"]), _nt(b[x]["title"])).ratio() >= 0.6:
            take(x, x, "number")
    # 3. same title, unique on both sides among what is left
    ta: dict[str, list[str]] = {}
    tb: dict[str, list[str]] = {}
    for n in ua:
        ta.setdefault(_nt(a[n]["title"]), []).append(n)
    for n in ub:
        tb.setdefault(_nt(b[n]["title"]), []).append(n)
    for t, xs in ta.items():
        ys = tb.get(t, [])
        if t and len(xs) == 1 and len(ys) == 1:
            take(xs[0], ys[0], "title")
    # 4. near-identical title (a renamed section)
    budget = FUZZY_CAP
    cand_a = sorted(ua)
    for y in sorted(ub):
        if budget <= 0 or not cand_a:
            break
        ty, best, best_r = _nt(b[y]["title"]), None, 0.0
        if len(ty) < 4:
            continue
        for x in cand_a:
            budget -= 1
            sm = difflib.SequenceMatcher(None, _nt(a[x]["title"]), ty)
            if sm.real_quick_ratio() >= 0.86 and sm.quick_ratio() >= 0.86:
                r = sm.ratio()
                if r > best_r:
                    best, best_r = x, r
        if best is not None and best_r >= 0.86:
            take(best, y, "renamed")
            cand_a.remove(best)
    return pairs


# ----------------------------------------------------------------------------- text comparison

def _words(s: str) -> list[str]:
    return _WORD.findall(s)


def word_delta(before: str, after: str) -> dict[str, list[str]]:
    """The words removed from / added to a sentence, in order (what a reader would highlight)."""
    wa, wb = _words(before), _words(after)
    rem: list[str] = []
    add: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            rem.append(" ".join(wa[i1:i2]))
        if op in ("replace", "insert"):
            add.append(" ".join(wb[j1:j2]))
    return {"removed": rem[:12], "added": add[:12]}


def _norm_sentence(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _strength(s: str) -> str | None:
    info = normative_info(s)
    return info[0] if info else None


def _sentence_impact(kind: str, before: str | None, after: str | None, delta: dict | None) -> str:
    sb, sa = (_strength(before) if before else None), (_strength(after) if after else None)
    if kind == "added":
        return "new" if sa else "clarification"
    if kind == "removed":
        return "breaking" if sb in ("mandatory", "prohibited") else ("changed" if sb else "clarification")
    if sb != sa:      # shall -> should, may -> shall, a requirement dropping its normative word ...
        return "breaking" if "mandatory" in (sb, sa) or "prohibited" in (sb, sa) else "changed"
    changed = " ".join((delta or {}).get("removed", []) + (delta or {}).get("added", []))
    if re.search(r"\d|\b[0-9a-f]+h\b|\breserved\b", changed, re.I):
        return "changed"
    if re.fullmatch(r"[\W_]*", changed):
        return "editorial"
    return "clarification" if not sa else "changed"


def diff_text(a: str, b: str) -> dict[str, Any] | None:
    """Sentence-level difference of two section bodies; None when nothing changed."""
    sa, sb = [_norm_sentence(s) for s in split_sentences(a)], [_norm_sentence(s) for s in split_sentences(b)]
    if sa == sb:
        return None
    added: list[dict] = []
    removed: list[dict] = []
    modified: list[dict] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, sa, sb, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        old, new = sa[i1:i2], sb[j1:j2]
        used: set[int] = set()
        for o in old:               # pair each old sentence with its closest new one
            best, best_r = None, 0.0
            for k, nw in enumerate(new):
                if k in used:
                    continue
                r = difflib.SequenceMatcher(None, o, nw, autojunk=False).ratio()
                if r > best_r:
                    best, best_r = k, r
            if best is not None and best_r >= 0.5:
                used.add(best)
                d = word_delta(o, new[best])
                modified.append({"before": o, "after": new[best], "delta": d, "impact": _sentence_impact("modified", o, new[best], d),
                                 "strength": [_strength(o), _strength(new[best])]})
            else:
                removed.append({"text": o, "strength": _strength(o), "impact": _sentence_impact("removed", o, None, None)})
        for k, nw in enumerate(new):
            if k not in used:
                added.append({"text": nw, "strength": _strength(nw), "impact": _sentence_impact("added", None, nw, None)})
    # trivially reflowed text (same words, different sentence breaks) is no change
    if not added and not removed and all(m["impact"] == "editorial" for m in modified):
        return {"ratio": 1.0, "added": [], "removed": [], "modified": modified, "editorial_only": True}
    ratio = difflib.SequenceMatcher(None, " ".join(sa), " ".join(sb), autojunk=False).ratio() if len(" ".join(sa + sb)) < 40000 else 0.0
    return {"ratio": round(ratio, 3), "added": added, "removed": removed, "modified": modified, "editorial_only": False}


# ----------------------------------------------------------------------------- tables

def _cells(r: list) -> list[str]:
    return [re.sub(r"\s+", " ", str(c or "")).strip() for c in r]


def _row_key(r: list[str]) -> str:
    return _nt(r[0]) if r else ""


def diff_table(ta: dict, tb: dict) -> dict[str, Any] | None:
    ha, hb = _cells(ta.get("header") or []), _cells(tb.get("header") or [])
    ra, rb = [_cells(r) for r in ta.get("rows") or []], [_cells(r) for r in tb.get("rows") or []]
    header_changed = [c.lower() for c in ha] != [c.lower() for c in hb]
    ka, kb = [_row_key(r) for r in ra], [_row_key(r) for r in rb]
    keyed = all(ka) and all(kb) and len(set(ka)) == len(ka) and len(set(kb)) == len(kb)
    added: list[list[str]] = []
    removed: list[list[str]] = []
    changed: list[dict] = []
    if keyed:
        da, db = dict(zip(ka, ra)), dict(zip(kb, rb))
        for k, r in da.items():
            if k not in db:
                removed.append(r)
            elif [c.lower() for c in r] != [c.lower() for c in db[k]]:
                changed.append(_cell_changes(r, db[k], ha, hb))
        added = [r for k, r in db.items() if k not in da]
    else:       # no usable key column: align the rows as lines
        la, lb = [" | ".join(r).lower() for r in ra], [" | ".join(r).lower() for r in rb]
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes():
            if op == "delete":
                removed += ra[i1:i2]
            elif op == "insert":
                added += rb[j1:j2]
            elif op == "replace":
                for x, y in zip(ra[i1:i2], rb[j1:j2]):
                    changed.append(_cell_changes(x, y, ha, hb))
                removed += ra[i1 + (j2 - j1):i2]
                added += rb[j1 + (i2 - i1):j2]
    # a row whose key changed (a field moved to other bits) shows up removed + added: pair them by the rest of the row
    still_added = list(added)
    for r in list(removed):
        best, best_r = None, 0.0
        for cand in still_added:
            sm = difflib.SequenceMatcher(None, " | ".join(r[1:]).lower(), " | ".join(cand[1:]).lower(), autojunk=False)
            if sm.real_quick_ratio() < 0.75 or sm.quick_ratio() < 0.75:
                continue
            if (rr := sm.ratio()) > best_r:
                best, best_r = cand, rr
        if best is not None and best_r >= 0.75 and len(r) > 1:
            removed.remove(r)
            still_added.remove(best)
            c = _cell_changes(r, best, ha, hb)
            c["rekeyed"] = True
            changed.append(c)
    added = still_added
    if not (header_changed or added or removed or changed):
        return None
    return {"header_before": ha, "header_after": hb, "header_changed": header_changed,
            "rows_added": added, "rows_removed": removed, "rows_changed": changed}


def _cell_changes(x: list[str], y: list[str], ha: list[str], hb: list[str]) -> dict:
    cols = []
    for i in range(max(len(x), len(y))):
        bx, ay = (x[i] if i < len(x) else ""), (y[i] if i < len(y) else "")
        if bx.lower() != ay.lower():
            name = (hb[i] if i < len(hb) else None) or (ha[i] if i < len(ha) else None) or f"col {i + 1}"
            cols.append({"column": name, "before": bx[:MAX_CELL], "after": ay[:MAX_CELL],
                         "delta": word_delta(bx, ay) if len(bx) + len(ay) > 40 else None})
    return {"key": (y[0] if y else x[0] if x else ""), "key_before": x[0] if x else "", "cells": cols}


def _table_impact(d: dict) -> str:
    if d["rows_removed_count"]:
        return "breaking"
    defined = False
    for c in d["rows_changed"]:
        before = " ".join(cell["before"] for cell in c["cells"]).lower()
        after = " ".join(cell["after"] for cell in c["cells"]).lower()
        if "reserved" in after and "reserved" not in before:
            return "breaking"            # a defined field became reserved
        defined = defined or ("reserved" in before and "reserved" not in after)
    if d["rows_added_count"] or defined:
        return "new"                     # new rows, or a reserved field that is now defined
    if d["rows_changed"] or d["header_changed"]:
        return "changed"
    return "editorial"


def match_tables(a: dict[str, dict], b: dict[str, dict], sec_pairs: dict[str, str]) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    ua, ub = set(a), set(b)
    b_to_a = {v: k for k, v in sec_pairs.items()}

    def a_sec_of(t: dict) -> str | None:       # the section *in A* that the table's (B-side) section was matched to
        return b_to_a.get(t.get("section") or "")

    def take(x: str, y: str, how: str) -> None:
        pairs.append((x, y, how))
        ua.discard(x)
        ub.discard(y)

    # 1. same caption inside matched sections
    by_a: dict[tuple, list[str]] = {}
    by_b: dict[tuple, list[str]] = {}
    for k in a:
        by_a.setdefault((sec_pairs.get(a[k].get("section") or "", None), _cap(a[k].get("caption"))), []).append(k)
    for k in b:
        by_b.setdefault((a_sec_of(b[k]), _cap(b[k].get("caption"))), []).append(k)
    for key, xs in by_a.items():
        if key[0] is None or not key[1]:
            continue
        for x, y in zip(xs, by_b.get(key, [])):
            if x in ua and y in ub:
                take(x, y, "section+caption")
    # 2. same caption anywhere, unique on both sides
    ca: dict[str, list[str]] = {}
    cb: dict[str, list[str]] = {}
    for k in ua:
        ca.setdefault(_cap(a[k].get("caption")), []).append(k)
    for k in ub:
        cb.setdefault(_cap(b[k].get("caption")), []).append(k)
    for c, xs in ca.items():
        ys = cb.get(c, [])
        if c and len(xs) == 1 and len(ys) == 1:
            take(xs[0], ys[0], "caption")
    # 3. same header in matched sections (uncaptioned tables), in order
    ha: dict[tuple, list[str]] = {}
    hb: dict[tuple, list[str]] = {}
    for k in sorted(ua):
        ha.setdefault((sec_pairs.get(a[k].get("section") or ""), tuple(c.lower() for c in _cells(a[k].get("header") or []))), []).append(k)
    for k in sorted(ub):
        hb.setdefault((a_sec_of(b[k]), tuple(c.lower() for c in _cells(b[k].get("header") or []))), []).append(k)
    for key, xs in ha.items():
        if key[0] is None or not any(key[1]):
            continue
        for x, y in zip(xs, hb.get(key, [])):
            take(x, y, "header")
    # 4. near-identical caption
    budget = FUZZY_CAP
    for y in sorted(ub):
        cy = _cap(b[y].get("caption"))
        if len(cy) < 6 or budget <= 0:
            continue
        best, best_r = None, 0.0
        for x in sorted(ua):
            budget -= 1
            cx = _cap(a[x].get("caption"))
            sm = difflib.SequenceMatcher(None, cx, cy)
            if cx and sm.real_quick_ratio() >= 0.85 and sm.quick_ratio() >= 0.85 and (r := sm.ratio()) > best_r:
                best, best_r = x, r
        if best is not None and best_r >= 0.85:
            take(best, y, "caption~")
    return pairs


# ----------------------------------------------------------------------------- the comparison

def _excerpt(text: str, n: int = 3, chars: int = 600) -> list[str]:
    out, total = [], 0
    for s in split_sentences(text):
        out.append(s)
        total += len(s)
        if len(out) >= n or total >= chars:
            break
    return out


def _normative_count(text: str) -> int:
    return sum(1 for s in split_sentences(text) if normative_info(s))


def compare_irs(ira: dict, irb: dict) -> dict[str, Any]:
    """The full comparison of two loaded IRs: {"summary": ..., "changes": [...]}."""
    sa, sb = ira["sections"], irb["sections"]
    pairs = match_sections(sa, sb)
    a_to_b = {x: y for x, y, _ in pairs}
    b_to_a = {y: x for x, y, _ in pairs}
    changes: list[dict[str, Any]] = []

    def add(**c: Any) -> None:
        c.setdefault("details", {})
        c["score"] = _SCORE[c["impact"]] + min(c.get("normative", 0), 20)
        changes.append(c)

    # --- sections
    for x, y, how in pairs:
        A, B = sa[x], sb[y]
        d = diff_text(A["text"], B["text"])
        moved = x != y
        renamed = _nt(A["title"]) != _nt(B["title"])
        if d is None or d.get("editorial_only"):
            if moved or renamed:
                add(kind="section", change="moved" if moved else "renamed", impact="editorial", chapter=_chapter(y), number_from=x, number_to=y,
                    title=B["title"], title_from=A["title"], details={"how": how})
            continue
        impacts = [i["impact"] for k in ("added", "removed", "modified") for i in d[k]]
        impact = min(impacts, key=IMPACTS.index) if impacts else "clarification"
        norm = sum(1 for k in ("added", "removed", "modified") for i in d[k]
                   if (i.get("strength") if k != "modified" else (i["strength"][0] or i["strength"][1])))
        n_items = len(d["added"]) + len(d["removed"]) + len(d["modified"])
        det = {"how": how, "ratio": d["ratio"], "counts": {"added": len(d["added"]), "removed": len(d["removed"]), "modified": len(d["modified"])},
               "added": d["added"][:MAX_DETAIL_ITEMS], "removed": d["removed"][:MAX_DETAIL_ITEMS], "modified": d["modified"][:MAX_DETAIL_ITEMS],
               "truncated": n_items > 3 * MAX_DETAIL_ITEMS}
        add(kind="section", change="modified", impact=impact, chapter=_chapter(y), number_from=x, number_to=y, title=B["title"],
            title_from=A["title"] if renamed else None, moved=moved, normative=norm, details=det)
    for x in sa:
        if x not in a_to_b:
            A = sa[x]
            add(kind="section", change="removed", impact="breaking" if _normative_count(A["text"]) else "changed", chapter=_chapter(x),
                number_from=x, number_to=None, title=A["title"], normative=_normative_count(A["text"]),
                nested=bool(A["parent"] and A["parent"] not in a_to_b), details={"excerpt": _excerpt(A["text"])})
    for y in sb:
        if y not in b_to_a:
            B = sb[y]
            add(kind="section", change="added", impact="new", chapter=_chapter(y), number_from=None, number_to=y, title=B["title"],
                normative=_normative_count(B["text"]), nested=bool(B["parent"] and B["parent"] not in b_to_a), details={"excerpt": _excerpt(B["text"])})

    # --- tables
    ta, tb = ira["tables"], irb["tables"]
    tpairs = match_tables(ta, tb, a_to_b)
    t_matched_a, t_matched_b = {x for x, _, _ in tpairs}, {y for _, y, _ in tpairs}
    for x, y, how in tpairs:
        A, B = ta[x], tb[y]
        d = diff_table(A, B)
        relabel = x != y
        if d is None:
            if relabel:
                add(kind="table", change="moved", impact="editorial", chapter=_chapter(B.get("section")), number_from=x, number_to=y,
                    title=B.get("caption") or f"Table {y}", details={"how": how})
            continue
        d["how"] = how
        for k in ("rows_added", "rows_removed"):
            d[k + "_count"] = len(d[k])
            d[k] = d[k][:MAX_DETAIL_ITEMS]
        d["rows_changed_count"] = len(d["rows_changed"])
        d["rows_changed"] = d["rows_changed"][:MAX_DETAIL_ITEMS]
        impact = _table_impact(d)
        add(kind="table", change="modified", impact=impact, chapter=_chapter(B.get("section")), number_from=x, number_to=y,
            title=B.get("caption") or f"Table {y}", moved=relabel, normative=0, details=d)
    for x in ta:
        if x not in t_matched_a:
            A = ta[x]
            add(kind="table", change="removed", impact="breaking", chapter=_chapter(A.get("section")), number_from=x, number_to=None,
                title=A.get("caption") or f"Table {x}", nested=bool(A.get("section")) and A.get("section") not in a_to_b,
                details={"header": _cells(A.get("header") or []), "rows": [_cells(r) for r in (A.get("rows") or [])[:MAX_DETAIL_ITEMS]],
                         "row_count": len(A.get("rows") or [])})
    for y in tb:
        if y not in t_matched_b:
            B = tb[y]
            add(kind="table", change="added", impact="new", chapter=_chapter(B.get("section")), number_from=None, number_to=y,
                title=B.get("caption") or f"Table {y}", nested=bool(B.get("section")) and B.get("section") not in b_to_a,
                details={"header": _cells(B.get("header") or []), "rows": [_cells(r) for r in (B.get("rows") or [])[:MAX_DETAIL_ITEMS]],
                         "row_count": len(B.get("rows") or [])})

    # stable ordering: document order of the newer version (removals by their old position), then by kind
    order_b = {n: i for i, n in enumerate(irb["section_order"])}
    order_a = {n: i for i, n in enumerate(ira["section_order"])}

    def sort_key(c: dict) -> tuple:
        n = c.get("number_to") or c.get("number_from")
        pos = order_b.get(c.get("number_to")) if c["kind"] == "section" and c.get("number_to") in order_b else None
        if pos is None and c["kind"] == "section":
            pos = order_a.get(c.get("number_from"), 10 ** 6)
        return (pos if pos is not None else 10 ** 7, 0 if c["kind"] == "section" else 1, str(n))
    changes.sort(key=sort_key)
    for i, c in enumerate(changes, 1):
        c["ord"] = i
    return {"summary": summarise(ira, irb, changes, len(pairs), len(tpairs)), "changes": changes}


def summarise(ira: dict, irb: dict, changes: list[dict], n_sections_matched: int, n_tables_matched: int) -> dict[str, Any]:
    by_impact = {i: 0 for i in IMPACTS}
    by_change: dict[str, int] = {}
    chapters: dict[str, dict[str, Any]] = {}
    norm = {"added": 0, "removed": 0, "strength_changed": 0}
    for c in changes:
        by_impact[c["impact"]] += 1
        key = f"{c['kind']}_{c['change']}"
        by_change[key] = by_change.get(key, 0) + 1
        ch = chapters.setdefault(c["chapter"], {"chapter": c["chapter"], "title": "", "total": 0, "score": 0, **{i: 0 for i in IMPACTS}})
        ch["total"] += 1
        ch[c["impact"]] += 1
        ch["score"] += c["score"]
        if c["kind"] == "section" and c["change"] == "modified":
            d = c["details"]
            norm["added"] += sum(1 for s in d.get("added", []) if s.get("strength"))
            norm["removed"] += sum(1 for s in d.get("removed", []) if s.get("strength"))
            norm["strength_changed"] += sum(1 for m in d.get("modified", []) if m["strength"][0] != m["strength"][1])
        elif c["kind"] == "section" and c["change"] in ("added", "removed") and not c.get("nested"):
            norm["added" if c["change"] == "added" else "removed"] += c.get("normative", 0)
    titles = {}
    for ir in (ira, irb):
        for n, s in ir["sections"].items():
            if "." not in n and n not in titles:
                titles[_chapter(n)] = s["title"]
    for k, ch in chapters.items():
        ch["title"] = titles.get(k, "")
    roots = [c for c in changes if not c.get("nested")]
    return {
        "from": {"run_id": ira["run_id"], "family": ira["family"], "version": ira["version"],
                 "sections": len(ira["sections"]), "tables": len(ira["tables"]), "requirements": len(ira["requirements"])},
        "to": {"run_id": irb["run_id"], "family": irb["family"], "version": irb["version"],
               "sections": len(irb["sections"]), "tables": len(irb["tables"]), "requirements": len(irb["requirements"])},
        "total": len(changes), "by_impact": by_impact, "by_change": by_change,
        "roots": {"sections_added": sum(1 for c in roots if c["kind"] == "section" and c["change"] == "added"),
                  "sections_removed": sum(1 for c in roots if c["kind"] == "section" and c["change"] == "removed"),
                  "tables_added": sum(1 for c in roots if c["kind"] == "table" and c["change"] == "added"),
                  "tables_removed": sum(1 for c in roots if c["kind"] == "table" and c["change"] == "removed")},
        "matched": {"sections": n_sections_matched, "tables": n_tables_matched},
        "normative": norm,
        "chapters": sorted(chapters.values(), key=lambda c: -c["score"]),
        "algo": ALGO_VERSION,
    }


def quick_overview(summary: dict) -> str:
    """A factual overview written from the counts alone (shown when no AI summary has been generated)."""
    f, t = summary["from"], summary["to"]
    bi, r, n = summary["by_impact"], summary["roots"], summary["normative"]
    if not summary["total"]:
        return f"{t['family']} {t['version']} has no content differences from {f['version']}."
    parts = [f"{t['family']} {t['version']} differs from {f['version']} in {summary['total']} places."]
    new_bits = []
    if r["sections_added"]:
        new_bits.append(f"{r['sections_added']} new section(s)")
    if r["tables_added"]:
        new_bits.append(f"{r['tables_added']} new table(s)/figure(s)")
    if new_bits:
        parts.append("It adds " + " and ".join(new_bits) + ".")
    gone = []
    if r["sections_removed"]:
        gone.append(f"{r['sections_removed']} section(s)")
    if r["tables_removed"]:
        gone.append(f"{r['tables_removed']} table(s)/figure(s)")
    if gone:
        parts.append("It removes " + " and ".join(gone) + ".")
    if n["added"] or n["removed"] or n["strength_changed"]:
        parts.append(f"Normative statements: {n['added']} added, {n['removed']} removed, {n['strength_changed']} with a changed requirement level (shall/should/may).")
    parts.append(f"{bi['breaking']} change(s) may break existing implementations or tests, {bi['changed']} change behaviour or values, "
                 f"{bi['clarification']} are clarifications and {bi['editorial']} are editorial (renumbering, renaming).")
    top = [c for c in summary["chapters"] if c["total"]][:3]
    if top:
        parts.append("Most affected: " + ", ".join(f"{('chapter ' + c['chapter']) if c['chapter'][0].isdigit() else c['chapter']}"
                                                   f"{(' (' + c['title'] + ')') if c['title'] else ''}" for c in top) + ".")
    return " ".join(parts)


# ----------------------------------------------------------------------------- storage

def _run_label(conn: sqlite3.Connection, run_id: int) -> dict:
    r = row(conn, "SELECT r.id,r.status,d.family,d.version,d.filename FROM parse_run r JOIN spec_document d ON d.id=r.document_id WHERE r.id=?", (run_id,))
    if not r:
        raise CompareError(f"parse run {run_id} not found")
    if r["status"] != "done":
        raise CompareError(f"{r['family']} {r['version']} has not been parsed successfully (run {run_id} is {r['status']}); parse it on the Specs page first")
    return r


def run_comparison(conn: sqlite3.Connection, run_a: int, run_b: int, progress=None) -> int:
    """Compare two parse runs and store the result; returns the comparison id (replacing an earlier one for the same pair)."""
    if run_a == run_b:
        raise CompareError("choose two different spec versions")
    _run_label(conn, run_a)
    _run_label(conn, run_b)
    if progress:
        progress("loading both versions", 0, 3)
    ira, irb = store.load_ir(conn, run_a), store.load_ir(conn, run_b)
    if progress:
        progress("comparing sections and tables", 1, 3)
    res = compare_irs(ira, irb)
    if progress:
        progress("saving", 2, 3)
    conn.execute("DELETE FROM spec_compare WHERE run_a=? AND run_b=?", (run_a, run_b))
    cur = conn.execute("INSERT INTO spec_compare(run_a,run_b,algo,summary_json) VALUES(?,?,?,?)", (run_a, run_b, ALGO_VERSION, json.dumps(res["summary"])))
    cid = cur.lastrowid
    conn.executemany(
        "INSERT INTO compare_change(compare_id,ord,kind,change,impact,score,chapter,number_from,number_to,title,nested,detail_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [(cid, c["ord"], c["kind"], c["change"], c["impact"], c["score"], c["chapter"], c.get("number_from"), c.get("number_to"), c["title"],
          1 if c.get("nested") else 0,
          json.dumps({k: v for k, v in c.items() if k in ("details", "title_from", "moved", "normative")})) for c in res["changes"]])
    conn.commit()
    return cid


def list_comparisons(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in rows(conn, "SELECT id,run_a,run_b,summary_json,ai_json IS NOT NULL AS has_ai,created FROM spec_compare ORDER BY id DESC LIMIT 100"):
        s = jloads(r["summary_json"], {})
        out.append({"id": r["id"], "created": r["created"], "has_ai": bool(r["has_ai"]), "from": s.get("from"), "to": s.get("to"),
                    "total": s.get("total", 0), "by_impact": s.get("by_impact", {})})
    return out


def get_comparison(conn: sqlite3.Connection, cid: int) -> dict | None:
    r = row(conn, "SELECT * FROM spec_compare WHERE id=?", (cid,))
    if not r:
        return None
    summary = jloads(r["summary_json"], {})
    return {"id": cid, "created": r["created"], "summary": summary, "overview": quick_overview(summary), "ai": jloads(r["ai_json"], None)}


def query_changes(conn: sqlite3.Connection, cid: int, kind: str | None = None, impact: str | None = None, change: str | None = None,
                  chapter: str | None = None, q: str | None = None, nested: bool = True, offset: int = 0, limit: int = 50,
                  sort: str = "document") -> dict[str, Any]:
    where, args = ["compare_id=?"], [cid]
    for col, val in (("kind", kind), ("impact", impact), ("change", change), ("chapter", chapter)):
        if val:
            where.append(f"{col}=?")
            args.append(val)
    if q:
        where.append("(title LIKE ? OR number_from LIKE ? OR number_to LIKE ? OR detail_json LIKE ?)")
        like = f"%{q}%"
        args += [like, like, like, like]
    if not nested:
        where.append("nested=0")
    w = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM compare_change WHERE {w}", args).fetchone()[0]
    order = "score DESC, ord" if sort == "importance" else "ord"
    items = rows(conn, f"SELECT * FROM compare_change WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset])
    for it in items:
        it.update(jloads(it.pop("detail_json"), {}))
        it["nested"] = bool(it["nested"])
        it["id"] = it.pop("ord")
        it.pop("compare_id", None)
    return {"total": total, "items": items}


def delete_comparison(conn: sqlite3.Connection, cid: int) -> bool:
    cur = conn.execute("DELETE FROM spec_compare WHERE id=?", (cid,))
    conn.commit()
    return cur.rowcount > 0


# ----------------------------------------------------------------------------- Markdown report

def _bullet_sentence(s: dict, kind: str) -> str:
    return f"- **{kind}**{' (' + s['strength'] + ')' if s.get('strength') else ''}: {s['text']}"


def export_markdown(conn: sqlite3.Connection, cid: int) -> str:
    comp = get_comparison(conn, cid)
    if comp is None:
        raise KeyError(cid)
    s = comp["summary"]
    f, t = s["from"], s["to"]
    out = [f"# {t['family']}: {f['version']} → {t['version']}", "", "## Overview", "", comp["overview"], ""]
    ai = comp["ai"]
    if ai:
        out += ["## AI summary", "", ai.get("overview", ""), ""]
        if ai.get("breaking"):
            out += ["### Changes that may break existing implementations or tests", ""] + [f"- {b}" for b in ai["breaking"]] + [""]
        if ai.get("highlights"):
            out += ["### Highlights", ""] + [f"- **{h['title']}** ({h.get('impact', 'changed')}): {h['detail']}" for h in ai["highlights"]] + [""]
        if ai.get("upgrade_notes"):
            out += ["### What to do next", ""] + [f"- {u}" for u in ai["upgrade_notes"]] + [""]
        out += ["_AI summaries describe the detected changes; the lists below are the authoritative record._", ""]
    bi = s["by_impact"]
    out += ["## Counts", "", "| Impact | Changes |", "| --- | --- |"] + [f"| {i} | {bi[i]} |" for i in IMPACTS] + [""]
    out += ["## By chapter", "", "| Chapter | Title | Changes | Breaking | New | Changed |", "| --- | --- | --- | --- | --- | --- |"]
    out += [f"| {c['chapter']} | {c['title']} | {c['total']} | {c['breaking']} | {c['new']} | {c['changed']} |" for c in s["chapters"]] + [""]
    out += ["## Changes", ""]
    page, off = 500, 0
    while True:
        res = query_changes(conn, cid, nested=False, offset=off, limit=page)
        for c in res["items"]:
            if c["impact"] == "editorial":
                continue
            num = c.get("number_to") or c.get("number_from") or ""
            label = "Section" if c["kind"] == "section" else "Table"
            out.append(f"### {label} {num}: {c['title']} — {c['change']} · {c['impact']}")
            d = c.get("details", {})
            if c["kind"] == "section" and c["change"] == "modified":
                out += [_bullet_sentence(x, "added") for x in d.get("added", [])[:10]]
                out += [_bullet_sentence(x, "removed") for x in d.get("removed", [])[:10]]
                out += [f"- **modified**: ~~{x['before']}~~ → {x['after']}" for x in d.get("modified", [])[:10]]
            elif c["kind"] == "section":
                out += [f"> {x}" for x in d.get("excerpt", [])[:2]]
            elif c["change"] == "modified":
                out += [f"- row added: {' | '.join(r)}" for r in d.get("rows_added", [])[:10]]
                out += [f"- row removed: {' | '.join(r)}" for r in d.get("rows_removed", [])[:10]]
                for r in d.get("rows_changed", [])[:10]:
                    out += [f"- row {r['key']}: {cell['column']}: ~~{cell['before']}~~ → {cell['after']}" for cell in r["cells"]]
            out.append("")
        off += page
        if off >= res["total"]:
            break
    return "\n".join(out).rstrip() + "\n"
