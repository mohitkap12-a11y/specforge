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
from .normalize import clean, norm_title, nospace_len, parse_bit_range, parse_byte_range, parse_caption, parse_value_range
from .semantics import build_semantics

DOCLING_EXTRACTOR = "docling"
LOW_QUALITY = 0.5
MAX_CONTINUATION_GAP = 3   # pages between two parts of one table that repeat its caption
_NUM_HEADING = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:Annex\s+[A-Z](?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(\S.*)$")
_TOC_LEADER = re.compile(r"(\.{4,}|…{2,}|(?:\.\s){4,})\s*\d*\s*$")
_NESTED_HEAD = {"bit", "byte", "value", "field", "code"}     # singular: compare with _hnorm
_KEYED_ROW = re.compile(r"^(\d{1,4}(?:\s*:\s*\d{1,4})?)\s+([A-Z(].*)$")   # '23:00 Reserved', '5 Ready (RDY): ...'
_CAPTION_LABELS = {"caption", "section_header", "title"}
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


def _hnorm(cell: str) -> str:
    """A header cell for comparison: 'Bytes' and 'Byte', 'Values' and 'Value' are the same column."""
    return re.sub(r"(?<=\w)s\b", "", norm_title(cell))


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
            if _hnorm(first) in _NESTED_HEAD:
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
    last_captioned: Table | None = None  # last captioned top-level table, the one a repeated caption continues
    row_tail: Table | None = None       # the table just closed: a text line right after it may be its last row
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
        """The item's Figure/Table caption. A picture's caption is sometimes kept as a plain child labelled a heading;
        only those labels are read, so a note or cross-reference inside the item is never taken for its caption."""
        refs = list(item.get("captions") or [])
        if _kind(item) == "pictures":
            refs += [r for r in item.get("children") or [] if (_ref(doc, r) or {}).get("label") in _CAPTION_LABELS]
        for ref in refs:
            c = _ref(doc, ref)
            if c is not None and _kind(c) == "texts" and (cap := parse_caption(c.get("text", ""))):
                return cap
        return None

    def own_text_chars(item: dict) -> int:
        """Characters of the texts an item holds besides its cells: a table's captions and footnotes (its cells are
        counted from the grid), a picture's caption and every label drawn inside it."""
        refs = list(item.get("captions") or []) + list(item.get("footnotes") or [])
        if _kind(item) == "pictures":
            refs += list(item.get("children") or [])
        paths = {(r.get("$ref") if isinstance(r, dict) else r) for r in refs}
        return sum(nospace_len((_ref(doc, r) or {}).get("text", "")) for r in paths if r)

    def adopt_between(owner: Table) -> None:
        """Uncaptioned tables found between two parts of one figure are pieces of it Docling cut out (a Value /
        Definition table inside a row): they become its nested tables instead of standing alone."""
        i = tables.index(owner)
        for t in tables[i + 1:]:
            if t.parent_table is None and (t.captioned or t.section != owner.section):
                break                   # another figure (a diagram too) or another section: not part of this one
            if t.parent_table is None:
                old = t.label
                t.label = f"{owner.label}.n{sum(1 for x in tables if x.parent_table == owner.label) + 1}"
                t.parent_table, t.caption, t.parent_cell = owner.label, owner.caption, (len(owner.rows) - 1, 1) if owner.rows else None
                for n_ in notes:
                    if n_.get("node_key") == f"fig:{old}" and n_["kind"] == "uncaptioned_table":
                        n_.update(severity="info", kind="nested_table", node_key=f"fig:{t.label}",
                                  message=f"Nested inside Figure {owner.label} (a part of it between two of its pages)")
                for x in tables:
                    if x.parent_table == old:
                        x.parent_table = t.label

    def nested_part(prev: Table, header: list[str], rows: list[list[str]], page: int, page_to: int) -> None:
        """A part on the next page that starts with its own Bits / Value header continues a sub-table printed inside one of
        prev's rows: it extends the last such sub-table if its keys carry on downwards, else it starts the next one."""
        kids = [t for t in tables if t.parent_table == prev.label]
        cols = [k for k in range(len(header)) if header[k] or any(k < len(r) and r[k] for r in rows)]
        head, body = [header[k] for k in cols], [[r[k] if k < len(r) else "" for k in cols] for r in rows if any(r)]
        last = kids[-1] if kids else None
        if last is not None and [_hnorm(c) for c in last.header] == [_hnorm(c) for c in head]:
            down = _hnorm(head[0]).startswith("bit")        # Bits count down (31:24, 23:16, ...); Bytes, Values, Codes up
            parse = parse_bit_range if down else (parse_byte_range if _hnorm(head[0]).startswith("byte") else parse_value_range)
            span = lambda cell: (lambda x: (max(x), min(x)) if x else None)(parse(cell))  # noqa: E731 - (hi, lo) for every kind
            a = span(last.rows[-1][0]) if last.rows and last.rows[-1] else None
            b = span(body[0][0]) if body and body[0] else None
            if a is None or b is None or (b[0] < a[1] if down else b[1] > a[0]):
                last.rows.extend(body)
                last.page_to = max(last.page_to, page_to)
                last.continued_pages += 1
                return
        lbl = f"{prev.label}.n{len(kids) + 1}"
        tables.append(Table(label=lbl, caption=prev.caption, header=head, rows=body, page_from=page, page_to=page_to,
                            section=prev.section, captioned=False, parent_table=prev.label,
                            parent_cell=(len(prev.rows) - 1, 1) if prev.rows else None, extractor=DOCLING_EXTRACTOR, quality=1.0))
        notes.append({"severity": "info", "kind": "nested_table", "node_key": f"fig:{lbl}", "page": page,
                      "message": f"Nested inside Figure {prev.label} (continued on page {page})"})

    def add_table(item: dict) -> None:
        nonlocal last_top, last_captioned, text_since_table, pending_caption, row_tail
        page, page_to = _page(item), _last_page(item)
        grid = _grid(item)
        acct["table_cells"] += _table_text(item)
        acct["caption"] += own_text_chars(item)
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
        # (a page in between may hold only a diagram or a part Docling did not see as a table: the repeated caption decides)
        prev = last_top
        if cap is not None and last_captioned is not None and cap[1] == last_captioned.label.split("#")[0] \
                and page - last_captioned.page_to <= MAX_CONTINUATION_GAP and last_captioned.section == sec_number() \
                and not any(t.captioned and t.parent_table is None for t in tables[tables.index(last_captioned) + 1:]):
            prev = last_captioned
            adopt_between(prev)
        cont = prev is not None and (
            (cap is not None and cap[1] == prev.label.split("#")[0] and page - prev.page_to <= MAX_CONTINUATION_GAP) or
            (cap is None and not text_since_table and prev.page_to == page - 1 and len(prev.header) == ncol))
        hn = [_hnorm(c) for c in header if c]
        pn = [_hnorm(c) for c in prev.header if c] if prev is not None else []
        if cont and prev is not None and len(hn) >= 2 and hn[0] in _NESTED_HEAD and pn and hn[0] != pn[0]:
            # every row of this part belongs to the sub-table, including any Docling saw as nested inside it
            all_rows = [r for r in ([_text_at(grid, r, k) for k in range(ncol)] for r in range(nh, len(grid))) if any(r)]
            nested_part(prev, header, all_rows, page, page_to)
            prev.page_to = max(prev.page_to, page_to)
            prev.continued_pages += 1
            text_since_table, last_top = False, prev
            row_tail = next(t for t in reversed(tables) if t.parent_table == prev.label)
            return
        if cont and prev is not None:
            # the header repeated on every page is dropped (Docling may add cells of the first row to it: those stay);
            # anything else Docling took for a header is the part's first row
            if pn and hn[:len(pn)] == pn:
                extra, seen = list(header), 0
                for k, c in enumerate(header):
                    if c and seen < len(pn):
                        extra[k], seen = "", seen + 1
                if any(extra):
                    rows.insert(0, extra)
                    body_rows.insert(0, -1)
            elif not set(hn) <= set(pn):
                rows.insert(0, header)
                body_rows.insert(0, -1)
            offset = len(prev.rows)
            prev.rows.extend(r + [""] * (len(prev.header) - len(r)) if len(r) < len(prev.header) else r for r in rows)
            prev.page_to = max(prev.page_to, page_to)
            prev.continued_pages += 1
            parent, parent_rows, last_top = prev, body_rows, prev
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
            if captioned:
                last_captioned = parent
        tail: Table = parent
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
            if e >= len(grid):
                tail = tables[-1]           # the sub-table runs to the bottom of the grid: a row under it is its row
        text_since_table = False
        row_tail = tail

    def trailing_row(text: str) -> bool:
        """'23:00 Reserved' printed right under a Bits / Bytes table is its last row, which Docling left out of the grid."""
        t = row_tail
        if t is None or len(t.header) < 2 or not t.rows or norm_title(t.header[0]).split(" ")[0] not in ("bits", "bit", "bytes", "byte"):
            return False
        m = _KEYED_ROW.match(text)
        if not m or len(text) > 600:
            return False
        bits = norm_title(t.header[0]).startswith("bit")
        parse = parse_bit_range if bits else parse_byte_range
        new, last = parse(m.group(1)), parse(t.rows[-1][0]) if t.rows[-1] else None
        # the key must be the very next range (bits count down, bytes up): '16 bytes of padding follow' after 15:08 is
        # a sentence, which the lower-case word after the number also gives away
        if new is None or last is None or (new[0] != last[1] - 1 if bits else new[1] != last[0] + 1):
            return False
        t.rows.append([m.group(1), m.group(2)] + [""] * (len(t.header) - 2))
        return True

    def add_picture(item: dict) -> None:
        nonlocal pending_caption, row_tail, text_since_table
        row_tail, text_since_table = None, True       # a table after a diagram does not continue the one before it
        page = _page(item)
        acct["figure_text"] += own_text_chars(item)
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
            row_tail = None
            cur = entry_section[id(matched)]
            acct["heading"] += nospace_len(text)
            text_since_table = True
            continue
        cap = parse_caption(text) if label in ("caption", "text", "paragraph") and len(text) < 300 else None
        if cap is not None:
            flush_pending()
            pending_caption = (cap, page)
            row_tail = None
            acct["caption"] += nospace_len(text)
            continue
        if label in ("text", "paragraph") and trailing_row(text):
            acct["table_cells"] += nospace_len(text)
            continue
        row_tail = None
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
