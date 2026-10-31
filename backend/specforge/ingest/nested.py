"""Nested tables of a specification PDF: a field's bit table, a bit's value table, drawn inside the Description cell of their
parent row (Identify Controller CTRATT, bytes 99:96: its 32 bits, and bit 24:23's values 00b-11b).

PyMuPDF's table finder returns a nested table in one of two ways, and both are read:

  separate   a table of its own whose box lies inside its parent table's box (CMIC, p. 367 of NVMe Base 2.4): it belongs to
             the parent row its top edge falls in; the parent's own text is what lies outside it in that cell (the parser
             also has the nested table's text flattened into the cell, which is left out)
  merged     extra columns to the right of the parent's Description column (CTRATT on pp. 369-372, where the bit table goes on
             over pages): rows with text in those columns are nested rows; a row starting further right is in a table inside
             the last bit (its values)

A nested table goes on over a page when the parent's first cell is empty at the top of the table there: it then belongs to the
field, and bit, the previous page ended with. A candidate whose rows do not start with bits or values (a diagram's boxes, a
list drawn in a cell) is not a nested table.

  page_facts(page, tables, pno)   per page, from the tables PyMuPDF found on it: plain data, so it can come from a worker
  assemble(facts)                 over the pages in order: the nested tables, their pieces joined, with their parents
  attach(spec, nested)            into a parsed spec: each nested table becomes a Table with parent_table / parent_cell
                                  set (as DOCX nested tables are); the parent row's description becomes its own text, and
                                  the copies the page-by-page extraction left (rows in the parent, uncaptioned pieces) go
  strip(top)                      a top table's rows without its nested content, for a writer that prints the nested
                                  tables on their own (the To Markdown converter)
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .normalize import clean

CAPTION = re.compile(r"^(Figure|Table)\s+(\d+[A-Za-z]?)\s*[:.]\s*(.+)$")
# the first cell of a row of a bit or value table: bits (31:25, 0), a value (00b, 1h, 0h to 7h) or a heading
KEY = re.compile(r"^(\d{1,4}(\s*:\s*\d{1,4})?|[0-9A-Fa-f]{1,4}[bh](\s*(to|-|–)\s*[0-9A-Fa-f]{1,4}[bh])?|bits?|value|values)$", re.I)
HEADER_WORD = re.compile(r"^(bits?|bytes?|value|values|offset|field|name|description|definition|code|type|state)$", re.I)
FLAT = re.compile(r"\s(Bits\s+Description|Value\s+Definition)\b.*$")     # a nested table flattened into its parent's text
TOL = 2.0


@dataclass
class Nested:
    figure: str                      # "338"
    caption: str                     # the figure's caption, without "Figure 338:"
    unit: str                        # heading of the parent's key column: "Bytes", "Bits"
    field: str                       # the parent row's key: "99:96"
    field_text: str                  # the parent row's own description
    bit: str | None = None           # a table inside a bit of a nested table: that bit ("24:23")
    bit_text: str = ""
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    pieces: list[str] = field(default_factory=list)  # text joined to a row (its description went on below it)

    @property
    def label(self) -> str:
        """Stable across versions of a spec (the parent's key, not a row number): 338.99:96, 338.99:96.24:23."""
        return f"{self.figure}.{self.field}" + (f".{self.bit}" if self.bit else "")


def _inside(a, b) -> bool:
    return a[0] >= b[0] - TOL and a[1] >= b[1] - TOL and a[2] <= b[2] + TOL and a[3] <= b[3] + TOL


def _area(b) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def _first(r: list[str]) -> str:
    return next((x for x in r if x), "")


def _worded(cells: list[str]) -> bool:
    return any(re.search(r"[A-Za-z]{2}", c) for c in cells)


def _is_header(cells: list[str]) -> bool:
    return bool(cells) and all(HEADER_WORD.match(c) for c in cells if c)


def _caption_above(blocks: list, y: float) -> tuple[str, str] | None:
    best = None
    for b in blocks:
        if b[3] <= y + TOL:
            m = CAPTION.match(clean(b[4]))
            if m and (best is None or b[3] > best[0]):
                best = (b[3], m.group(2), m.group(3))
    return (best[1], best[2]) if best else None


def _text_in(page, rect) -> str:
    import pymupdf

    if rect[3] - rect[1] < 1 or rect[2] - rect[0] < 1:
        return ""
    return clean(page.get_text("text", clip=pymupdf.Rect(rect)))


def _laid_out(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """(header, rows) of a nested table: empty cells dropped, a header row recognised by its words."""
    cells = [[c for c in r if c] for r in rows]
    cells = [c for c in cells if c]
    if cells and _is_header(cells[0]):
        return cells[0], cells[1:]
    return [], cells


def page_facts(page, tables: list, pno: int) -> dict[str, Any]:
    """What the tables PyMuPDF found on one page hold, as plain data: every top-level table (caption, key and Description
    columns, rows, its own text of rows that hold a nested table) and the nested tables found as tables of their own."""
    blocks = page.get_text("blocks")
    boxes = [tuple(float(v) for v in t.bbox) for t in tables]
    ex = [[[clean(c) for c in (r or [])] for r in t.extract()] for t in tables]
    parent_of: dict[int, int] = {}
    for i, b in enumerate(boxes):
        outer = [j for j, o in enumerate(boxes) if j != i and _inside(b, o) and _area(o) > _area(b)]
        if outer:
            parent_of[i] = min(outer, key=lambda j: _area(boxes[j]))
    tops = []
    for ti in (i for i in range(len(tables)) if i not in parent_of):
        rows = ex[ti]
        if len(rows) < 2 or not any(rows[0]):
            continue
        D = max(k for k, c in enumerate(rows[0]) if c)          # the Description column: the last heading
        # the key column (Bytes / Bits): the first holding data in rows with words (not the footnote row "1 | 1 | 1" under
        # the header); the header is often drawn one column to the right of it
        K = min((next(c for c, x in enumerate(r) if x) for r in rows[1:] if _worded(r[:D + 1])), default=0)
        top = {"index": ti, "figure": _caption_above(blocks, boxes[ti][1]), "D": D, "K": K,
               "unit": next((h for h in rows[0][K:K + 2] if h), ""), "rows": rows, "own": {}, "nested": [], "inner": {}}
        tops.append(top)
        # a nested row in extra columns: its description from the page text across the rest of the row, not from the cells
        # (the last letters of wrapped lines often land in a column of their own)
        prow_boxes = tables[ti].rows
        for k, r in enumerate(rows[1:], start=1):
            filled = [D + 1 + c for c, x in enumerate(r[D + 1:]) if x]
            if not filled or k >= len(prow_boxes):
                continue
            key = r[filled[0]] if KEY.match(r[filled[0]]) and len(filled) >= 2 else None
            start = filled[1] if key else filled[0]          # a row going on from the one above has no key
            cells = prow_boxes[k].cells
            db = cells[start] if start < len(cells) else None
            if db:
                rb = prow_boxes[k].bbox
                y1 = min(rb[3], prow_boxes[k + 1].bbox[1]) if k + 1 < len(prow_boxes) else rb[3]   # a row box can run into the next
                txt = _text_in(page, (db[0], rb[1] + 2, boxes[ti][2], y1 - 2))      # inset: not the row above's descenders
                if txt:
                    top["inner"][k] = [key, txt] if key else [txt]
        # nested tables found as tables of their own: one level down first, then tables inside those
        level1: dict[int, int] = {}
        for i, j in sorted(parent_of.items(), key=lambda ij: ij[1] != ti):
            if j != ti and parent_of.get(j) != ti:
                continue
            body = ex[i][1:]
            firsts = [_first(r) for r in body]
            if not firsts or sum(bool(KEY.match(x)) for x in firsts) < 0.6 * len(firsts):
                continue                                           # a diagram's boxes, a list drawn in a cell
            b = boxes[i]
            prow = next((k for k, rr in enumerate(tables[j].rows) if rr.bbox[1] - TOL <= b[1] < rr.bbox[3]), None)
            if prow is None:
                continue
            header, nrows = _laid_out(ex[i])
            entry = {"index": i, "row": prow, "header": header, "rows": nrows, "in": None}
            if j == ti:
                level1[i] = len(top["nested"])
                r = rows[prow]
                if prow < len(r) and r[K]:                         # the parent row starts here: its own text, outside the table
                    di = max((c for c, x in enumerate(r[:D + 1]) if x), key=lambda c: len(r[c]), default=None)
                    cells = tables[ti].rows[prow].cells
                    cell = cells[di] if di is not None and di < len(cells) and cells[di] else None
                    if cell:
                        own = " ".join(x for x in (_text_in(page, (cell[0], cell[1], cell[2], b[1])),
                                                   _text_in(page, (cell[0], b[3], cell[2], cell[3]))) if x)
                        if own:
                            top["own"][prow] = own
            elif j in level1:
                entry["in"] = level1[j]
                entry["bit"] = _first(ex[j][prow]) if prow < len(ex[j]) else ""
                entry["bit_text"] = next((x for x in reversed(ex[j][prow]) if x), "") if prow < len(ex[j]) else ""
            else:
                continue
            top["nested"].append(entry)
    # the tables taken as nested (a writer prints them under their parent instead), not every table drawn inside another
    return {"page": pno + 1, "tops": tops, "consumed": sorted(e["index"] for top in tops for e in top["nested"])}


class _Tables:
    """Nested tables as they are found, keyed by (figure, field, bit): pieces from later pages join the same table."""

    def __init__(self) -> None:
        self.by_key: dict[tuple, Nested] = {}

    def add(self, fig, unit, field_: dict, bit: dict | None, page: int, header: list[str], rows: list[list[str]]) -> tuple:
        key = (fig[0], field_["key"], bit["key"] if bit else None)
        n = self.by_key.get(key)
        if n is None:
            n = self.by_key[key] = Nested(figure=fig[0], caption=fig[1], unit=unit, field=field_["key"], field_text=field_["text"],
                                          bit=bit["key"] if bit else None, bit_text=bit["text"] if bit else "")
        if header and not n.header:
            n.header = header
        if page not in n.pages:
            n.pages.append(page)
        seen = {tuple(r) for r in n.rows}
        n.rows += [r for r in rows if tuple(r) not in seen and r != n.header]
        return key

    def append_text(self, key: tuple | None, text: str) -> None:
        n = self.by_key.get(key) if key else None
        if n is not None and n.rows:
            n.rows[-1][-1] = clean(n.rows[-1][-1] + " " + text)
            n.pieces.append(text)


def assemble(facts: list[dict[str, Any]]) -> list[Nested]:
    """The nested tables of a document from its pages' facts (in page order), each piece joined to the table it continues."""
    T = _Tables()
    carry: dict[str, dict] = {}          # figure -> where its table ended on the previous page: field, bit, page, last table
    for f in facts:
        pg = f["page"]
        for top in f["tops"]:
            fig = top["figure"]
            if fig is None:
                continue
            rows, D, K, unit = top["rows"], top["D"], top["K"], top["unit"]
            cont = carry.get(fig[0]) if carry.get(fig[0], {}).get("page") == pg - 1 else None
            field_ = cont["field"] if cont else None
            bit = cont["bit"] if cont else None
            last = cont.get("last") if cont else None             # the table the previous page added to last
            lvl1_col = None
            row_field: dict[int, dict] = {}
            for k, r in enumerate(rows[1:], start=1):
                if K < len(r) and r[K] and KEY.match(r[K]) and not HEADER_WORD.match(r[K]) and _worded(r[K + 1:D + 1]):
                    text = top["own"].get(k) or max(r[K + 1:D + 1], key=len, default="")
                    field_ = {"key": r[K], "text": FLAT.sub("", text)}
                    bit, lvl1_col = None, None
                if field_ is not None:
                    row_field[k] = field_
                inner = r[D + 1:]
                filled = [c for c, x in enumerate(inner) if x]
                if not filled or field_ is None:
                    continue
                cells = top["inner"].get(k) or [x for x in inner if x]
                if not KEY.match(cells[0]):                         # the description of the row above goes on
                    T.append_text(last, " ".join(cells))
                    continue
                if lvl1_col is None or filled[0] <= lvl1_col:       # a row of the field's own nested table
                    lvl1_col = filled[0] if lvl1_col is None else min(lvl1_col, filled[0])
                    if _is_header(cells):
                        last = T.add(fig, unit, field_, None, pg, cells, [])
                        continue
                    bit = {"key": cells[0], "text": cells[-1] if len(cells) > 1 else ""}
                    last = T.add(fig, unit, field_, None, pg, [], [cells])
                else:                                               # further right: a table inside the last bit
                    if _is_header(cells):
                        last = T.add(fig, unit, field_, bit, pg, cells, [])
                        continue
                    last = T.add(fig, unit, field_, bit, pg, [], [cells])
            keys: dict[int, tuple] = {}
            for e in top["nested"]:
                if e["in"] is None:
                    fd = row_field.get(e["row"])
                    if fd is None:
                        continue
                    keys[len(keys)] = last = T.add(fig, unit, fd, None, pg, e["header"], e["rows"])
                    carry[fig[0]] = {"field": fd, "bit": None, "page": pg, "last": last}
                else:
                    owner = keys.get(e["in"])
                    if owner is None:
                        continue
                    n = T.by_key[owner]
                    last = T.add(fig, unit, {"key": n.field, "text": n.field_text}, {"key": e["bit"], "text": e["bit_text"]},
                                 pg, e["header"], e["rows"])
            if field_ is not None:
                carry[fig[0]] = {"field": field_, "bit": bit, "page": pg, "last": last}
    out = []
    for n in T.by_key.values():
        n.rows = _normalize(n.rows, n.pieces)
        if n.rows:
            if not n.header:
                n.header = ["Bits", "Description"] if n.bit is None and n.unit.lower().startswith("byte") else ["Value", "Definition"]
            width = len(n.header)
            n.rows = [(r[:width - 1] + [" ".join(r[width - 1:])] if len(r) > width else r + [""] * (width - len(r))) for r in n.rows]
            out.append(n)
    return out


def _normalize(rows: list[list[str]], pieces: list[str]) -> list[list[str]]:
    """A row that does not start with a bit or value is the description of the row above going on."""
    out: list[list[str]] = []
    for r in rows:
        cells = [c for c in r if c]
        if not cells:
            continue
        if out and not KEY.match(cells[0]):
            out[-1][-1] = clean(out[-1][-1] + " " + " ".join(cells))
            pieces.append(" ".join(cells))
            continue
        out.append(cells)
    return out


def _sig(cells: list[str]) -> tuple[str, str]:
    """A row's identity across extractors (PyMuPDF / pdfplumber cells differ in spacing and empty cells): its first and last
    non-empty cells, letters and digits only."""
    cs = [c for c in cells if c]
    if not cs:
        return "", ""
    rest = max(cs[1:], key=len) if len(cs) > 1 else cs[0]
    return re.sub(r"[^a-z0-9]", "", cs[0].lower())[:24], re.sub(r"[^a-z0-9]", "", rest.lower())[:80]


class _Sigs:
    """Rows to recognise: the same first cell, and descriptions alike: one starting with the other (a note joined to the row
    in one extraction and not in the other) or nearly the same (a letter of a wrapped line lost to another column)."""

    def __init__(self, rows) -> None:
        self.by_first: dict[str, list[str]] = defaultdict(list)
        for r in rows:
            a, b = _sig(r)
            if a:
                self.by_first[a].append(b)

    def __contains__(self, row: list[str]) -> bool:
        a, b = _sig(row)
        return bool(a) and any(x == b or (min(len(x), len(b)) >= 6 and (x.startswith(b) or b.startswith(x)
                                                                      or SequenceMatcher(None, x[:60], b[:60]).ratio() >= 0.85))
                               for x in self.by_first.get(a, ()))


def _longest(r: list[str]) -> int | None:
    return max((c for c, x in enumerate(r) if x), key=lambda c: len(r[c]), default=None)


def attach(spec, found: list[Nested]) -> dict[str, int]:
    """Put the nested tables into a parsed spec (before its normative statements are read from the tables). Returns counts."""
    from .ir import Table

    stats = {"nested": 0, "unplaced": 0, "rows_removed": 0, "tables_removed": 0}
    figures = {n.figure for n in found}
    nested_rows = _Sigs(x for n in found for x in [*n.rows, n.header, *([p] for p in n.pieces)])
    # first the copies the page-by-page extraction left: rows of the parent tables, and uncaptioned pieces (or tables the
    # parser itself saw as nested) holding only nested rows; a field row of the parent itself (its key in the first column,
    # its other columns filled) is never one
    drop = set()
    by_label = {t.label: t for t in spec.tables}
    for t in spec.tables:
        # a table the parser saw as nested that only repeats its parent's rows: PyMuPDF's copy of a table pdfplumber
        # extracted again (the outer table, not a nested one)
        pt = by_label.get(t.parent_table) if t.parent_table else None
        if pt is not None and t.rows and all(r in _Sigs(pt.rows) for r in t.rows):
            drop.add(id(t))
            stats["tables_removed"] += 1
            continue
        if not (t.label.split("#")[0] in figures or not t.captioned or t.parent_table):
            continue
        keep = [r for r in t.rows if not (r in nested_rows and not (r and r[0] and len([c for c in r if c]) > 2))]
        removed = len(t.rows) - len(keep)
        if not removed:
            continue
        stats["rows_removed"] += removed
        if not keep and (not t.captioned or t.parent_table):
            drop.add(id(t))
            stats["tables_removed"] += 1
        else:
            t.rows = keep
    spec.tables = [t for t in spec.tables if id(t) not in drop]
    # then each nested table under its parent row
    tops: dict[str, list] = defaultdict(list)
    for t in spec.tables:
        if not t.parent_table:
            tops[t.label.split("#")[0]].append(t)
    used = {t.label for t in spec.tables}
    made: dict[tuple[str, str], Any] = {}
    for n in sorted(found, key=lambda n: n.bit is not None):          # bit tables before the value tables inside them
        if n.bit is None:
            hit = next(((t, i) for t in tops.get(n.figure, []) for i, r in enumerate(t.rows)
                        if _first(r) == n.field and _worded(r[1:])), None)
        else:
            pt = made.get((n.figure, n.field))
            hit = next(((pt, i) for i, r in enumerate(pt.rows) if _first(r) == n.bit), None) if pt else None
        if hit is None:
            stats["unplaced"] += 1
            continue
        t, i = hit
        r = t.rows[i]
        di = _longest(r)
        if di is not None:                    # its own text, without the nested table flattened into it
            r[di] = n.field_text if n.bit is None and n.field_text else FLAT.sub("", r[di])
        label = n.label
        k = 2
        while label in used:
            label, k = f"{n.label}#{k}", k + 1
        used.add(label)
        whose = _name(n.bit_text if n.bit else n.field_text) or (n.bit if n.bit else n.field)
        child = Table(label=label, caption=f"{whose}: {'values' if n.header[:1] == ['Value'] else 'bits'}", header=n.header,
                      rows=n.rows, page_from=min(n.pages), page_to=max(n.pages), section=t.section, captioned=False,
                      parent_table=t.label, parent_cell=(i, di if di is not None else len(r) - 1), extractor="pymupdf-nested",
                      quality=1.0)
        spec.tables.append(child)
        if n.bit is None:
            made[(n.figure, n.field)] = child
        stats["nested"] += 1
    spec.stats["nested_tables"] = stats
    return stats


def _name(desc: str) -> str:
    """"Controller Attributes (CTRATT): ..." -> "Controller Attributes (CTRATT)"."""
    m = re.match(r"^(.{2,90}?\([A-Za-z0-9/_-]{2,16}\))\s*:", desc or "")
    return m.group(1) if m else ""


def strip(top: dict[str, Any]) -> list[list[str]]:
    """A top table's rows without its nested content: the columns right of Description and the rows only they filled are
    dropped, and a parent row's description is its own text."""
    D = top["D"]
    out = []
    for k, r in enumerate(top["rows"]):
        cut = list(r[:D + 1])
        if k and not any(cut):
            continue
        if k in top["own"]:
            di = max((c for c, x in enumerate(cut) if x), key=lambda c: len(cut[c]), default=None)
            if di is not None:
                cut[di] = top["own"][k]
        elif k:
            di = max((c for c, x in enumerate(cut) if x), key=lambda c: len(cut[c]), default=None)
            if di is not None:
                cut[di] = FLAT.sub("", cut[di])
        out.append(cut)
    return out


def caption(n: Nested) -> str:
    """How a writer titles a nested table: "Figure 338 – Bytes 99:96, Controller Attributes (CTRATT): bits" (a caption
    line of its figure, as a reader of the Markdown takes it)."""
    unit = (n.unit or "Bytes").rstrip("s") + "s" if n.unit else "Bytes"
    head = f"Figure {n.figure} – {unit} {n.field}, {_name(n.field_text) or n.field}"
    if n.bit:
        return f"{head}, bits {n.bit}, {_name(n.bit_text) or n.bit}: values"
    return f"{head}: {'values' if n.header[:1] == ['Value'] else 'bits'}"


def find_nested(path: str) -> list[Nested]:
    """Every nested table of a PDF (one pass of PyMuPDF's table finder over it)."""
    import pymupdf

    doc = pymupdf.open(path)
    try:
        return assemble([page_facts(p, p.find_tables().tables, i) for i, p in enumerate(doc)])
    finally:
        doc.close()
