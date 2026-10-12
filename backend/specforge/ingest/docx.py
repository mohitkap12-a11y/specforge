"""DOCX -> IR.  Reads OOXML directly so merged cells and nested tables are exact.

Section numbers come from heading text when present, otherwise from replaying heading-level counters
(Word does not store auto numbers).  Cross references use regex like the PDF path; native REF-field
resolution is not implemented yet.
"""
from __future__ import annotations

import os
import re
from typing import Iterator

from .ir import ParsedSpec, Section, Table
from .normalize import clean, detect_identity, norm_title, nospace_len, parse_caption
from .semantics import build_semantics

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NUM_HEADING = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(\S.*)$")


def _text(el) -> str:
    out = []
    for node in el.iter():
        if node.tag == W + "t" and node.text:
            out.append(node.text)
        elif node.tag == W + "tab":
            out.append(" ")
        elif node.tag in (W + "br", W + "cr"):
            out.append(" ")
    return clean("".join(out))


def _para_style(p) -> str:
    ppr = p.find(W + "pPr")
    if ppr is not None:
        st = ppr.find(W + "pStyle")
        if st is not None:
            return st.get(W + "val", "") or ""
    return ""


def _heading_level(style: str) -> int | None:
    m = re.match(r"(?i)^heading\s*(\d+)$", style.replace("_", " ").strip())
    return int(m.group(1)) if m else None


def _grid(tbl) -> tuple[list[list[str]], list[dict]]:
    """Return rectangular grid of cell text (spans filled) and nested table descriptors."""
    rows: list[list[str]] = []
    nested: list[dict] = []
    vmerge_open: dict[int, tuple[int, int]] = {}
    for ri, tr in enumerate(tbl.findall(W + "tr")):
        row: list[str] = []
        col = 0
        for tc in tr.findall(W + "tc"):
            tcpr = tc.find(W + "tcPr")
            span, vm = 1, None
            if tcpr is not None:
                gs = tcpr.find(W + "gridSpan")
                if gs is not None:
                    span = int(gs.get(W + "val", "1"))
                v = tcpr.find(W + "vMerge")
                if v is not None:
                    vm = v.get(W + "val", "continue")
            paras = []
            for child in tc:
                if child.tag == W + "p":
                    t = _text(child)
                    if t:
                        paras.append(t)
                elif child.tag == W + "tbl":
                    nested.append({"el": child, "row": ri, "col": col})
            text = " ".join(paras)
            if vm == "continue" and col in vmerge_open:
                srow, scol = vmerge_open[col]
                if text:
                    rows[srow][scol] = clean(rows[srow][scol] + " " + text)
                text = ""
            elif vm == "restart":
                vmerge_open[col] = (ri, len(row))
            else:
                vmerge_open.pop(col, None)
            row.append(text)
            for _ in range(span - 1):
                row.append("")  # spanned cells stay empty (value lives in first cell)
            col += span
        rows.append(row)
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    return rows, nested


def _iter_block(body) -> Iterator:
    for child in body:
        if child.tag in (W + "p", W + "tbl"):
            yield child


