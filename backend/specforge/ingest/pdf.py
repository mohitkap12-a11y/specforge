"""PDF -> IR.

Stages
  1. extract_pages()   (parallel by page chunk) lines + candidate tables per page, PyMuPDF with pdfplumber fallback
  2. assemble()        header/footer detection, bookmark-driven section tree, caption binding,
                       multi-page table stitching, nested table detection, text accounting
  3. normalise         requirements + references (shared with DOCX, see build_semantics)

Bookmarks (the PDF outline) are the authority for the section tree. Docling can replace stage 1's table
extractor via the optional adapter in docling_adapter.py when installed.
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from .ir import Excluded, ParsedSpec, Section, Table
from .normalize import (
    clean,
    detect_identity,
    norm_title,
    nospace_len,
    parse_bit_range,
    parse_byte_range,
    parse_caption,
    parse_value_range,
)
from .semantics import build_semantics

QUALITY_FALLBACK_THRESHOLD = 0.72
MIN_RULING_SEGMENTS = 8   # drawn lines / rectangles on a page before we suspect a ruled table PyMuPDF missed
MAX_HEADER_LINES = 5      # a heading wrapped over more lines than this is not treated as one
MAX_HEADER_CELL = 40      # ... and each wrapped fragment is short
_NUM_HEADING = re.compile(r"^((?:\d+(?:\.\d+)*)|(?:Annex\s+[A-Z](?:\.\d+)*)|(?:[A-Z](?:\.\d+)+))\s+(\S.*)$")
_TOC_LEADER = re.compile(r"(\.{4,}|…{2,}|(?:\.\s){4,})\s*\d*\s*$")
_PAGENUM = re.compile(r"^\s*(?:page\s+)?\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?\s*$", re.I)


# ----------------------------------------------------------------------------- stage 1: per-page extraction

def _table_quality(rows: list[list[str]]) -> float:
    if len(rows) < 2 or not rows[0] or len(rows[0]) < 2:
        return 0.0
    ncol = len(rows[0])
    cells = [c for r in rows for c in r]
    nonempty = sum(1 for c in cells if c.strip()) / max(1, len(cells))
    header_ok = sum(1 for c in rows[0] if c.strip()) / ncol
    first_col = [r[0] for r in rows[1:] if r]
    parse_hits = 0
    if first_col:
        parse_hits = sum(1 for c in first_col if parse_bit_range(c) or parse_byte_range(c) or parse_value_range(c)) / len(first_col)
    consistent = 1.0 if all(len(r) == ncol for r in rows) else 0.6
    return round(0.40 * nonempty + 0.25 * header_ok + 0.15 * consistent + 0.20 * parse_hits, 4)


def _cells(rows) -> list[list[str]]:
    return [[clean(c) for c in (r or [])] for r in rows if r is not None]


def _extract_chunk(args: tuple[str, list[int], bool]) -> list[dict[str, Any]]:
    """Extract a list of pages with ONE document handle (and one lazily opened pdfplumber handle)."""
    path, pnos, allow_fallback = args
    import pymupdf as fitz

    doc = fitz.open(path)
    plumber = None
    out = []
    try:
        for pno in pnos:
            page = doc[pno]
            H, W = page.rect.height, page.rect.width
            tables: list[dict] = []
            try:
                for t in page.find_tables().tables:
                    rows = _cells(t.extract())
                    if rows:
                        tables.append({"bbox": [float(v) for v in t.bbox], "rows": rows, "extractor": "pymupdf",
                                       "quality": _table_quality(rows)})
            except Exception:  # pragma: no cover - defensive
                pass
            weak = any(t["quality"] < QUALITY_FALLBACK_THRESHOLD for t in tables)
            # a page with no PyMuPDF table that still names one in a caption, or is full of ruling lines (the middle page of a
            # multi-page table has no caption), is worth a second look with pdfplumber
            missed = not tables and (any(parse_caption(ln) for ln in page.get_text("text").splitlines())
                                     or len(page.get_drawings()) >= MIN_RULING_SEGMENTS)
            if allow_fallback and (weak or missed):
                try:
                    if plumber is None:
                        import pdfplumber

                        plumber = pdfplumber.open(path)
                    for pt in plumber.pages[pno].find_tables():
                        rows = _cells(pt.extract())
                        q = _table_quality(rows)
                        bb = [float(v) for v in pt.bbox]
                        overlapping = [t for t in tables if _overlap(t["bbox"], bb) > 0.6]
                        if not overlapping:
                            if rows and q >= QUALITY_FALLBACK_THRESHOLD:     # a table PyMuPDF missed entirely
                                tables.append({"bbox": bb, "rows": rows, "extractor": "pdfplumber", "quality": q})
                            continue
                        # only tables this one covers (the same table, or fragments of it): a table nested inside a
                        # larger one must not replace the outer table
                        worse = [t for t in overlapping if q > t["quality"] and _covered(t["bbox"], bb) > 0.6]
                        for t in worse:
                            tables.remove(t)
                        if worse:       # once, even when it replaces several PyMuPDF fragments
                            tables.append({"bbox": bb, "rows": rows, "extractor": "pdfplumber", "quality": q})
                except Exception:  # pragma: no cover
                    pass
            tables.sort(key=lambda t: (t["bbox"][1], t["bbox"][0]))
            lines: list[dict] = []
            bi = 0
            for b in page.get_text("dict").get("blocks", []):
                if b.get("type") != 0:
                    continue
                bi += 1
                for ln in b.get("lines", []):
                    spans = [s for s in ln.get("spans", []) if s.get("text", "").strip() != ""]
                    text = "".join(s["text"] for s in ln.get("spans", []))
                    if not spans or not text.strip():
                        continue
                    x0, y0, x1, y1 = ln["bbox"]
                    lines.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "block": bi,
                                  "size": max(s["size"] for s in spans),
                                  "bold": any((s["flags"] & 16) or "bold" in s["font"].lower() for s in spans)})
            lines.sort(key=lambda l: (round(l["y0"], 0), l["x0"]))
            out.append({"page": pno + 1, "height": H, "width": W, "lines": lines, "tables": tables,
                        "raw_chars": nospace_len(page.get_text("text"))})
    finally:
        doc.close()
        if plumber is not None:
            plumber.close()
    return out


def _overlap(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    area = max(1e-6, min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])))
    return inter / area


def _covered(a: list[float], b: list[float]) -> float:
    """Fraction of box a that lies inside box b."""
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))


def extract_pages(path: str, workers: int = 1, progress: Callable[[int, int], None] | None = None,
                  allow_fallback: bool = True) -> list[dict]:
    import multiprocessing

    import pymupdf as fitz

    with fitz.open(path) as d:
        n = d.page_count
    if workers <= 1 or n < 8:
        chunks = [list(range(i, min(n, i + 10))) for i in range(0, n, 10)]
        out: list[dict] = []
        for c in chunks:
            out += _extract_chunk((path, c, allow_fallback))
            if progress:
                progress(len(out), n)
        return out
    size = max(1, -(-n // (workers * 4)))
    chunks = [list(range(i, min(n, i + size))) for i in range(0, n, size)]
    out = []
    # spawn: the API process is multi-threaded, and forking it can deadlock the children
    with ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn")) as ex:
        for res in ex.map(_extract_chunk, [(path, c, allow_fallback) for c in chunks]):
            out += res
            if progress:
                progress(len(out), n)
    return out


# ----------------------------------------------------------------------------- stage 2: assembly

@dataclass
class _Entry:
    level: int
    title: str
    page: int
    number: str = ""
    name: str = ""
    consumed: bool = False


def _inside(line: dict, bbox: list[float], pad: float = 1.5) -> bool:
    cx, cy = (line["x0"] + line["x1"]) / 2, (line["y0"] + line["y1"]) / 2
    return bbox[0] - pad <= cx <= bbox[2] + pad and bbox[1] - pad <= cy <= bbox[3] + pad


def _group_by_page(entries: list) -> dict[int, list]:
    out: dict[int, list] = defaultdict(list)
    for e in entries:
        out[e.page].append(e)
    return out


def _split_heading(title: str) -> tuple[str, str]:
    m = _NUM_HEADING.match(clean(title))
    if m:
        return m.group(1).replace("Annex ", "Annex_"), m.group(2)
    return "", clean(title)


def _detect_headers_footers(pages: list[dict], heading_titles: dict[int, list[str]] | None = None) -> set[tuple[int, int]]:
    """Return (page, line_index) of header/footer/page-number lines.

    heading_titles: normalised bookmark titles per page. A line that IS a heading is never a running header, even when its
    digit-normalised text repeats on other pages ("5.1 Command Dword 10" / "5.2 Command Dword 11").
    """
    n = len(pages)
    titles = heading_titles or {}

    def is_heading(pg: int, text: str) -> bool:
        nl = norm_title(text)
        return any(nl == t or (len(nl) >= 3 and t.startswith(nl)) for t in titles.get(pg, []))

    sig_pages: dict[str, set[int]] = defaultdict(set)
    for p in pages:
        H = p["height"]
        for i, l in enumerate(p["lines"]):
            if is_heading(p["page"], l["text"]):
                continue
            if l["y1"] < 0.09 * H or l["y0"] > 0.91 * H:
                sig = re.sub(r"\d+", "#", norm_title(l["text"]))
                sig_pages[sig].add(p["page"])
    thresh = max(2, int(0.4 * n)) if n >= 3 else 2
    repeated = {s for s, ps in sig_pages.items() if len(ps) >= thresh}
    out = set()
    for p in pages:
        H = p["height"]
        for i, l in enumerate(p["lines"]):
            if not (l["y1"] < 0.09 * H or l["y0"] > 0.91 * H) or is_heading(p["page"], l["text"]):
                continue
            sig = re.sub(r"\d+", "#", norm_title(l["text"]))
            if sig in repeated or _PAGENUM.match(l["text"]):
                out.add((p["page"], i))
    return out


def _build_entries(doc_toc: list, pages: list[dict]) -> list[_Entry]:
    entries: list[_Entry] = []
    if doc_toc:
        for item in doc_toc:
            lvl, title, pg = item[0], item[1], item[2]
            if pg < 1:
                continue
            num, name = _split_heading(title)
            entries.append(_Entry(level=lvl, title=clean(title), page=pg, number=num, name=name))
        return entries
    # fallback: bold lines that start with a section number
    for p in pages:
        sizes = Counter(round(l["size"], 1) for l in p["lines"])
        body = sizes.most_common(1)[0][0] if sizes else 10
        for l in p["lines"]:
            t = clean(l["text"])
            if _TOC_LEADER.search(t):
                continue
            m = _NUM_HEADING.match(t)
            if m and (l["bold"] or l["size"] > body + 0.8) and len(t) < 140 and "." not in t[-1:]:
                num = m.group(1)
                entries.append(_Entry(level=num.count(".") + 1, title=t, page=p["page"], number=num, name=m.group(2)))
    return entries


def assemble(path: str, pages: list[dict], family: str, version: str, title: str, fmt: str = "pdf",
             toc: list | None = None) -> ParsedSpec:
    entries = _build_entries(toc or [], pages)
    hf = _detect_headers_footers(pages, {pg_: [norm_title(e.title) for e in es] for pg_, es in _group_by_page(entries).items()})

    # --- section skeleton from entries
    sections: list[Section] = []
    front = Section(number="front", title="Front matter", level=0, page_from=1, parent=None)
    sections.append(front)
    by_page: dict[int, list[_Entry]] = defaultdict(list)
    stack: list[tuple[int, Section]] = []
    seen_numbers: set[str] = set()
    notes: list[dict] = []
    synth = 0
    entry_section: dict[int, Section] = {}
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
        parent = stack[-1][1].number if stack else None
        sec = Section(number=number, title=e.name or e.title, level=e.level, page_from=e.page, parent=parent)
        sections.append(sec)
        entry_section[id(e)] = sec
        stack.append((e.level, sec))
        by_page[e.page].append(e)

    tables: list[Table] = []
    excluded: list[Excluded] = []
    raw_chars = sum(p["raw_chars"] for p in pages)
    acct = {"body": 0, "heading": 0, "caption": 0, "excluded": 0, "link_doubles": 0, "table_cells": 0, "table_lines": 0}

    cur = front
    last_table: Table | None = None
    last_table_page = 0
    last_table_kind = ""        # 'Figure' / 'Table' of the caption the last table was bound to
    carry_caption: dict | None = None  # caption left at a page bottom whose table starts on the next page
    last_table_end = (0.0, 1.0)         # (y1 of the last table segment, page height): continued tables run to the page bottom
    table_count_on_page: Counter = Counter()
    labels_used: Counter = Counter()

    def add_text(sec: Section, text: str, new_para: bool, page: int):
        text = clean(text)
        if not text:
            return
        acct["body"] += nospace_len(text)
        if new_para or not sec.paragraphs:
            # merge across blocks/pages when the previous paragraph is visibly unfinished
            if sec.paragraphs and not re.search(r"[.:;!?)\]]\s*$", sec.paragraphs[-1]) and text[:1].islower():
                join(sec, text)
            else:
                sec.paragraphs.append(text)
        else:
            join(sec, text)

    def join(sec: Section, text: str):
        # a doubled hyperlink can wrap over two lines ("Figure 638Figure" / "638)"); clean() again once they are joined
        sec.paragraphs[-1] = clean(sec.paragraphs[-1] + " " + text)

    for p in pages:
        pg = p["page"]
        lines = p["lines"]
        ptables = [part for t in p["tables"] for part in _split_at_captions(t, lines)]
        # table containment => nested
        tinfo = []
        for ti, t in enumerate(ptables):
            parent_idx = None
            for tj, o in enumerate(ptables):
                if tj != ti and _overlap(t["bbox"], o["bbox"]) > 0.95 and \
                        (o["bbox"][2] - o["bbox"][0]) * (o["bbox"][3] - o["bbox"][1]) > \
                        (t["bbox"][2] - t["bbox"][0]) * (t["bbox"][3] - t["bbox"][1]):
                    parent_idx = tj
            tinfo.append({"t": t, "parent": parent_idx, "label": None})

        items: list[tuple[float, int, str, Any]] = []
        for i, l in enumerate(lines):
            tix = next((k for k, ti in enumerate(tinfo) if _inside(l, ti["t"]["bbox"])), None)
            if tix is not None:
                acct["table_lines"] += nospace_len(l["text"])
                continue
            items.append((l["y0"], 0, "line", (i, l)))
        for k, ti in enumerate(tinfo):
            items.append((ti["t"]["bbox"][1], 1, "table", (k, ti)))
        items.sort(key=lambda x: (x[0], x[1]))

        # a bookmark can point at the bottom of the page before its heading: look for a missed one on the next page too
        pending_entries = [e for e in by_page.get(pg, []) + by_page.get(pg - 1, []) if not e.consumed]
        pending_caption: dict | None = carry_caption
        carry_caption = None
        wrap: tuple[_Entry, str] | None = None
        seen_body_on_page = False
        prev_block = None

        def flush_caption(cap):
            if cap is None:
                return
            lbl = _unique_label(cap["label"], labels_used, notes, cap["page"])
            tables.append(Table(label=lbl, caption=cap["title"], header=[], rows=[], page_from=cap["page"], page_to=cap["page"],
                                section=cur.number if cur.number != "front" else None, captioned=True,
                                extractor="none", quality=0.0))
            notes.append({"severity": "info", "kind": "figure_without_table", "node_key": f"fig:{lbl}", "page": cap["page"],
                          "message": f"Caption '{cap['kind'].title()} {cap['label']}' has no extractable table "
                                     f"(image/diagram?)"})

        for _, _, kind, payload in items:
            if kind == "line":
                i, l = payload
                text = clean(l["text"])
                acct["link_doubles"] += nospace_len(l["text"]) - nospace_len(text)   # hyperlink text the PDF repeats
                if (pg, i) in hf:
                    excluded.append(Excluded(pg, "header/footer", text))
                    acct["excluded"] += nospace_len(text)
                    continue
                if _TOC_LEADER.search(text):
                    excluded.append(Excluded(pg, "toc", text))
                    acct["excluded"] += nospace_len(text)
                    continue
                # heading wrap continuation
                if wrap is not None:
                    e, acc = wrap
                    cand = clean(acc + " " + text)
                    if norm_title(e.title).startswith(norm_title(cand)):
                        acct["heading"] += nospace_len(text)
                        if norm_title(cand) == norm_title(e.title):
                            wrap = None
                        else:
                            wrap = (e, cand)
                        continue
                    wrap = None
                # heading match
                matched = None
                nl = norm_title(text)
                for k_, e in enumerate(pending_entries):      # any pending bookmark of this page may match, not just the first
                    nt = norm_title(e.title)
                    if nl == nt or (len(nl) >= 3 and nt.startswith(nl) and (l["bold"] or _NUM_HEADING.match(text))):
                        matched = pending_entries.pop(k_)
                        break
                if matched is None and l["bold"] and nl:
                    # deep headings are often printed without their number ("I/O Controller" for bookmark
                    # "3.1.3.1 I/O Controller"); only a bold line, and only the first pending bookmark it fits
                    for k_, e in enumerate(pending_entries):
                        nn = norm_title(e.name)
                        if e.number and (nl == nn or (len(nl) >= 3 and nn.startswith(nl))):
                            matched = pending_entries.pop(k_)
                            break
                if matched is not None:
                    matched.consumed = True
                    flush_caption(pending_caption)
                    pending_caption = None
                    cur = entry_section[id(matched)]
                    acct["heading"] += nospace_len(text)
                    if norm_title(text) != norm_title(matched.title):
                        wrap = (matched, text)
                    prev_block = None
                    seen_body_on_page = True
                    continue
                cap = parse_caption(text)
                if cap:
                    flush_caption(pending_caption)
                    pending_caption = {"kind": cap[0], "label": cap[1], "title": cap[2], "cont": cap[3], "y": l["y1"],
                                       "block": l["block"], "page": pg}
                    acct["caption"] += nospace_len(text)
                    prev_block = ("cap", l["block"])
                    seen_body_on_page = True
                    continue
                if pending_caption is not None and prev_block == ("cap", pending_caption["block"]) \
                        and l["block"] == pending_caption["block"]:
                    # wrapped caption line
                    pending_caption["title"] = clean(pending_caption["title"] + " " + text)
                    acct["caption"] += nospace_len(text)
                    continue
                # ordinary body text: a caption not directly followed by its table is a figure without table
                if pending_caption is not None:
                    flush_caption(pending_caption)
                    pending_caption = None
                new_para = prev_block != ("body", l["block"], pg)
                add_text(cur, text, new_para, pg)
                prev_block = ("body", l["block"], pg)
                seen_body_on_page = True
            else:
                k, ti = payload
                t = ti["t"]
                rows = t["rows"]
                if ti["parent"] is None:    # a nested table's text is already in its parent's cells
                    acct["table_cells"] += sum(nospace_len(c) for r in rows for c in r)
                cap = pending_caption
                pending_caption = None
                inner = _caption_row(rows)
                if inner is not None:
                    # the caption is drawn inside the table frame and was extracted as its first row
                    if cap is not None and (cap["kind"], cap["label"]) != (inner[0], inner[1]):
                        flush_caption(cap)
                    cap = {"kind": inner[0], "label": inner[1], "title": inner[2], "cont": inner[3], "page": pg}
                    rows = rows[1:]
                prev_block = None
                nested_parent = None
                if ti["parent"] is not None and tinfo[ti["parent"]]["label"] is not None:
                    nested_parent = tinfo[ti["parent"]]["label"]

                is_cont = False
                hdr, body = _split_header(rows)
                if last_table is not None and nested_parent is None:
                    same_cols = len(rows[0]) == len(last_table.header) if rows and last_table.header else False
                    hdr_repeat = same_cols and _header_lines(rows, last_table.header) > 0
                    if cap is not None and cap["cont"] and cap["label"] == last_table.label.split("#")[0]:
                        is_cont = True
                    elif cap is not None and hdr_repeat and last_table_page == pg - 1 and cap["label"] == last_table.label.split("#")[0] \
                            and cap["kind"] == last_table_kind and norm_title(cap["title"]) == norm_title(last_table.caption):
                        is_cont = True      # the caption is repeated on the next page without saying "continued"
                    elif cap is None and last_table_page == pg - 1 and not seen_body_on_page and same_cols and \
                            (hdr_repeat or last_table_end[0] >= 0.75 * last_table_end[1]):
                        # evidence required: the header repeats, or the previous table ran to the bottom of its page.
                        # An independent table that merely has the same number of columns is NOT merged.
                        is_cont = True
                        notes.append({"severity": "info" if hdr_repeat else "warning", "kind": "continuation_inferred", "node_key": f"fig:{last_table.label}",
                                      "page": pg, "message": "Table without caption at page top treated as continuation "
                                                              f"of Figure {last_table.label}"})
                if is_cont:
                    last_table_end = (t["bbox"][3], p["height"])
                    _merge_continuation(last_table, rows, pg)
                    last_table.quality = min(last_table.quality, t["quality"])
                    tinfo[k]["label"] = last_table.label
                    last_table_page = pg
                    seen_body_on_page = True
                    continue

                if cap is not None:
                    lbl = _unique_label(cap["label"], labels_used, notes, pg)
                    caption, captioned = cap["title"], True
                else:
                    table_count_on_page[pg] += 1
                    lbl = f"p{pg}t{table_count_on_page[pg]}"
                    caption, captioned = "", False
                    notes.append({"severity": "warning", "kind": "uncaptioned_table", "node_key": f"fig:{lbl}", "page": pg,
                                  "message": "Table without a Figure/Table caption"})
                tb = Table(label=lbl, caption=caption, header=hdr, rows=body, page_from=pg, page_to=pg,
                           section=cur.number if cur.number != "front" else None, captioned=captioned,
                           parent_table=nested_parent, extractor=t["extractor"], quality=t["quality"], bbox=t["bbox"])
                if t["quality"] < QUALITY_FALLBACK_THRESHOLD:
                    notes.append({"severity": "warning", "kind": "low_table_quality", "node_key": f"fig:{lbl}", "page": pg,
                                  "message": f"Table quality {t['quality']:.2f} below threshold ({t['extractor']})"})
                if nested_parent:
                    notes.append({"severity": "info", "kind": "nested_table", "node_key": f"fig:{lbl}", "page": pg,
                                  "message": f"Nested inside Figure {nested_parent}; parent cell text may duplicate it"})
                tables.append(tb)
                tinfo[k]["label"] = lbl
                if nested_parent is None:
                    last_table, last_table_page = tb, pg
                    last_table_kind = cap["kind"] if cap is not None else ""
                    last_table_end = (t["bbox"][3], p["height"])
                seen_body_on_page = True
        if pending_caption is not None:
            if pg < len(pages):
                carry_caption = pending_caption  # table may start at the top of the next page
            else:
                flush_caption(pending_caption)

    for e in entries:
        if not e.consumed:
            notes.append({"severity": "error", "kind": "heading_not_found", "node_key": f"sec:{e.number or e.title}",
                          "page": e.page, "message": f"Bookmark '{e.title}' (page {e.page}) was not found in the page text"})
    # page_to for sections
    for i, s in enumerate(sections):
        nxt = next((x.page_from for x in sections[i + 1:] if x.level <= s.level), None)
        s.page_to = nxt if nxt is not None else len(pages)

    accounted = sum(acct[k] for k in ("body", "heading", "caption", "excluded", "link_doubles", "table_cells"))
    spec = ParsedSpec(family=family, version=version, title=title, fmt=fmt, pages=len(pages), sections=sections,
                      tables=tables, requirements=[], references=[], excluded=excluded, raw_char_count=raw_chars,
                      accounted_char_count=accounted, notes=notes,
                      stats={"char_accounting": acct, "toc_entries": len(entries),
                             "extractors": dict(Counter(t.extractor for t in tables))})
    # table text loss check (cells vs lines inside table bboxes)
    if acct["table_lines"] and acct["table_cells"] < 0.985 * acct["table_lines"]:
        spec.notes.append({"severity": "warning", "kind": "table_text_loss", "node_key": None, "page": None,
                           "message": f"Table cells hold {acct['table_cells']} chars but {acct['table_lines']} chars "
                                      "lie inside table regions"})
    build_semantics(spec)
    return spec


def _unique_label(label: str, used: Counter, notes: list, page: int) -> str:
    used[label] += 1
    if used[label] == 1:
        return label
    new = f"{label}#{used[label]}"
    notes.append({"severity": "warning", "kind": "duplicate_figure_label", "node_key": f"fig:{new}", "page": page,
                  "message": f"Figure/Table {label} appears more than once without being a continuation"})
    return new


def _split_at_captions(t: dict, lines: list[dict]) -> list[dict]:
    """Two tables drawn as one grid come out as one table with the second caption as a row; split them there."""
    rows = t["rows"]
    for k in range(2, len(rows)):
        if _caption_row(rows[k:]) is None:
            continue
        cap = next((l for l in lines if _inside(l, t["bbox"]) and clean(l["text"]) == clean(next(c for c in rows[k] if c.strip()))), None)
        if cap is None:
            continue
        y = cap["y0"] - 1.0
        x0, y0, x1, y1 = t["bbox"]
        head = {**t, "bbox": [x0, y0, x1, y], "rows": rows[:k], "quality": _table_quality(rows[:k])}
        return [head] + _split_at_captions({**t, "bbox": [x0, y, x1, y1], "rows": rows[k:], "quality": _table_quality(rows[k:])}, lines)
    return [t]


def _caption_row(rows: list[list[str]]):
    """parse_caption() of the first row when that row is nothing but a Figure/Table caption, else None."""
    if len(rows) < 2:
        return None
    cells = [c for c in rows[0] if c.strip()]
    return parse_caption(cells[0]) if len(cells) == 1 else None


def _same_header(a: list[str], b: list[str]) -> bool:
    return [norm_title(c) for c in a] == [norm_title(c) for c in b]


def _join_lines(lines: list[list[str]]) -> list[str]:
    header = list(lines[0])
    for r in lines[1:]:
        header += [""] * (len(r) - len(header))
        header = [clean(f"{h} {r[j]}") if j < len(r) else h for j, h in enumerate(header)]
    return header


def _key(row: list[str]) -> str:
    return row[0].strip() if row else ""


def _split_header(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """(header, body). A heading wrapped over several lines arrives as the first row plus short rows with an empty first cell
    (before any row that has a key); those lines belong to the header, joined column by column."""
    if not rows:
        return [], []
    n = 1
    while (n < len(rows) and n < MAX_HEADER_LINES and _key(rows[0]) and not _key(rows[n])
           and any(c.strip() for c in rows[n]) and all(len(c) <= MAX_HEADER_CELL for c in rows[n])):
        n += 1
    return _join_lines(rows[:n]), rows[n:]


def _header_lines(rows: list[list[str]], header: list[str]) -> int:
    """How many leading rows are a repeat of `header` (0 if none). Tried line by line, because a fragment of the row that
    was split across the page break can follow the repeated heading."""
    for k in range(1, min(len(rows), MAX_HEADER_LINES) + 1):
        if _same_header(_join_lines(rows[:k]), header):
            return k
    return 0


def _merge_continuation(prev: Table, rows: list[list[str]], page: int) -> None:
    if not rows:
        return
    body = rows[_header_lines(rows, prev.header):] if prev.header else rows
    if body and prev.rows and not _key(body[0]) and any(c.strip() for c in body[0][1:]):
        # row split across the page break: first cell empty -> glue onto the previous row
        last = prev.rows[-1]
        for j, c in enumerate(body[0]):
            if c.strip() and j < len(last):
                last[j] = clean(last[j] + " " + c)
        body = body[1:]
    prev.rows.extend(body)
    prev.page_to = page
    prev.continued_pages += 1


# ----------------------------------------------------------------------------- entry point

def parse_pdf(path: str, family: str | None = None, version: str | None = None, workers: int = 1,
              progress: Callable[[str, int, int], None] | None = None, allow_fallback: bool = True) -> ParsedSpec:
    import pymupdf as fitz

    doc = fitz.open(path)
    toc = doc.get_toc(simple=True)
    first_text = "\f".join(doc[i].get_text("text") for i in range(min(3, doc.page_count)))
    meta_title = (doc.metadata or {}).get("title") or ""
    doc.close()
    f2, v2 = detect_identity(first_text, os.path.basename(path))
    family = family or f2 or "unknown"
    version = version or v2 or "0"

    def prog(done, total):
        if progress:
            progress("extract", done, total)

    pages = extract_pages(path, workers=workers, progress=prog, allow_fallback=allow_fallback)
    if progress:
        progress("assemble", 0, 1)
    spec = assemble(path, pages, family, version, meta_title or os.path.basename(path), "pdf", toc)
    if progress:
        progress("assemble", 1, 1)
    return spec
