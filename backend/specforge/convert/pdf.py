"""PDF -> Markdown blocks.

Text comes from PyMuPDF with its font information, so structure is inferred from how the page is typeset:
  * headings    PDF bookmarks when present (title + page), else larger font size, else a bold numbered title ("5.1.2 Name")
  * paragraphs  lines are joined unless the vertical gap or a list marker starts a new one; hyphenated line breaks are healed
  * lists       bullet / numbered / lettered markers, nesting from the marker indent
  * code        monospace lines become fenced blocks, monospace spans inline code
  * tables      PyMuPDF table finder; text inside a table is not repeated as prose; a table that continues on the next page is stitched
  * extras      bold / italic, hyperlinks, repeating headers / footers and page numbers removed, table-of-contents dot leaders dropped
Single-column reading order is assumed; multi-column layouts are read column by column only as far as PyMuPDF orders them.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .md import Block, Code, Heading, Item, Para, Seg, Table, escape_text

_BULLET = re.compile(r"^([•●▪■◦‣·–—\-])\s+(?=\S)")
_NUMBER = re.compile(r"^(\d{1,3}[.)]|\(?[A-Za-z][.)]|\((?:\d{1,3}|[ivxIVX]{1,5})\))\s+(?=\S)")
_NUM_HEADING = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:Annex\s+[A-Z](?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(\S.*)$")
_TOC_LEADER = re.compile(r"(\.{4,}|…{2,}|(?:\.\s){4,})\s*\d*\s*$")
_PAGENUM = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)
_CAPTION = re.compile(r"^(Table|Figure|Fig\.)\s+[\w.\-]+\s*[:.–—-]")
_MONO = re.compile(r"mono|courier|consolas|menlo|lucida console|code", re.I)
_END = tuple(".:;!?)]”\"")


class PdfError(Exception):
    pass


@dataclass
class Line:
    segs: list[Seg]
    plain: str
    size: float
    bold: bool
    mono: bool
    x0: float
    y0: float
    y1: float
    page: int
    rel: float = 0.0   # indent relative to the page's left text edge


@dataclass
class _Table:
    y0: float
    rows: list[list[str]]
    page: int


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())   # keeps letters and digits of every script


def _clean_cell(c) -> str:
    s = re.sub(r"[ \t]+", " ", (c or "").replace("\r", ""))
    s = re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", s)
    return re.sub(r"\s*\n\s*", " ", s).strip()


def _clean_rows(rows) -> list[list[str]]:
    rows = [[_clean_cell(c) for c in (r or [])] for r in rows or []]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    keep = [i for i in range(max(len(r) for r in rows)) if any(i < len(r) and r[i] for r in rows)]
    return [[r[i] if i < len(r) else "" for i in keep] for r in rows]


def _read_page(page, pno: int, pymupdf) -> tuple[list[Line], list[_Table], int]:
    tables: list[tuple[tuple, _Table]] = []
    try:
        found = page.find_tables().tables
    except Exception:  # noqa: BLE001 - a page the table finder cannot read still has its text
        found = []
    for t in found:
        rows = [[escape_text(c) for c in r] for r in _clean_rows(t.extract())]   # cells are literal spec text, not Markdown
        if len(rows) >= 2 and len(rows[0]) >= 2:
            tables.append((tuple(t.bbox), _Table(t.bbox[1], rows, pno)))
    links = [(pymupdf.Rect(lk["from"]), lk["uri"]) for lk in page.get_links() if lk.get("uri")]

    def in_table(cx: float, cy: float) -> bool:
        return any(b[0] - 1 <= cx <= b[2] + 1 and b[1] - 1 <= cy <= b[3] + 1 for b, _ in tables)

    lines: list[Line] = []
    for blk in page.get_text("dict", sort=True)["blocks"]:
        if blk.get("type") != 0:
            continue
        for ln in blk["lines"]:
            spans = [s for s in ln["spans"] if s["text"] != ""]
            plain = "".join(s["text"] for s in spans).strip()
            if not plain:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            if in_table((x0 + x1) / 2, (y0 + y1) / 2):
                continue
            segs, weight = [], Counter()
            for s in spans:
                font = s["font"]
                bold = bool(s["flags"] & 16) or bool(re.search(r"bold|black|heavy", font, re.I))
                italic = bool(s["flags"] & 2) or bool(re.search(r"italic|oblique", font, re.I))
                mono = bool(s["flags"] & 8) or bool(_MONO.search(font))
                uri = None
                if links:
                    c = pymupdf.Point((s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2)
                    uri = next((u for r, u in links if r.contains(c)), None)
                segs.append(Seg(s["text"], bold and s["text"].strip() != "", italic, mono, link=uri))
                weight[(round(s["size"], 1), bold, mono)] += len(s["text"].strip())
            (size, _, _), _ = weight.most_common(1)[0]
            body = [s for s in segs if s.text.strip()]
            lines.append(Line(segs, plain, size, all(s.bold for s in body), all(s.code for s in body),
                              x0, y0, y1, pno))
    return lines, [t for _, t in tables], len(page.get_images())


def _drop_furniture(pages: list[list[Line]], heights: list[float]) -> int:
    """Remove repeating running headers/footers and bare page numbers from the page margins."""
    n = len(pages)
    seen: Counter = Counter()
    for lines, h in zip(pages, heights):
        for k in {re.sub(r"\d+", "#", ln.plain.lower()) for ln in lines if ln.y1 < 0.09 * h or ln.y0 > 0.91 * h}:
            seen[k] += 1
    dropped = 0
    for i, (lines, h) in enumerate(zip(pages, heights)):
        keep = []
        for ln in lines:
            margin = ln.y1 < 0.09 * h or ln.y0 > 0.91 * h
            if margin and (_PAGENUM.match(ln.plain) or (n >= 3 and seen[re.sub(r"\d+", "#", ln.plain.lower())] >= max(3, 0.4 * n))):
                dropped += 1
                continue
            keep.append(ln)
        pages[i] = keep
    return dropped


def _toc_map(doc) -> dict[str, list[tuple[int, int]]]:
    toc = [t for t in doc.get_toc(simple=True) if t[1].strip()]
    if not toc:
        return {}
    base = min(t[0] for t in toc)
    out: dict[str, list[tuple[int, int]]] = {}
    for lvl, title, pg in toc:
        out.setdefault(_norm(title), []).append((max(1, lvl - base + 1), pg - 1))
    return out


class _Builder:
    def __init__(self, body: float, toc: dict, sizes: list[float]):
        self.body, self.toc = body, toc
        self.rank = {s: min(i + 1, 6) for i, s in enumerate(sorted(sizes, reverse=True))}
        self.blocks: list[Block] = []
        self.cur: list[Line] | None = None
        self.kind = ""
        self.cur_level = 0
        self.last: Line | None = None
        self.list_x: list[float] = []   # distinct list indents across the whole document

    # ------------------------------------------------------------------ classification
    def heading_level(self, ln: Line) -> int | None:
        t = ln.plain
        if len(t) > 160 or _TOC_LEADER.search(t) or t.endswith(("," , ";")):
            return None
        key = _norm(t)
        for lvl, pg in self.toc.get(key, []) if key else []:
            if pg == ln.page:
                return lvl
        big = ln.size >= self.body * 1.15 and ln.size >= self.body + 1.0
        if big and not _BULLET.match(t):
            return self.rank.get(ln.size, 6)
        m = _NUM_HEADING.match(t)
        if m and ln.bold and len(t) <= 120 and not t.endswith(".") and not _NUMBER.match(t):
            return min(6, max(1, m.group(1).count(".") + 1))
        return None

    # ------------------------------------------------------------------ assembly
    def flush(self) -> None:
        if not self.cur:
            self.cur = None
            return
        lines, kind = self.cur, self.kind
        self.cur = None
        if kind == "code":
            base = min(ln.x0 for ln in lines)
            cw = max(1.0, lines[0].size * 0.6)
            self.blocks.append(Code([" " * int(round((ln.x0 - base) / cw)) + ln.plain for ln in lines]))
        elif kind == "heading":
            self.blocks.append(Heading(self.cur_level, " ".join(ln.plain for ln in lines)))
        else:
            segs = _join_lines(lines)
            if kind == "item":
                self.blocks.append(self._item(lines[0], segs))
            else:
                text = "".join(s.text for s in segs).strip()
                if _CAPTION.match(text) and not any(s.bold for s in segs):
                    segs = [Seg(s.text, True, s.italic, s.code, link=s.link) for s in segs]
                self.blocks.append(Para(segs))

    def _item(self, first: Line, segs: list[Seg]) -> Item:
        text = "".join(s.text for s in segs)
        lvl = next((i for i, x in enumerate(self.list_x) if abs(x - first.rel) <= 4), 0)
        lvl = min(lvl, 4)
        m = _BULLET.match(text.lstrip())
        if m:
            return Item(lvl, _strip_prefix(segs, m.end()))
        m = _NUMBER.match(text.lstrip())
        if m:
            tok = m.group(1)
            rest = _strip_prefix(segs, m.end())
            if re.fullmatch(r"\d{1,3}[.)]", tok):
                return Item(lvl, rest, True, tok[:-1])
            return Item(lvl, rest, True, "1", tok)
        return Item(lvl, segs)

    def add(self, ln: Line, page_start: bool) -> None:
        level = self.heading_level(ln)
        mono = ln.mono and not level
        item = bool(not level and not mono and (_BULLET.match(ln.plain) or _NUMBER.match(ln.plain)))
        kind = "heading" if level else "code" if mono else "item" if item else "p"
        if self.cur is not None and self.last is not None:
            cont = False
            if kind == self.kind and kind != "item":
                cont = kind != "heading" or level == self.cur_level
            elif kind == "p" and self.kind == "item":
                # wrapped continuation of a list item: indented past the marker, or running on in lower case
                cont = ln.x0 > self.cur[0].x0 + 4 or ln.plain[:1].islower()
            if cont and page_start:
                prev = self.cur[-1].plain[-1:]
                cont = kind == "code" or (kind == "p" and ln.plain[:1].islower() and prev not in _END)
            elif cont:
                cont = -ln.size <= ln.y0 - self.last.y1 <= max(0.45 * ln.size, 3.0)
            if cont:
                self.cur.append(ln)
                self.last = ln
                return
        self.flush()
        self.cur, self.kind, self.cur_level, self.last = [ln], kind, level or 0, ln

    def table(self, t: _Table, page_start: bool) -> None:
        self.flush()
        self.last = None
        prev = self.blocks[-1] if self.blocks else None
        if (page_start and isinstance(prev, Table) and len(prev.rows[0]) == len(t.rows[0])
                and [c.lower() for c in t.rows[0]] == [c.lower() for c in prev.rows[0]]):
            prev.rows.extend(t.rows[1:])   # same repeated header: one table continued on the next page
        else:
            self.blocks.append(Table(t.rows))


def _strip_prefix(segs: list[Seg], n: int) -> list[Seg]:
    out, skip = [], n
    stripped_lead = False
    for s in segs:
        text = s.text
        if not stripped_lead:
            lead = len(text) - len(text.lstrip())
            if text.strip():
                text = text[lead:]
                stripped_lead = True
            else:
                continue
        if skip > 0:
            cut = min(skip, len(text))
            text, skip = text[cut:], skip - cut
        if text:
            out.append(Seg(text, s.bold, s.italic, s.code, s.sup, s.sub, s.link))
    if out:
        out[0] = Seg(out[0].text.lstrip(), *[getattr(out[0], f) for f in ("bold", "italic", "code", "sup", "sub", "link")])
    return out


def _join_lines(lines: list[Line]) -> list[Seg]:
    out: list[Seg] = []
    for i, ln in enumerate(lines):
        segs = list(ln.segs)
        if i:
            tail = out[-1].text if out else ""
            head = segs[0].text if segs else ""
            if re.search(r"[a-z]-$", tail) and head[:1].islower():
                out[-1] = Seg(tail[:-1], *[getattr(out[-1], f) for f in ("bold", "italic", "code", "sup", "sub", "link")])
            elif not tail.endswith(" ") and not head.startswith(" "):
                out.append(Seg(" "))
        out.extend(segs)
    return out


def _drop_empty_toc(blocks: list[Block]) -> list[Block]:
    """A 'Contents' heading whose entries were all dot-leader lines is left with nothing under it."""
    out: list[Block] = []
    for i, b in enumerate(blocks):
        if isinstance(b, Heading) and b.toc and re.fullmatch(r"(table of )?contents", b.text.strip(), re.I):
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is None or isinstance(nxt, Heading):
                continue
        out.append(b)
    return out


def convert(path: str) -> tuple[list[Block], dict]:
    import pymupdf

    try:
        doc = pymupdf.open(path)
    except Exception as e:  # noqa: BLE001
        raise PdfError(f"Cannot open the PDF: {e}") from e
    if doc.needs_pass:
        raise PdfError("The PDF is password protected.")
    pages, tables, images, heights = [], [], 0, []
    for pno, page in enumerate(doc):
        lines, tbls, n_img = _read_page(page, pno, pymupdf)
        pages.append(lines)
        tables.append(tbls)
        heights.append(page.rect.height)
        images += n_img
    total_chars = sum(len(ln.plain) for p in pages for ln in p)
    if total_chars == 0 and not any(tables):
        raise PdfError("The PDF has no text layer (scanned images). OCR is not supported; run the file through an OCR tool first.")
    furniture = _drop_furniture(pages, heights)
    indents: list[float] = []
    for lines in pages:
        prose = [ln.x0 for ln in lines if not (_BULLET.match(ln.plain) or _NUMBER.match(ln.plain))] or [ln.x0 for ln in lines]
        left = min(prose, default=0.0)
        for ln in lines:
            ln.rel = ln.x0 - left
            if (_BULLET.match(ln.plain) or _NUMBER.match(ln.plain)) and not ln.mono:
                indents.append(ln.rel)
    clusters: list[float] = []
    for x in sorted(indents):
        if not clusters or x - clusters[-1] > 4:
            clusters.append(x)

    weight: Counter = Counter()
    for p in pages:
        for ln in p:
            if not _TOC_LEADER.search(ln.plain):
                weight[round(ln.size, 1)] += len(ln.plain)
    body = weight.most_common(1)[0][0] if weight else 10.0
    toc = _toc_map(doc)
    sizes = sorted({round(ln.size, 1) for p in pages for ln in p
                    if ln.size >= body * 1.15 and ln.size >= body + 1.0 and len(ln.plain) <= 160 and ln.plain not in ("",)
                    and not _TOC_LEADER.search(ln.plain)})
    b = _Builder(body, toc, sizes)
    b.list_x = clusters
    leaders = 0
    for pno, (lines, tbls) in enumerate(zip(pages, tables)):
        items = sorted([(ln.y0, 0, ln) for ln in lines] + [(t.y0, 1, t) for t in tbls], key=lambda x: (x[0], x[1]))
        first = True
        for _, kind, it in items:
            if kind == 1:
                b.table(it, first)
            elif _TOC_LEADER.search(it.plain):
                leaders += 1
                b.flush()
                if b.blocks and isinstance(b.blocks[-1], Heading):
                    b.blocks[-1].toc = True
                continue
            else:
                b.add(it, first)
            first = False
    b.flush()
    blocks = _drop_empty_toc(b.blocks)
    meta = {"pages": len(doc), "title": (doc.metadata or {}).get("title") or "", "images_skipped": images,
            "furniture_removed": furniture, "toc_lines_removed": leaders}
    return blocks, meta
