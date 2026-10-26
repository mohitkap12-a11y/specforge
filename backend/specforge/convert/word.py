"""DOCX -> Markdown blocks, reading the OOXML in document order.

Preserved: heading levels (Heading N, Title, outline levels, including the auto-numbers Word does not store in the text),
bullet / numbered lists with nesting, bold / italic / monospace / super- and subscript, hyperlinks, tables (merged cells,
nested tables flattened into their cell), footnotes, image alt text, code-styled paragraphs.
Tracked deletions are dropped and insertions kept. Headers, footers, comments and text boxes are not part of the body and are skipped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .md import Block, Code, Heading, Item, Para, Quote, Raw, Seg, Table, inline

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_MONO = re.compile(r"courier|consolas|mono|menlo|lucida console", re.I)
_CODE_STYLE = re.compile(r"code|preformatted|source|verbatim|listing", re.I)
MAX_COLS = 256   # cells kept per table row
MAX_TABLE_CELLS = 2_000_000
MAX_SPAN = 64   # a hostile gridSpan must not allocate billions of cells
_QUOTE_STYLE = re.compile(r"(intense )?quote|block text|callout", re.I)


class DocxError(Exception):
    pass


def _val(el, tag: str, default=None):
    if el is None:
        return default
    c = el.find(W + tag)
    return default if c is None else c.get(W + "val", default)


def _on(rpr, tag: str) -> bool | None:
    """w:b / w:i style toggle: absent -> None (inherit), present without val or val=1/true -> True, val=0/false -> False."""
    if rpr is None:
        return None
    c = rpr.find(W + tag)
    if c is None:
        return None
    return (c.get(W + "val") or "true").lower() not in ("0", "false", "off", "none")


@dataclass
class _Level:
    fmt: str
    text: str
    start: int


def _format_num(n: int, fmt: str) -> str:
    if fmt in ("upperLetter", "lowerLetter"):
        s, m = "", n
        while m > 0:
            m, r = divmod(m - 1, 26)
            s = chr(65 + r) + s
        return s if fmt == "upperLetter" else s.lower()
    if fmt in ("upperRoman", "lowerRoman") and 0 < n < 4000:   # outside Roman range (or hostile start values) fall through to digits
        vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
        s, m = "", n
        for v, t in vals:
            while m >= v:
                s, m = s + t, m - v
        return s if fmt == "upperRoman" else s.lower()
    if fmt == "decimalZero":
        return f"{n:02d}"
    return str(n)


class _Ctx:
    def __init__(self, doc):
        self.doc = doc
        self.rels = doc.part.rels
        self.cur_rels = self.rels   # relationships of the part being read (footnotes have their own)
        self.styles = {s.get(W + "styleId"): s for s in doc.styles.element.findall(W + "style")}
        self.nums: dict[str, dict[int, _Level]] = {}
        self.style_level: dict[tuple[str, str], int] = {}   # (numId, styleId) -> level a style is linked to in the abstract definition
        self.counters: dict[tuple[str, int], int] = {}
        self.footnotes: dict[str, str] = {}
        self.used_notes: list[str] = []
        self.images = 0
        self.alt_images = 0
        self._load_numbering()
        self._load_footnotes()

    def _load_numbering(self) -> None:
        try:
            root = self.doc.part.numbering_part.element
        except Exception:  # noqa: BLE001 - document without numbering definitions
            return
        absn = {a.get(W + "abstractNumId"): a for a in root.findall(W + "abstractNum")}
        for num in root.findall(W + "num"):
            a = absn.get(_val(num, "abstractNumId"))
            if a is None:
                continue
            lv = {}
            for l in a.findall(W + "lvl"):
                if _val(l, "pStyle"):
                    self.style_level[(num.get(W + "numId"), _val(l, "pStyle"))] = int(l.get(W + "ilvl", "0"))
                lv[int(l.get(W + "ilvl", "0"))] = _Level(_val(l, "numFmt", "decimal"), _val(l, "lvlText", ""), int(_val(l, "start", "1")))
            for o in num.findall(W + "lvlOverride"):
                s = o.find(W + "startOverride")
                i = int(o.get(W + "ilvl", "0"))
                rep = o.find(W + "lvl")   # a full replacement level definition for this list instance
                if rep is not None:
                    base = lv.get(i, _Level("decimal", "%1.", 1))
                    lv[i] = _Level(_val(rep, "numFmt", base.fmt), _val(rep, "lvlText", base.text), int(_val(rep, "start", str(base.start))))
                if s is not None and i in lv:
                    lv[i] = _Level(lv[i].fmt, lv[i].text, int(s.get(W + "val", "1")))
            self.nums[num.get(W + "numId")] = lv

    def _load_footnotes(self) -> None:
        for rel in self.rels.values():
            if rel.reltype.endswith("/footnotes") and not rel.is_external:
                from lxml import etree

                root = etree.fromstring(rel.target_part.blob)
                self.cur_rels = rel.target_part.rels
                for fn in root.findall(W + "footnote"):
                    if fn.get(W + "type") in ("separator", "continuationSeparator", "continuationNotice"):
                        continue
                    segs = []
                    for p in fn.findall(W + "p"):
                        segs.extend(self.runs(p, None))
                        segs.append(Seg(" "))
                    self.footnotes[fn.get(W + "id")] = inline(segs).strip()
                self.cur_rels = self.rels

    # ------------------------------------------------------------------ styles
    def chain(self, style_id: str | None):
        seen = 0
        while style_id and style_id in self.styles and seen < 12:
            st = self.styles[style_id]
            yield st
            style_id = _val(st, "basedOn")
            seen += 1

    def style_name(self, style_id: str | None) -> str:
        st = self.styles.get(style_id or "")
        return _val(st, "name", "") or "" if st is not None else ""

    def style_prop(self, style_id: str | None, tag: str):
        for st in self.chain(style_id):
            ppr = st.find(W + "pPr")
            if ppr is not None and ppr.find(W + tag) is not None:
                return ppr.find(W + tag)
        return None

    def run_flag(self, r, pstyle: str | None, tag: str) -> bool:
        rpr = r.find(W + "rPr")
        v = _on(rpr, tag)
        if v is not None:
            return v
        for sid in (_val(rpr, "rStyle"), pstyle):
            for st in self.chain(sid):
                v = _on(st.find(W + "rPr"), tag)
                if v is not None:
                    return v
        return False

    def run_font(self, r, pstyle: str | None) -> str:
        rpr = r.find(W + "rPr")
        for node in [rpr] + [st.find(W + "rPr") for sid in (_val(rpr, "rStyle"), pstyle) for st in self.chain(sid)]:
            f = node.find(W + "rFonts") if node is not None else None
            if f is not None and f.get(W + "ascii"):
                return f.get(W + "ascii")
        return ""

    # ------------------------------------------------------------------ inline content
    def runs(self, p, pstyle: str | None, link: str | None = None, heading: bool = False) -> list[Seg]:
        out: list[Seg] = []
        for ch in p:
            tag = ch.tag
            if tag == W + "r":
                out.extend(self.run(ch, pstyle, link, heading))
            elif tag == W + "hyperlink":
                rid = ch.get(R + "id")
                rels = self.cur_rels
                target = rels[rid].target_ref if rid and rid in rels and rels[rid].is_external else None
                anchor = ch.get(W + "anchor")
                if target and anchor:
                    target += "#" + anchor
                out.extend(self.runs(ch, pstyle, target or link, heading))
            elif tag in (W + "ins", W + "smartTag", W + "sdtContent", W + "fldSimple", W + "customXml") or tag == W + "sdt":
                out.extend(self.runs(ch.find(W + "sdtContent") if tag == W + "sdt" and ch.find(W + "sdtContent") is not None else ch,
                                     pstyle, link, heading))
        return out

    def run(self, r, pstyle: str | None, link: str | None, heading: bool) -> list[Seg]:
        bold = False if heading else self.run_flag(r, pstyle, "b")
        italic = False if heading else self.run_flag(r, pstyle, "i")
        mono = bool(_MONO.search(self.run_font(r, pstyle)))
        va = _val(r.find(W + "rPr"), "vertAlign")
        sup, sub = va == "superscript", va == "subscript"
        segs: list[Seg] = []
        buf: list[str] = []

        def flush():
            if buf:
                segs.append(Seg("".join(buf), bold, italic, mono, sup, sub, link))
                buf.clear()

        for c in r:
            t = c.tag
            if t == W + "t":
                buf.append(c.text or "")
            elif t == W + "tab":
                buf.append("\t")   # kept for code blocks; prose rendering turns it into a space
            elif t in (W + "br", W + "cr"):
                if c.get(W + "type") == "page":
                    continue
                buf.append("\n")
            elif t == W + "noBreakHyphen":
                buf.append("-")
            elif t == W + "footnoteReference":
                fid = c.get(W + "id")
                if fid in self.footnotes:
                    flush()
                    if fid not in self.used_notes:
                        self.used_notes.append(fid)
                    segs.append(Seg(f"[^{fid}]", raw=True))
            elif t == W + "drawing" or t == W + "pict":
                self.images += 1
                doc_pr = next(iter(c.iter(WP + "docPr")), None)
                alt = ((doc_pr.get("descr") or doc_pr.get("title") or "") if doc_pr is not None else "").strip()
                if alt:
                    self.alt_images += 1
                    flush()
                    segs.append(Seg(f"[Image: {alt}]", italic=True))
        flush()
        return segs

    # ------------------------------------------------------------------ lists / headings
    def num_of(self, p) -> tuple[str, int] | None:
        ppr = p.find(W + "pPr")
        num = ppr.find(W + "numPr") if ppr is not None else None
        sid = _val(ppr, "pStyle")
        if num is None:
            num = self.style_prop(sid, "numPr")
        if num is None:
            return None
        nid = _val(num, "numId")
        if not nid or nid == "0" or nid not in self.nums:
            return None
        ilvl = _val(num, "ilvl")
        if ilvl is None:   # a style-linked level (Heading numbering): the abstract definition names the style it belongs to
            ilvl = next((str(self.style_level[(nid, st.get(W + "styleId"))]) for st in self.chain(sid)
                         if (nid, st.get(W + "styleId")) in self.style_level), "0")
        return nid, int(ilvl)

    def label(self, nid: str, ilvl: int) -> tuple[str, str, int]:
        """Advance the counters for this list level; returns (text of the label, number format, counter)."""
        lv = self.nums[nid]
        cur = lv.get(ilvl, _Level("decimal", "%1.", 1))
        key = (nid, ilvl)
        self.counters[key] = self.counters.get(key, cur.start - 1) + 1
        for k in [k for k in self.counters if k[0] == nid and k[1] > ilvl]:
            del self.counters[k]

        def sub(m):
            i = int(m.group(1)) - 1
            l2 = lv.get(i, _Level("decimal", "", 1))
            return _format_num(self.counters.get((nid, i), l2.start), l2.fmt)

        return re.sub(r"%(\d)", sub, cur.text), cur.fmt, self.counters[key]

    def heading_level(self, p) -> int | None:
        ppr = p.find(W + "pPr")
        sid = _val(ppr, "pStyle")
        name = self.style_name(sid).lower().replace("_", " ")
        m = re.match(r"heading\s*(\d)$", name)
        if m:
            return min(6, int(m.group(1)))
        if name == "title":
            return 1
        ol = ppr.find(W + "outlineLvl") if ppr is not None else None
        if ol is None:
            ol = self.style_prop(sid, "outlineLvl")
        if ol is not None and ol.get(W + "val", "9").isdigit() and int(ol.get(W + "val")) < 6:
            return int(ol.get(W + "val")) + 1
        return None


def _plain(segs: list[Seg]) -> str:
    return re.sub(r"\s+", " ", "".join(s.text for s in segs)).strip()


def _is_bullet(lab: str, fmt: str) -> bool:
    return fmt in ("bullet", "none") or not lab.strip() or not re.search(r"[A-Za-z0-9]", lab)


def _cell_text(ctx: _Ctx, tc) -> str:
    parts: list[str] = []
    for ch in tc:
        if ch.tag == W + "p":
            ppr = ch.find(W + "pPr")
            t = inline(ctx.runs(ch, _val(ppr, "pStyle")))
            num = ctx.num_of(ch)
            if num:   # advance the list counters here too, so numbering after the table continues correctly
                lab, fmt, _ = ctx.label(*num)
                if t:
                    t = ("• " if _is_bullet(lab, fmt) else lab.strip() + " ") + t
            if t:
                parts.append(t)
        elif ch.tag == W + "tbl":
            parts.extend("; ".join(c for c in r if c) for r in _grid(ctx, ch) if any(r))
    return "\n".join(parts).replace("  \n", "\n")


def _grid(ctx: _Ctx, tbl) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in tbl.findall(W + "tr"):
        trpr = tr.find(W + "trPr")
        before = trpr.find(W + "gridBefore") if trpr is not None else None
        raw_before = (before.get(W + "val") or "0") if before is not None else "0"   # columns skipped at the start of the row
        row: list[str] = [""] * (min(int(raw_before), MAX_SPAN) if raw_before.isdigit() else 0)
        for tc in tr.findall(W + "tc"):
            tcpr = tc.find(W + "tcPr")
            span = 1
            if tcpr is not None and tcpr.find(W + "gridSpan") is not None:
                raw = tcpr.find(W + "gridSpan").get(W + "val", "1") or "1"
                span = max(1, min(int(raw), MAX_SPAN)) if raw.isdigit() else 1
            vm = tcpr.find(W + "vMerge") if tcpr is not None else None
            cont = vm is not None and (vm.get(W + "val") or "continue") == "continue"
            row.append("" if cont else _cell_text(ctx, tc))
            row.extend([""] * (span - 1))
        rows.append(row[:MAX_COLS])
    return rows


def _table_blocks(ctx: _Ctx, tbl) -> list[Block]:
    rows = [r for r in _grid(ctx, tbl) if any(c.strip() for c in r)]
    if not rows:
        return []
    ncol = max(len(r) for r in rows)
    if len(rows) * ncol > MAX_TABLE_CELLS:
        raise DocxError("A table in the document is too large to convert.")
    keep = [i for i in range(ncol) if any(i < len(r) and r[i].strip() for r in rows)]
    rows = [[r[i] if i < len(r) else "" for i in keep] for r in rows]
    if len(keep) == 1:   # a one-column table is a call-out box
        return [Raw("\n".join("> " + ln for ln in r[0].split("\n"))) for r in rows]
    return [Table(rows)]


def convert(path: str) -> tuple[list[Block], dict]:
    import docx
    from docx.opc.exceptions import PackageNotFoundError

    try:
        doc = docx.Document(path)
    except PackageNotFoundError as e:
        raise DocxError("The file is not a valid DOCX document.") from e
    except Exception as e:  # noqa: BLE001 - corrupt zip / xml
        raise DocxError(f"Cannot open the DOCX: {e}") from e
    ctx = _Ctx(doc)
    blocks: list[Block] = []
    code_run: list[str] = []
    stats = {"toc_paragraphs_removed": 0}

    def flush_code():
        if code_run:
            blocks.append(Code(list(code_run)))
            code_run.clear()

    def paragraph(p):
        ppr = p.find(W + "pPr")
        sid = _val(ppr, "pStyle")
        sname = ctx.style_name(sid)
        if re.match(r"(?i)^(toc|table of figures|index)\b", sname) or sname.lower() == "toc heading":
            stats["toc_paragraphs_removed"] += 1
            if blocks and isinstance(blocks[-1], Heading):
                blocks[-1].toc = True
            return
        level = ctx.heading_level(p)
        segs = ctx.runs(p, sid, heading=bool(level))
        text = _plain(segs)
        num = ctx.num_of(p)
        if level:
            flush_code()
            if not text:
                return
            if num:
                lab, _, _ = ctx.label(*num)
                lab = lab.strip()
                if lab and not re.match(rf"{re.escape(lab.rstrip('.'))}\.?(?:\s|$)", text):
                    text = f"{lab} {text}"
            blocks.append(Heading(level, text))
            return
        mono = bool(segs) and all(s.code or not s.text.strip() for s in segs) and any(s.text.strip() for s in segs)
        if _CODE_STYLE.search(sname) or (mono and not num):
            code_run.append("".join(s.text for s in segs).replace("\n", "\n").rstrip())
            return
        flush_code()
        if not text and not any(s.text.startswith("[Image") for s in segs):
            return
        if num:
            lab, fmt, n = ctx.label(*num)
            depth = min(max(num[1], 0), 8)
            ms = re.search(r"(?i)^list\s+\w+\s+(\d)$", sname)
            if ms and not depth:
                depth = int(ms.group(1)) - 1
            if _is_bullet(lab, fmt):
                blocks.append(Item(depth, segs))
            elif fmt in ("decimal", "decimalZero") and re.fullmatch(r"%?\d+[.)]?|\d+[.)]?", lab.strip()):
                blocks.append(Item(depth, segs, True, str(n)))
            else:
                blocks.append(Item(depth, segs, True, "1", lab.strip()))
            return
        m = re.match(r"(?i)^list\s+(bullet|number)(?:\s+(\d))?$", sname)
        if m:
            blocks.append(Item(int(m.group(2) or 1) - 1, segs, m.group(1).lower() == "number", "1", "" if m.group(1).lower() == "bullet" else "1."))
            return
        if _QUOTE_STYLE.match(sname):
            blocks.append(Quote(segs))
            return
        blocks.append(Para(segs))

    def walk(parent):
        for ch in parent:
            if ch.tag == W + "p":
                paragraph(ch)
            elif ch.tag == W + "tbl":
                flush_code()
                blocks.extend(_table_blocks(ctx, ch))
            elif ch.tag == W + "sdt":
                c = ch.find(W + "sdtContent")
                if c is not None:
                    walk(c)

    walk(doc.element.body)
    flush_code()
    # a "Contents" heading whose entries were table-of-contents paragraphs
    blocks = [b for i, b in enumerate(blocks)
              if not (isinstance(b, Heading) and b.toc and re.fullmatch(r"(table of )?contents", b.text, re.I)
                      and (i + 1 == len(blocks) or isinstance(blocks[i + 1], Heading)))]
    cp = doc.core_properties
    meta = {"title": (cp.title or "").strip(), "footnotes": [(f, ctx.footnotes[f]) for f in ctx.used_notes],
            "images_skipped": ctx.images - ctx.alt_images, **stats}
    return blocks, meta