def parse_docx(path: str, family: str | None = None, version: str | None = None, progress=None) -> ParsedSpec:
    import docx

    d = docx.Document(path)
    body = d.element.body
    paragraphs = [_text(p) for p in body.iter(W + "p")]
    first = "\n".join(paragraphs[:60])
    f2, v2 = detect_identity(first, os.path.basename(path))
    family = family or f2 or "unknown"
    version = version or v2 or "0"
    title = clean(d.core_properties.title or os.path.basename(path))

    front = Section(number="front", title="Front matter", level=0, page_from=1)
    sections: list[Section] = [front]
    tables: list[Table] = []
    notes: list[dict] = []
    cur = front
    counters: list[int] = []
    stack: list[Section] = []
    pending_caption: tuple[str, str, bool] | None = None
    labels: dict[str, int] = {}
    raw_chars = 0
    acct = 0
    anon = 0

    def next_number(level: int) -> str:
        while len(counters) < level:
            counters.append(0)
        del counters[level:]
        counters[level - 1] += 1
        return ".".join(str(c) for c in counters)

    def add_table(tbl, parent_label: str | None, parent_cell, caption) -> None:
        nonlocal anon, acct
        rows, nested = _grid(tbl)
        if caption:
            kind, label, ttl, cont = caption
            n = labels.get(label, 0) + 1
            labels[label] = n
            lbl = label if n == 1 else f"{label}#{n}"
            if n > 1 and not cont:
                notes.append({"severity": "warning", "kind": "duplicate_figure_label", "node_key": f"fig:{lbl}",
                              "page": None, "message": f"Figure/Table {label} appears more than once"})
        else:
            anon += 1
            lbl, ttl = f"t{anon}", ""
            notes.append({"severity": "warning", "kind": "uncaptioned_table", "node_key": f"fig:{lbl}", "page": None,
                          "message": "Table without a Figure/Table caption"})
        # continuation by label: stitch with previous table of the same label
        if caption and caption[3] and tables:
            base = caption[1]
            prev = next((t for t in reversed(tables) if t.label.split("#")[0] == base), None)
            if prev is not None:
                body_rows = rows[1:] if rows and [norm_title(c) for c in rows[0]] == [norm_title(c) for c in prev.header] else rows
                prev.rows.extend(body_rows)
                prev.continued_pages += 1
                labels[base] -= 1
                acct += sum(nospace_len(c) for r in rows for c in r)
                return
        header = rows[0] if rows else []
        t = Table(label=lbl, caption=ttl, header=header, rows=rows[1:], page_from=1, page_to=1,
                  section=cur.number if cur.number != "front" else None, captioned=bool(caption),
                  parent_table=parent_label, parent_cell=parent_cell, extractor="ooxml", quality=1.0)
        tables.append(t)
        acct += sum(nospace_len(c) for r in rows for c in r)
        for n_ in nested:
            add_table(n_["el"], lbl, (n_["row"], n_["col"]), None)

    blocks = list(_iter_block(body))
    for bi, el in enumerate(blocks):
        if progress and bi % 200 == 0:
            progress("parse", bi, len(blocks))
        if el.tag == W + "p":
            text = _text(el)
            if not text:
                continue
            style = _para_style(el)
            lvl = _heading_level(style)
            if lvl is not None:
                m = _NUM_HEADING.match(text)
                if m:
                    number, name = m.group(1), m.group(2)
                    parts = number.split(".")
                    counters[:] = [int(x) if x.isdigit() else 0 for x in parts][:lvl]
                else:
                    number, name = next_number(lvl), text
                while stack and stack[-1].level >= lvl:
                    stack.pop()
                sec = Section(number=number, title=name, level=lvl, page_from=1,
                              parent=stack[-1].number if stack else None)
                sections.append(sec)
                stack.append(sec)
                cur = sec
                pending_caption = None
                acct += nospace_len(text)
                continue
            cap = parse_caption(text) if (style.lower().startswith("caption") or text[:6].lower() in ("figure", "table ")) else None
            if cap:
                pending_caption = cap
                acct += nospace_len(text)
                continue
            if pending_caption is not None:
                notes.append({"severity": "info", "kind": "figure_without_table", "node_key": f"fig:{pending_caption[1]}",
                              "page": None, "message": f"Caption {pending_caption[1]} has no following table"})
                pending_caption = None
            cur.paragraphs.append(text)
            acct += nospace_len(text)
        else:
            cap, pending_caption = pending_caption, None
            add_table(el, None, None, cap)
    raw_chars = sum(nospace_len(_text(p)) for p in body.iter(W + "p"))
    for s in sections:
        s.page_to = 1
    spec = ParsedSpec(family=family, version=version, title=title, fmt="docx", pages=1, sections=sections, tables=tables,
                      requirements=[], references=[], excluded=[], raw_char_count=raw_chars, accounted_char_count=acct,
                      notes=notes, stats={"toc_entries": len(sections) - 1, "extractors": {"ooxml": len(tables)}})
    build_semantics(spec)
    return spec
