"""Docling -> IR.  Builds the same ParsedSpec as the PDF and DOCX pipelines from a DoclingDocument (the JSON a Docling
conversion saves next to its Markdown, see convert/docling_engine.py), so a spec converted once on the To Markdown page is
parsed without running the layout models again.

Docling's TableFormer rebuilds each table as a grid with row and column spans, which is what the PyMuPDF extractor gets
wrong most often (shifted columns, phantom empty columns, tables it misses). The rest follows the PDF pipeline:

  * sections   the PDF outline (bookmarks) is the authority, as in pdf.py; Docling marks every heading as level 1 and
               prints deep headings without their number, so its section headers are only matched against bookmarks.
               Without an outline (DOCX, PDFs without bookmarks) numbered headings ("5.1.3 Title") make the tree.
  * tables     a Figure/Table caption labels the table; a table split over pages repeats its caption (or none) and is
               stitched to the previous part, its repeated header dropped. A sub-table printed inside a row-spanning
               cell (a Bits / Description table inside a Bytes row) becomes a nested table, as DOCX nested tables do.
  * excluded   page headers and footers (Docling's furniture layer), the table of contents, text inside pictures.
  * accounting every character of the PDF's text layer must land in a section, a table, a caption or the excluded list.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable, Iterator

from .ir import Excluded, ParsedSpec, Section, Table
from .normalize import clean, norm_title, nospace_len, parse_caption
from .semantics import build_semantics

DOCLING_EXTRACTOR = "docling"
LOW_QUALITY = 0.5
_NUM_HEADING = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:Annex\s+[A-Z](?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(\S.*)$")
_TOC_LEADER = re.compile(r"(\.{4,}|…{2,}|(?:\.\s){4,})\s*\d*\s*$")
_NESTED_HEAD = {"bits", "bit", "bytes", "byte", "value", "values", "field", "fields", "code"}
_FURNITURE = {"page_header", "page_footer"}
_TEXT_LABELS = {"text", "paragraph", "list_item", "footnote", "code", "formula", "reference", "checkbox_selected",
                "checkbox_unselected", "title", "section_header", "caption", "key_value_region", "form"}

ProgressFn = Callable[[str, int, int], None]


# ----------------------------------------------------------------------------- document access

def _ref(doc: dict, ref: dict | str | None) -> dict | None:
    """'#/texts/3' -> doc['texts'][3]."""
    path = ref.get("$ref") if isinstance(ref, dict) else ref
    if not path or not path.startswith("#/"):
        return None
    parts = path[2:].split("/")
    node: Any = doc
    try:
        for p in parts:
            node = node[int(p)] if isinstance(node, list) else node[p]
    except (KeyError, IndexError, ValueError):
        return None
    return node


def _kind(item: dict) -> str:
    ref = item.get("self_ref", "")
    return ref.split("/")[1] if ref.count("/") >= 2 else ""


def _page(item: dict, default: int = 1) -> int:
    prov = item.get("prov") or []
    return int(prov[0].get("page_no", default)) if prov else default


def _last_page(item: dict, default: int = 1) -> int:
    prov = item.get("prov") or []
    return max((int(p.get("page_no", default)) for p in prov), default=default)


def _walk(doc: dict, node: dict) -> Iterator[dict]:
    """Body items in reading order. Groups (lists, sections) are entered; tables and pictures are yielded whole (their
    children are their captions and the text drawn inside them)."""
    for ch in node.get("children") or []:
        item = _ref(doc, ch)
        if item is None:
            continue
        kind = _kind(item)
        if kind == "groups":
            yield from _walk(doc, item)
            continue
        yield item
        if kind == "texts" and item.get("children"):
            yield from _walk(doc, item)


def _is_furniture(item: dict) -> bool:
    return item.get("content_layer") == "furniture" or item.get("label") in _FURNITURE


# ----------------------------------------------------------------------------- tables

def _grid(tb: dict) -> list[list[dict | None]]:
    data = tb.get("data") or {}
    grid = data.get("grid")
    if grid:
        return grid
    nr, nc = data.get("num_rows", 0), data.get("num_cols", 0)
    out: list[list[dict | None]] = [[None] * nc for _ in range(nr)]
    for c in data.get("table_cells") or []:
        for r in range(c["start_row_offset_idx"], c["end_row_offset_idx"]):
            for k in range(c["start_col_offset_idx"], c["end_col_offset_idx"]):
                if r < nr and k < nc:
                    out[r][k] = c
    return out


def _origin(cell: dict | None, r: int, k: int) -> bool:
    """True where a (possibly spanning) cell starts: its text goes there, the cells it also covers stay empty."""
    return cell is not None and cell.get("start_row_offset_idx", r) == r and cell.get("start_col_offset_idx", k) == k


def _text_at(grid, r: int, k: int) -> str:
    c = grid[r][k]
    return clean(c.get("text", "")) if _origin(c, r, k) else ""


def _header_count(grid) -> int:
    """Leading rows Docling marked as column headers (every non-empty cell is one)."""
    n = 0
    for row in grid:
        cells = [c for c in row if c is not None and clean(c.get("text", ""))]
        if cells and all(c.get("column_header") for c in cells):
            n += 1
        else:
            break
    return n


def _merge_header(rows: list[list[str]]) -> list[str]:
    """Multi-row headers ('Command Dword 10' over 'Bits' / 'Description') collapse into one row, top to bottom."""
    if not rows:
        return []
    out = []
    for k in range(max(len(r) for r in rows)):
        parts: list[str] = []
        for r in rows:
            t = r[k] if k < len(r) else ""
            if t and t not in parts:
                parts.append(t)
        out.append(" ".join(parts))
    return out


def _nested_blocks(grid, first_body: int) -> list[tuple[int, int, int]]:
    """(parent_row, start, end) grid rows of sub-tables printed inside a row-spanning first-column cell: a row under that
    cell whose remaining cells start with 'Bits' / 'Value' / ... begins the sub-table, which runs until the span ends."""
    out = []
    r = first_body
    while r < len(grid):
        key = grid[r][0] if grid[r] else None
        if key is not None and key.get("start_row_offset_idx", r) < r and len(grid[r]) > 2:
            first = next((_text_at(grid, r, k) for k in range(1, len(grid[r])) if _text_at(grid, r, k)), "")
            if norm_title(first) in _NESTED_HEAD:
                end = min(key.get("end_row_offset_idx", r + 1), len(grid))
                if end - r >= 2:
                    out.append((key["start_row_offset_idx"], r, end))
                    r = end
                    continue
        r += 1
    return out


def _table_text(tb: dict) -> int:
    """Characters in a table, each spanning cell once (the grid repeats a spanning cell in every position it covers)."""
    cells = (tb.get("data") or {}).get("table_cells")
    if cells is None:
        return sum(nospace_len(_text_at(g, r, k)) for g in [_grid(tb)] for r in range(len(g)) for k in range(len(g[r])))
    return sum(nospace_len(c.get("text", "")) for c in cells)


# ----------------------------------------------------------------------------- outline

class _Entry:
    __slots__ = ("level", "title", "page", "number", "name", "consumed")

    def __init__(self, level: int, title: str, page: int, number: str, name: str):
        self.level, self.title, self.page, self.number, self.name = level, title, page, number, name
        self.consumed = False


def _split_heading(title: str) -> tuple[str, str]:
    m = _NUM_HEADING.match(clean(title))
    if m:
        return m.group(1).replace("Annex ", "Annex_"), m.group(2)
    return "", clean(title)


def _entries(doc: dict, toc: list | None) -> tuple[list[_Entry], str]:
    if toc:
        out = []
        for lvl, title, pg, *_ in toc:
            if pg >= 1:
                num, name = _split_heading(title)
                out.append(_Entry(lvl, clean(title), pg, num, name))
        return out, "bookmarks"
    out = []
    for item in _walk(doc, doc.get("body") or {}):
        if item.get("label") != "section_header" or _is_furniture(item):
            continue
        t = clean(item.get("text", ""))
        num, name = _split_heading(t)
        if num and len(t) < 160 and not _TOC_LEADER.search(t):
            out.append(_Entry(num.count(".") + 1 if not num.startswith("Annex") else 1 + num.count("."), t, _page(item), num, name))
    return out, "headings"


# ----------------------------------------------------------------------------- assembly

def parse_docling(doc: dict, family: str, version: str, title: str = "", fmt: str = "pdf", toc: list | None = None,
                  raw_chars: int | None = None, progress: ProgressFn | None = None) -> ParsedSpec:
    """DoclingDocument dict -> ParsedSpec. `toc` is the PDF outline (pymupdf get_toc), `raw_chars` the non-whitespace
    character count of the PDF's text layer (for the accounting check); see parse_docling_file."""
    entries, outline = _entries(doc, toc)
    notes: list[dict] = []
    sections: list[Section] = [Section(number="front", title="Front matter", level=0, page_from=1, parent=None)]
    entry_section: dict[int, Section] = {}
    stack: list[tuple[int, Section]] = []
    seen_numbers: set[str] = set()
    synth = 0
    for e in entries:
        number = e.number
        if not number:
            synth += 1
            number = f"u{synth}"
            notes.append({"severity": "info", "kind": "section_number_synthesized", "node_key": f"sec:{number}",
                          "page": e.page, "message": f"Bookmark '{e.title}' has no section number; assigned {number}"})
        if number in seen_numbers:
            n2 = 2
            while f"{number}#{n2}" in seen_numbers:
                n2 += 1
            notes.append({"severity": "warning", "kind": "duplicate_section_number", "node_key": f"sec:{number}",
                          "page": e.page, "message": f"Section number {number} appears more than once; renamed {number}#{n2}"})
            number = f"{number}#{n2}"
        seen_numbers.add(number)
        while stack and stack[-1][0] >= e.level:
            stack.pop()
        sec = Section(number=number, title=e.name or e.title, level=e.level, page_from=e.page,
                      parent=stack[-1][1].number if stack else None)
        sections.append(sec)
        entry_section[id(e)] = sec
        stack.append((e.level, sec))

    tables: list[Table] = []
    excluded: list[Excluded] = []
    acct = {"body": 0, "heading": 0, "caption": 0, "excluded": 0, "table_cells": 0, "figure_text": 0}
    labels_used: Counter = Counter()
    anon: Counter = Counter()
    cur = sections[0]
    last_top: Table | None = None       # last top-level table, the one a table on the next page may continue
    text_since_table = True
    pending_caption: tuple[tuple, int] | None = None   # a caption printed as text, waiting for its table

    def sec_number() -> str | None:
        return cur.number if cur.number != "front" else None

    def new_label(label: str, page: int) -> str:
        labels_used[label] += 1
        if labels_used[label] == 1:
            return label
        lbl = f"{label}#{labels_used[label]}"
        notes.append({"severity": "warning", "kind": "duplicate_figure_label", "node_key": f"fig:{lbl}", "page": page,
                      "message": f"Figure/Table {label} appears more than once without being a continuation"})
        return lbl

    def figure_only(cap: tuple, page: int) -> None:
        """A captioned figure with no table (a diagram): kept so references to it resolve, as the PDF parser does."""
        lbl = new_label(cap[1], page)
        tables.append(Table(label=lbl, caption=cap[2], header=[], rows=[], page_from=page, page_to=page, section=sec_number(),
                            captioned=True, extractor="none", quality=0.0))
        notes.append({"severity": "info", "kind": "figure_without_table", "node_key": f"fig:{lbl}", "page": page,
                      "message": f"Caption '{cap[0].title()} {cap[1]}' has no extractable table (a picture or diagram)"})

    def flush_pending() -> None:
        nonlocal pending_caption
        if pending_caption is not None:
            figure_only(*pending_caption)
            pending_caption = None

    def caption_of(item: dict) -> tuple | None:
        for ref in item.get("captions") or []:
            c = _ref(doc, ref)
            if c is not None and (cap := parse_caption(c.get("text", ""))):
                return cap
        return None

    def caption_chars(item: dict) -> int:
        return sum(nospace_len((_ref(doc, r) or {}).get("text", "")) for r in item.get("captions") or [])

    def add_table(item: dict) -> None:
        nonlocal last_top, text_since_table, pending_caption
        page, page_to = _page(item), _last_page(item)
        grid = _grid(item)
        acct["table_cells"] += _table_text(item)
        acct["caption"] += caption_chars(item)
        if not grid or not grid[0]:
            return
        cap = caption_of(item)
        if cap is None and pending_caption is not None and pending_caption[1] >= page - 1:
            cap = pending_caption[0]
            pending_caption = None
        else:
            flush_pending()
        nh = _header_count(grid) or 1
        ncol = len(grid[0])
        header = _merge_header([[_text_at(grid, r, k) for k in range(ncol)] for r in range(min(nh, len(grid)))])
        blocks = _nested_blocks(grid, nh)
        inside = {r for _, s, e in blocks for r in range(s, e)}
        body_rows = [r for r in range(nh, len(grid)) if r not in inside]
        rows = [[_text_at(grid, r, k) for k in range(ncol)] for r in body_rows]
        keep = [i for i, row in enumerate(rows) if any(row)]
        rows, body_rows = [rows[i] for i in keep], [body_rows[i] for i in keep]

        # continuation of the previous table on the next page: the same caption again (or "continued"), or no caption
        # at all right at the top of the page with the same columns
        prev = last_top
        cont = prev is not None and prev.page_to >= page - 1 and (
            (cap is not None and cap[1] == prev.label.split("#")[0]) or
            (cap is None and not text_since_table and prev.page_to == page - 1 and len(prev.header) == ncol))
        if cont and prev is not None:
            # the header repeated on every page is dropped; anything else Docling took for a header is the part's first row
            if not ({norm_title(c) for c in header if c} <= {norm_title(c) for c in prev.header if c}):
                rows.insert(0, header)       # what Docling took for a header is the next part's first row
                body_rows.insert(0, -1)
            offset = len(prev.rows)
            prev.rows.extend(r + [""] * (len(prev.header) - len(r)) if len(r) < len(prev.header) else r for r in rows)
            prev.page_to = max(prev.page_to, page_to)
            prev.continued_pages += 1
            parent, parent_rows = prev, body_rows
        else:
            if cap is not None:
                lbl, caption, captioned = new_label(cap[1], page), cap[2], True
            else:
                anon[page] += 1
                lbl, caption, captioned = f"p{page}t{anon[page]}", "", False
                notes.append({"severity": "warning", "kind": "uncaptioned_table", "node_key": f"fig:{lbl}", "page": page,
                              "message": "Table without a Figure/Table caption"})
            parent = Table(label=lbl, caption=caption, header=header, rows=rows, page_from=page, page_to=page_to,
                           section=sec_number(), captioned=captioned, extractor=DOCLING_EXTRACTOR, quality=_quality([header] + rows))
            tables.append(parent)
            if parent.quality < LOW_QUALITY:
                notes.append({"severity": "warning", "kind": "low_table_quality", "node_key": f"fig:{lbl}", "page": page,
                              "message": f"Table quality {parent.quality:.2f} is low; check it against the PDF"})
            last_top, offset, parent_rows = parent, 0, body_rows
        for n, (prow, s, e) in enumerate(blocks, 1):
            sub_head = [_text_at(grid, s, k) for k in range(1, ncol)]
            sub_rows = [[_text_at(grid, r, k) for k in range(1, ncol)] for r in range(s + 1, e)]
            cols = [k for k in range(ncol - 1) if sub_head[k] or any(r[k] for r in sub_rows)]
            row_idx = offset + parent_rows.index(prow) if prow in parent_rows else None
            lbl = f"{parent.label}.n{sum(1 for t in tables if t.parent_table == parent.label) + 1}"
            tables.append(Table(label=lbl, caption=parent.caption, header=[sub_head[k] for k in cols],
                                rows=[[r[k] for k in cols] for r in sub_rows if any(r)], page_from=page, page_to=page_to,
                                section=parent.section, captioned=False, parent_table=parent.label,
                                parent_cell=(row_idx, 1) if row_idx is not None else None, extractor=DOCLING_EXTRACTOR, quality=1.0))
            notes.append({"severity": "info", "kind": "nested_table", "node_key": f"fig:{lbl}", "page": page,
                          "message": f"Nested inside Figure {parent.label}"})
        text_since_table = False

    def add_picture(item: dict) -> None:
        nonlocal pending_caption
        page = _page(item)
        acct["caption"] += caption_chars(item)
        for ch in item.get("children") or []:      # labels drawn inside a diagram
            c = _ref(doc, ch)
            if c is not None and _kind(c) == "texts" and not any((r.get("$ref") if isinstance(r, dict) else r) == c.get("self_ref") for r in item.get("captions") or []):
                acct["figure_text"] += nospace_len(c.get("text", ""))
        cap = caption_of(item)
        if cap is None and pending_caption is not None:
            cap, pending_caption = pending_caption[0], None
        else:
            flush_pending()
        if cap is not None:
            figure_only(cap, page)

    pending_entries: list[_Entry] = []
    ei = 0
    items = list(_walk(doc, doc.get("body") or {}))
    picture_children = {(_ref(doc, ch) or {}).get("self_ref") for p in doc.get("pictures") or [] for ch in p.get("children") or []}
    table_children = {(_ref(doc, ch) or {}).get("self_ref") for t in doc.get("tables") or [] for ch in t.get("children") or []}
    for n, item in enumerate(items):
        if progress and n % 500 == 0:
            progress("assemble", n, len(items))
        kind, label = _kind(item), item.get("label", "")
        page = _page(item)
        while ei < len(entries) and entries[ei].page <= page:
            pending_entries.append(entries[ei])
            ei += 1
        if item.get("self_ref") in picture_children or item.get("self_ref") in table_children:
            continue                                     # counted with their picture / table
        if kind == "tables":
            if label == "document_index":                # the table of contents as a table
                excluded.append(Excluded(page, "toc", f"table of contents ({len(_grid(item))} rows)"))
                acct["excluded"] += _table_text(item)
                continue
            add_table(item)
            continue
        if kind == "pictures":
            add_picture(item)
            continue
        if kind != "texts" or _is_furniture(item):
            continue
        text = clean(item.get("text", ""))
        if not text:
            continue
        if _TOC_LEADER.search(text):
            excluded.append(Excluded(page, "toc", text))
            acct["excluded"] += nospace_len(text)
            continue
        # heading: a bookmark of this page (or an earlier one not found yet) with this title, with or without its number
        nl = norm_title(text)
        matched = None
        if len(text) < 200:
            for k, e in enumerate(pending_entries):
                nt, nn = norm_title(e.title), norm_title(e.name)
                if nl == nt or (label in ("section_header", "title") and nl and (nl == nn or (len(nl) >= 3 and (nt.startswith(nl) or (e.number and nn.startswith(nl)))))):
                    matched = pending_entries.pop(k)
                    break
        if matched is not None:
            matched.consumed = True
            flush_pending()
            cur = entry_section[id(matched)]
            acct["heading"] += nospace_len(text)
            text_since_table = True
            continue
        cap = parse_caption(text) if label in ("caption", "text", "paragraph") and len(text) < 300 else None
        if cap is not None:
            flush_pending()
            pending_caption = (cap, page)
            acct["caption"] += nospace_len(text)
            continue
        cur.paragraphs.append(text)
        acct["body"] += nospace_len(text)
        text_since_table = True
    flush_pending()

    furniture = [t for t in doc.get("texts") or [] if _is_furniture(t)]
    for t in furniture:
        text = clean(t.get("text", ""))
        if text:
            excluded.append(Excluded(_page(t), "header/footer", text))
            acct["excluded"] += nospace_len(text)

    for e in entries:
        if not e.consumed:
            notes.append({"severity": "error", "kind": "heading_not_found", "node_key": f"sec:{e.number or e.title}",
                          "page": e.page, "message": f"Bookmark '{e.title}' (page {e.page}) was not found in the page text"})
    pages = len(doc.get("pages") or {}) or max((s.page_from for s in sections), default=1)
    for i, s in enumerate(sections):
        nxt = next((x.page_from for x in sections[i + 1:] if x.level <= s.level), None)
        s.page_to = nxt if nxt is not None else pages
    accounted = sum(acct.values())
    spec = ParsedSpec(family=family, version=version, title=title or (doc.get("name") or ""), fmt=fmt, pages=pages,
                      sections=sections, tables=tables, requirements=[], references=[], excluded=excluded,
                      raw_char_count=raw_chars if raw_chars is not None else accounted, accounted_char_count=accounted, notes=notes,
                      stats={"engine": "docling", "char_accounting": acct, "toc_entries": len(entries), "outline": outline,
                             "extractors": dict(Counter(t.extractor for t in tables))})
    build_semantics(spec)
    if progress:
        progress("assemble", len(items), len(items))
    return spec


def _quality(rows: list[list[str]]) -> float:
    from .pdf import _table_quality

    return _table_quality(rows)


def parse_docling_file(json_path: str, source_path: str | None, family: str, version: str,
                       progress: ProgressFn | None = None) -> ParsedSpec:
    """Parse a saved DoclingDocument; the original PDF (when there is one) supplies the outline and the text-layer count."""
    import json
    from pathlib import Path

    with open(json_path, encoding="utf-8") as f:
        doc = json.load(f)
    toc, raw, title, fmt = None, None, "", "docx"
    if source_path and Path(source_path).suffix.lower() == ".pdf":
        import pymupdf

        with pymupdf.open(source_path) as pdf:
            toc = pdf.get_toc(simple=True)
            raw = sum(nospace_len(p.get_text("text")) for p in pdf)
            title = (pdf.metadata or {}).get("title") or ""
        fmt = "pdf"
    return parse_docling(doc, family, version, title or Path(source_path or json_path).name, fmt, toc, raw, progress)
