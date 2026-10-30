"""Read-only questions about the parsed specifications, answered as Markdown for coding agents (the MCP server, mcp_server.py).

Everything comes from the parsed model in the workspace database (ir_node: sections, tables and normative statements with stable
keys, built from the Docling conversion), not from the flat Markdown: a command, a Command Dword table or a byte of a data
structure is then an exact lookup with its figure number and page, instead of a text search that hopes the right table is
nearby. The Docling Markdown stays reachable through read_raw for anything the parser did not model.

  documents            every parsed document, one parse run each (the active one, else the newest successful one)
  resolve_doc          "base" / "NVMe-MI" / "nvme-nvm-cs@1.3" + "2.4" / "latest" / "previous" -> one document
  search               keyword search (SQLite FTS5) over sections, tables and table rows, with NVMe abbreviations expanded
  section / table      one section (optionally with its sub-sections) / one figure, with captions, pages and parser notes
  command / field      a command (opcode, section, Command Dword tables) / one Command Dword or field and the values it refers to
  structure            a data structure, log page or descriptor, optionally narrowed to a byte offset or a field
  reference            "Figure 312", "section 5.2.1 of the NVM Command Set Specification" -> the node it names
  diff / changes       what changed between two versions (computed on demand through the job queue, then kept)
  raw                  the Docling Markdown of a document, paged, around a phrase, one section or a line range
  library_entries      a converted-only document (To Markdown library, not parsed) cut into search entries: its title, section
                       text in pieces, tables and table rows, each with its line numbers to open with read_raw

Answers are capped at PAGE_CHARS characters; longer ones end with the page to ask for next. A version that was not asked for is
the latest parsed one, and the answer says so.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from . import compare, jobs
from .catalog import _split_field
from .compare_entities import _Version, _ck, _name_of, describe
from .core.config import workspace_dir
from .core.db import connect, jloads, row, rows
from .ingest import store
from .ingest.normalize import SPEC_ALIASES, _ALIAS_RE, clean, extract_references, norm_title, parse_byte_range

PAGE_CHARS = 120_000            # about 30k tokens per answer: a whole Identify Controller structure fits in one page
INDEX_VERSION = "3"             # bump when what the search index holds changes: every run is indexed again
MAX_HITS = 25
CELL_CHARS = 1500

FAMILY_NAMES = {"nvme-base": "NVMe Base", "nvme-nvm-cs": "NVM Command Set", "nvme-zns-cs": "Zoned Namespace Command Set",
                "nvme-kv-cs": "Key Value Command Set", "nvme-slm-cs": "Subsystem Local Memory Command Set", "nvme-mi": "NVMe-MI",
                "nvme-pcie": "PCIe Transport", "nvme-tcp": "TCP Transport", "nvme-rdma": "RDMA Transport"}
# short names an agent is likely to use for a family
FAMILY_ALIASES = {"base": "nvme-base", "nvme": "nvme-base", "nvme base": "nvme-base", "mi": "nvme-mi", "nvme mi": "nvme-mi",
                  "nvmemi": "nvme-mi", "management interface": "nvme-mi", "nvm": "nvme-nvm-cs", "nvm cs": "nvme-nvm-cs", "nvmcs": "nvme-nvm-cs",
                  "nvm command set": "nvme-nvm-cs", "command set": "nvme-nvm-cs", "zns": "nvme-zns-cs", "kv": "nvme-kv-cs", "key value": "nvme-kv-cs",
                  "slm": "nvme-slm-cs", "pcie": "nvme-pcie", "tcp": "nvme-tcp", "rdma": "nvme-rdma"}
# NVMe abbreviations: a query term also matches its spelled-out form
ABBREVIATIONS = {
    "cdw": "command dword", "dw": "dword", "sqe": "submission queue entry", "cqe": "completion queue entry", "sq": "submission queue",
    "cq": "completion queue", "nsid": "namespace identifier", "ns": "namespace", "cntlid": "controller identifier", "cntid": "controller identifier",
    "lba": "logical block address", "lbaf": "lba format", "fid": "feature identifier", "lid": "log page identifier",
    "cns": "controller or namespace structure", "csi": "command set identifier", "aen": "asynchronous event", "aer": "asynchronous event request",
    "pi": "protection information", "hmb": "host memory buffer", "cmb": "controller memory buffer", "pmr": "persistent memory region",
    "sgl": "scatter gather list", "prp": "physical region page", "fw": "firmware", "ctrl": "controller", "endgid": "endurance group identifier",
    "ana": "asymmetric namespace access", "mptr": "metadata pointer", "dptr": "data pointer", "cid": "command identifier",
    "nlb": "number of logical blocks", "slba": "starting lba", "fua": "force unit access", "lr": "limited retry", "numd": "number of dwords",
    "numdl": "number of dwords lower", "numdu": "number of dwords upper", "rae": "retain asynchronous event", "lsp": "log specific",
    "sel": "select", "sv": "save", "nmimt": "message type", "mctp": "management component transport protocol", "vpd": "vital product data",
    "smart": "health information", "dsm": "dataset management", "ses": "secure erase settings", "nvmset": "nvm set", "rr": "read recovery",
    "pel": "persistent event log", "tnvmcap": "total nvm capacity", "mdts": "maximum data transfer size", "oacs": "optional admin command support",
    "oncs": "optional nvm command support", "vid": "pci vendor id", "sn": "serial number", "mn": "model number", "fr": "firmware revision",
}
_STOP = {"what", "is", "are", "the", "a", "an", "of", "for", "in", "on", "to", "how", "does", "do", "which", "with", "and", "or", "spec",
         "specification", "specifications", "version", "revision", "rev", "about", "me", "show", "tell", "explain", "define", "defined",
         "definition", "describe", "there", "this", "that", "be", "by", "nvme", "express"}
_SPLIT = re.compile(r"\b(dword|dw|cdw|nmd|bits?|bytes?|figure|fig|section|sec|offset)(\d+)\b", re.I)
_VERSION_IN = re.compile(r"(?<![\d.])(?:v|rev(?:ision)?\s*)?(\d+\.\d+(?:\.\d+)?[a-z]?)(?![\d.])", re.I)
_DWORD = re.compile(r"\b(?:command\s+)?dword\s*(\d+)\b|\bcdw\s*(\d+)\b|\bdw\s*(\d+)\b|\bnmd\s*([01])\b", re.I)
# "Command Dword 10", "Command Dwords 10 and 11", NVMe-MI "NMD0" / "NVMe Management Dword 0" (checked first: it also says "Dword 0")
_CAP_DWORDS = re.compile(r"\b(?:NMD\s*|management\s+dword\s*)([01])\b|\b(?:(?:command|request)\s+)?dwords?\s+(\d+)(?:\s*(?:to|-|–|—|and)\s*(\d+))?", re.I)
FAILED_FOR = 600        # seconds a failed comparison is reported instead of being started again
LIB_INDEX_VERSION = "2"   # bump when library_entries changes: every converted document is indexed again
LIB_PIECE = 1500        # characters of section text per search entry of a converted document
LOW_QUALITY = 0.45      # a table extracted worse than this is flagged in answers
MD_CACHE = 6            # Markdown files read_raw keeps in memory (text, lines, section outline)
REF_ROWS = 80           # a referenced table up to this size is printed with the answer; a bigger one is named, to open with get_table


class QueryError(ValueError):
    """A question the data cannot answer as asked (unknown spec, version, section ...): the message says what exists instead."""


class NotFound(QueryError):
    """Nothing matched (counted as a miss on the admin MCP page)."""


# ----------------------------------------------------------------------------- small helpers

def version_key(v: str) -> list:
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", v or "") if t]


def _norm_version(v: str) -> str:
    v = re.sub(r"^(?:v|rev(?:ision)?)\s*", "", (v or "").strip().lower())
    while re.search(r"\.\d+\.0$", v) or re.fullmatch(r"\d+\.\d+\.0", v):
        v = v[:-2]
    return v


def spec_name(d: dict) -> str:
    return f"{FAMILY_NAMES.get(d['family'], d['family'])} {d['version']}"


def _cell(s: Any) -> str:
    s = clean(str(s or ""))
    if len(s) > CELL_CHARS:
        s = s[:CELL_CHARS] + " …"
    return s.replace("|", "\\|")


def md_table(header: list, data: list[list]) -> str:
    width = max([len(header or [])] + [len(r) for r in data] + [1])
    head = [(_cell(h) or " ") for h in (header or [])] + [" "] * (width - len(header or []))
    if not any(h.strip() for h in head):
        head = [f"Col {i + 1}" for i in range(width)]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    for r in data:
        cells = [_cell(c) for c in r] + [""] * (width - len(r))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def pages(a: int | None, b: int | None) -> str:
    if not a:
        return ""
    return f"p. {a}" if not b or b == a else f"pp. {a}–{b}"


def uri(d: dict, node: str) -> str:
    return f"specforge://{d['family']}@{d['version']}/{node}"


def paginate(text: str, page: int = 1, limit: int = PAGE_CHARS) -> str:
    """One page of a long answer, cut at a line boundary, ending with how to get the next one."""
    page = max(1, int(page or 1))
    if len(text) <= limit and page == 1:
        return text
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            nl = text.rfind("\n", start + limit // 2, end)
            end = nl + 1 if nl > 0 else end
        chunks.append(text[start:end])
        start = end
    if page > len(chunks):
        raise QueryError(f"there are only {len(chunks)} page(s) in this answer")
    body = chunks[page - 1]
    tail = f"\n\n---\n_Page {page} of {len(chunks)}." + (f" Call again with page={page + 1} for the rest._" if page < len(chunks) else "_")
    return body + tail


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _similar(a: str, b: str) -> float:
    """Word overlap of two names (Dice), with an exact match of the normalised names scoring highest."""
    na, nb = norm_title(a), norm_title(b)
    if na and na == nb:
        return 2.0
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return 2 * len(wa & wb) / (len(wa) + len(wb))


def name_score(q: str, name: str) -> float:
    """How well a name answers a query: every query word in the name (coverage) and the query as one phrase in it count most,
    word overlap breaks ties ('Identify Controller' is the Identify Controller Data Structure, not the Primary Controller
    Capabilities one). 3.0 for the same name."""
    nq, nn = norm_title(q), norm_title(name)
    if not nq or not nn:
        return 0.0
    if nq == nn:
        return 3.0
    wq, wn = _words(q), _words(name)
    if not wq or not wn:
        return 0.0
    cover = len(wq & wn) / len(wq)
    phrase = 0.5 if re.search(rf"(?<![a-z0-9]){re.escape(' '.join(re.findall(r'[a-z0-9]+', nq)))}(?![a-z0-9])",
                              " ".join(re.findall(r"[a-z0-9]+", nn))) else 0.0
    return cover + phrase + 0.3 * (2 * len(wq & wn) / (len(wq) + len(wn)))


def fig_no(label: str) -> str:
    """The figure number as printed ('338' for the parser's '338#2'; uncaptioned 'p367t1' stays as it is)."""
    return label.split("#", 1)[0]


def _sentence_like(caption: str) -> bool:
    """A 'caption' that is really a sentence of body text picked up beside a figure number."""
    c = _name_of(caption)
    return len(c) > 110 or c.endswith((".", ";", ":")) or bool(re.match(r"^(?:the|if|a|an|unused|this|that|when|for)\b", c, re.I))


def _desc(r: list) -> str:
    """The description cell of a field row: the last non-empty cell (Bits | Description, Bytes | O/M | Description); of a
    sub-row (logical_tables), its text after the bits."""
    d = next((clean(x) for x in reversed(r) if clean(x)), "")
    return _SUB_HEAD.sub("", d) if d.startswith(SUB.strip()) else d


_SUB_HEAD = re.compile(r"^↳\s*(?:[^·]{0,24}·\s*)?")


def _sub_bits(r: list) -> str:
    """The bits a sub-row starts with ("16" of "↳ 16 · MDTS and Size Limits ... (MEM): ..."), or ""."""
    m = re.match(r"^↳\s*([^·]{1,24}?)\s*·", clean(r[-1]))
    return m.group(1) if m else ""


class Answer(str):
    """An answer's Markdown with structured data beside it (.data): the MCP tool returns both, the text for the agent to read
    and the data for one that fills in code from it."""
    data: dict


def answer(text: str, data: dict | None) -> str:
    if not data:
        return text
    a = Answer(text)
    a.data = data
    return a


SUB = "↳ "      # first characters of a sub-row: a nested bit table's row, or text continued from the row above, in the last column


def is_sub(r: list) -> bool:
    return bool(r) and not clean(r[0]) and clean(r[-1]).startswith(SUB.strip())


def _align(hp: list[int], kp: list[int]) -> dict[int, int]:
    """Header cell positions -> data column positions, in order, each at most once, as close as possible: a header cell over a
    merged span (or a group heading such as "Controller Capabilities and Features" in the header row) does not push the
    labels after it onto the wrong columns. Edit-distance alignment; skipping a header cell or a column costs SKIP."""
    SKIP = 2.5
    n, m = len(hp), len(kp)
    cost = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        cost[i][0] = i * SKIP
    for j in range(1, m + 1):
        cost[0][j] = j * SKIP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i][j] = min(cost[i - 1][j - 1] + abs(hp[i - 1] - kp[j - 1]), cost[i - 1][j] + SKIP, cost[i][j - 1] + SKIP)
    out, i, j = {}, n, m
    while i and j:
        if cost[i][j] == cost[i - 1][j - 1] + abs(hp[i - 1] - kp[j - 1]):
            out[hp[i - 1]] = kp[j - 1]
            i, j = i - 1, j - 1
        elif cost[i][j] == cost[i - 1][j] + SKIP:
            i -= 1
        else:
            j -= 1
    return out


def _layout(header: list[str], rows: list[list[str]]) -> tuple[list[str], list[tuple[list[str], bool]]]:
    """(labels, [(cells, is a sub-row)]) of one table piece as the PDF parser left it: columns empty in every row are dropped
    and the header labels put over the columns they belong to. With a heading over the first column, a row whose first
    column is empty is a sub-row: a bit table nested in the row above, or its description continued."""
    width = max([len(header), *(len(r) for r in rows)] or [0])
    pad = lambda r: [*r, *[""] * (width - len(r))]  # noqa: E731
    rows = [pad(r) for r in rows]
    header = pad(header)
    keep = [i for i in range(width) if any(clean(r[i]) for r in rows)]
    hp = [i for i in range(width) if clean(header[i])]
    where = _align(hp, keep)
    labels = [""] * len(keep)
    for h, k in where.items():
        labels[keep.index(k)] = clean(header[h])
    nested = bool(labels and labels[0])
    out = []
    for r in rows:
        cells = [r[i] for i in keep]
        filled = [k for k, c in enumerate(cells) if clean(c)]
        if nested and filled and filled[0] > 0 and len(filled) >= max(3, len(cells) - 2):
            cells = cells[filled[0]:] + [""] * filled[0]       # a whole row the parser shifted right: not a sub-row
        out.append((cells, nested and not clean(cells[0]) and bool(filled)))
    return labels, out


_NESTED_HEADER = re.compile(r"(?i)(bits?|bytes?|value|offset)(\s*·\s*(description|definition|value|name|field|o/m|attribute))+")


def _label_key(h: str) -> str:
    return re.sub(r"[^a-z/]", "", clean(h).lower())


def logical_tables(raw: dict[str, dict]) -> tuple[dict[str, dict], dict[str, str]]:
    """(tables, alias): tables as a reader sees them. Empty spacer columns are dropped and header labels aligned to the data
    (_layout); a table the PDF parser cut into pieces (continued over pages: its caption again, or an uncaptioned piece, in
    the same section) becomes one table under the label of its first piece, with repeated rows dropped. A piece's rows go
    under the columns of the same name; rows that fit no column (a nested bit table, continued text) become sub-rows: the
    cells joined, after SUB, in the last column. alias maps every piece's label to that table."""
    out: dict[str, dict] = {}
    alias: dict[str, str] = {}
    named: dict | None = None           # the last captioned table with rows in the current section: what a piece continues

    def place(g: dict, labels: list[str], laid: list[tuple[list[str], bool]]) -> None:
        main = g["header"]
        w = len(main)
        keys = [_label_key(h) for h in main]
        same = labels == main or (len(labels) == w and not any(labels))
        by_name = None
        if not same and labels and labels[0] and _label_key(labels[0]) == keys[0]:
            # columns by heading; a column without one (a nested table's) adds its text to the last column
            idx = [(keys.index(_label_key(h)) if _label_key(h) in keys else None) if h else w - 1 for h in labels]
            named_idx = [k for k, h in zip(idx, labels) if h]
            if None not in idx and len(set(named_idx)) == len(named_idx):
                by_name = idx
        for cells, sub in laid:
            if not sub and same:
                r = list(cells)
            elif not sub and by_name is not None:
                r = [""] * w
                for c, k in zip(cells, by_name):
                    if clean(c):
                        r[k] = f"{r[k]} {c}".strip() if r[k] else c
            else:
                text = " · ".join(clean(c) for c in cells if clean(c))
                if not text or _NESTED_HEADER.fullmatch(text):     # empty, or a nested table's own header row
                    continue
                r = [""] * (w - 1) + [SUB + text] if w > 1 else [SUB + text]
            sig = tuple(clean(x) for x in r)
            if sig not in g["_seen"]:
                g["_seen"].add(sig)
                g["rows"].append(r)

    for lab, t in raw.items():
        labels, laid = _layout(t.get("header") or [], t.get("rows") or [])
        name = norm_title(_name_of(t.get("caption"))) if t.get("captioned", True) and t.get("caption") else ""
        if named is not None and named["section"] != t.get("section"):
            named = None
        if named is not None and laid and ((name and name == named["_name"]) or not t.get("captioned", True)):
            place(named, labels, laid)
            named["page_to"] = max(named.get("page_to") or 0, t.get("page_to") or t.get("page_from") or 0) or named.get("page_to")
            named["parts"].append(lab)
            n0, n1 = named["_n"], len(laid)                  # quality weighted by rows: one bad scrap does not flag the table
            named["quality"] = ((named.get("quality") or 1.0) * n0 + (t.get("quality") or 1.0) * n1) / max(1, n0 + n1)
            named["_n"] = n0 + n1
            alias[lab] = named["label"]
            continue
        g = {**t, "header": labels, "rows": [], "parts": [lab], "_name": name, "_seen": set(), "_n": len(laid)}
        place(g, labels, laid)
        out[lab] = g
        alias[lab] = lab
        if name and laid and not _sentence_like(t.get("caption") or ""):
            named = g
    for g in out.values():
        g.pop("_seen", None)
        g.pop("_name", None)
        g.pop("_n", None)
    return out, alias


# ----------------------------------------------------------------------------- the library

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_NUMBERED = re.compile(r"^((?:\d+|[A-Z](?=\.\d))(?:\.\d+)*)\.?\s+(\S.*)$")
_BOLD_LINE = re.compile(r"^\*\*(.+?)\*\*$")
_CAPTION = re.compile(r"^(?:\*\*)?\s*(Figure|Table)\s+(\d+[A-Za-z]?)\s*[:.\u2013\u2014-]\s*(.*?)\s*(?:\*\*)?$", re.I)
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|?$")
_TOC_TITLE = re.compile(r"^(table of (contents|figures|tables)|contents|list of (figures|tables))$", re.I)


def md_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    """(the YAML header's key: value pairs, the first line after it) of a converted file."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    meta = {}
    for i in range(1, min(len(lines), 60)):
        if lines[i].strip() == "---":
            return meta, i + 1
        k, _, v = lines[i].partition(":")
        meta[k.strip()] = v.strip().strip('"')
    return {}, 0


def md_outline(lines: list[str]) -> list[dict]:
    """The headings of a converted file: {line, end, level, number, title, skip}. Besides Markdown headings, a bold line holding only a
    section number with a bold title on the next line ("**5.2.14.2.1**" / "**Identify Controller Data Structure (CNS 01h)**", as the
    PDF converters leave headings they did not recognise) counts. A numbered section ends at the next numbered heading at its depth or
    above (stray unnumbered headings inside it do not end it); an unnumbered one at the next heading at its level or above."""
    heads, i = [], md_front_matter(lines)[1]
    while i < len(lines):
        ln = lines[i].strip()
        h = None
        if m := _MD_HEADING.match(ln):
            title = clean(m.group(2).strip("*"))
            n = _NUMBERED.match(title)
            h = {"line": i, "level": len(m.group(1)), "number": n.group(1) if n else "", "title": n.group(2) if n else title, "skip": i}
        elif (m := _BOLD_LINE.match(ln)) and not _CAPTION.match(inner := m.group(1).strip()):
            if re.fullmatch(r"\d+(?:\.\d+)+", inner):
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and (t := _BOLD_LINE.match(lines[j].strip())) and not _CAPTION.match(t.group(1)):
                    h = {"line": i, "level": 0, "number": inner, "title": clean(t.group(1)), "skip": j}
            elif (n := _NUMBERED.match(inner)) and "." in n.group(1) and n.group(2)[0].isalpha():
                h = {"line": i, "level": 0, "number": n.group(1), "title": clean(n.group(2)), "skip": i}
        if h:
            heads.append(h)
            i = h["skip"]
        i += 1
    for k, h in enumerate(heads):
        h["end"] = len(lines)
        depth = h["number"].count(".") + 1 if h["number"] else None
        for nxt in heads[k + 1:]:
            if (depth and nxt["number"] and nxt["number"].count(".") + 1 <= depth) or \
               (not depth and (nxt["number"] or (nxt["level"] and nxt["level"] <= h["level"]))):
                h["end"] = nxt["line"]
                break
    return heads


def _row_cells(line: str) -> list[str]:
    return [clean(c) for c in line.strip().strip("|").split("|")]


def lib_key(rid: str, sec: str, kind: str, lo: int, hi: int, row: int | None = None) -> str:
    """The key of a converted document's search entry: rid|<section>|<S section text, T table><from>-<to>[#<row line>].
    parse_lib_key reads it back; nothing else builds or splits these keys."""
    return f"{rid}|{sec.replace('|', '/')}|{kind}{lo}-{hi}" + (f"#{row}" if row is not None else "")


def parse_lib_key(key: str) -> dict | None:
    m = re.fullmatch(r"(lib:[^|]*)\|([^|]*)\|([ST])(\d+)-(\d+)(?:#(\d+))?", key)
    if not m:
        return None
    return {"rid": m.group(1), "sec": m.group(2), "kind": m.group(3), "lo": int(m.group(4)), "hi": int(m.group(5)),
            "row": int(m.group(6)) if m.group(6) else None}


def library_entries(text: str, rid: str, filename: str) -> list[tuple]:
    """Search entries (number, title, body, run_id, key, kind, line) of one converted document: one for the document (key rid|doc),
    section text in pieces of about LIB_PIECE characters, tables and their rows (keys: lib_key). Line numbers count from 1.
    A table continues across a repeated caption (a figure cut over pages; what lies between belongs to it) or a following table
    block with no text in between (a bit table nested in a row); any other table block is a table of its own. Left out: the table of contents, image markers,
    separator rows, spacer rows (only footnote numbers) and a header row repeated where the table continues."""
    lines = text.splitlines()
    meta, start = md_front_matter(lines)
    title = meta.get("title") or filename
    out = [("", title, f"{filename}\n{meta.get('source', '')}", rid, f"{rid}|doc", "doc", 1)]
    heads = {h["line"]: h for h in md_outline(lines)}
    skip_to, cur = -1, None
    prose: list[tuple[int, str]] = []              # (line, text) of the current section; "" marks a paragraph break
    tables: list[dict] = []                        # {from, to, number, title, header, rows, sec}
    tab, caption, new_caption, text_since, in_table = None, None, False, False, False

    def flush() -> None:
        sec = cur["number"] if cur else ""
        name = cur["title"] if cur else title
        paras: list[tuple[int, int, str]] = []
        buf, b0, b1 = [], None, None
        for ln, t in prose + [(0, "")]:
            if t:
                buf.append(t)
                b0, b1 = (ln if b0 is None else b0), ln
            elif buf:
                paras.append((b0, b1, " ".join(buf)))
                buf, b0 = [], None
        piece, size, a, b = [], 0, None, None
        for p0, p1, t in paras:
            if piece and size + len(t) > LIB_PIECE:
                out.append((sec, name, "\n".join(piece), rid, lib_key(rid, sec, "S", a, b), "section", a))
                piece, size = [], 0
            while len(t) > LIB_PIECE * 2:             # one huge paragraph (a converter that lost the line breaks)
                cut = t.rfind(" ", 0, LIB_PIECE)
                cut = cut if cut > 0 else LIB_PIECE     # no space at all: cut hard
                out.append((sec, name, t[:cut], rid, lib_key(rid, sec, "S", p0, p1), "section", p0))
                t = t[cut:].strip()
            if not piece:
                a = p0
            piece.append(t)
            size, b = size + len(t), p1
        if piece:
            out.append((sec, name, "\n".join(piece), rid, lib_key(rid, sec, "S", a, b), "section", a))
        prose.clear()

    for i in range(start, len(lines)):
        if i <= skip_to:
            continue
        ln = lines[i].strip()
        if i in heads:
            flush()
            cur, skip_to = heads[i], heads[i]["skip"]
            tab, caption, new_caption, text_since, in_table = None, None, False, False, False
            continue
        if cur and not cur["number"] and _TOC_TITLE.match(cur["title"]):
            continue
        if ln.startswith("|"):
            if _TABLE_SEP.match(ln):
                continue
            cells = [c for c in _row_cells(ln) if c]
            if not in_table:                          # a table block starts: a new table, or more of the current one
                in_table = True
                sec = cur["number"] if cur else ""
                cont = tab is not None and (caption and tab["caption"] == caption[0] if new_caption else not text_since)
                if not cont and new_caption and caption:
                    # the caption repeats after text and other table blocks: all of that was inside this figure (field
                    # descriptions the converter wrote as text, bit tables after them); fold uncaptioned blocks back into it
                    k = next((k for k in range(len(tables) - 1, -1, -1) if tables[k]["caption"] == caption[0] and tables[k]["sec"] == sec), None)
                    if k is not None and all(t["caption"] is None for t in tables[k + 1:]):
                        tab = tables[k]
                        for t in tables[k + 1:]:
                            tab["rows"] += [(t["from"], t["header"])] + t["rows"]
                        del tables[k + 1:]
                        cont = True
                if not cont:
                    tab = {"from": i + 1, "to": i + 1, "caption": caption[0] if new_caption and caption else None,
                           "number": caption[1] if new_caption and caption else "",
                           "title": caption[2] if new_caption and caption else (cur["title"] if cur else title),
                           "header": cells, "rows": [], "sec": sec}
                    tables.append(tab)
                    new_caption = text_since = False
                    continue
                new_caption = text_since = False
            tab["to"] = i + 1
            if not cells or cells == tab["header"] or all(re.fullmatch(r"\d{1,2}", c) for c in cells):
                continue
            tab["rows"].append((i + 1, cells))
            continue
        in_table = False
        if not ln:
            prose.append((i + 1, ""))
            continue
        if m := _CAPTION.match(ln):
            caption, new_caption = (f"{m.group(1).title()} {m.group(2)}: {clean(m.group(3))}", f"{m.group(1).title()} {m.group(2)}",
                                    clean(m.group(3))), True
            continue
        if ln.startswith("<!--") or re.search(r"\.{5,}\s*\d+$", ln):
            continue
        text_since = True
        prose.append((i + 1, clean(ln.strip("*"))))
    flush()
    for t in tables:
        tk = lib_key(rid, t["sec"], "T", t["from"], t["to"])
        body = " | ".join(t["header"]) + "\n" + "\n".join(" | ".join(c) for _, c in t["rows"])
        out.append((t["number"], t["title"], body[:20_000], rid, tk, "table", t["from"]))
        for ln, cells in t["rows"]:
            out.append((t["number"], t["title"], " | ".join(cells), rid, lib_key(rid, t["sec"], "T", t["from"], t["to"], ln), "row", ln))
    return out


def _with_subs(rs: list[list], idx: list[int], context: set[int] | None = None) -> list[int]:
    """Row indices with the sub-rows of each: a field row brings its bit rows; a sub-row brings the field row it belongs to
    (added to `context`: shown for where the bit is, not for its own text)."""
    out: set[int] = set()
    for i in idx:
        out.add(i)
        if is_sub(rs[i]):
            p = next((k for k in range(i - 1, -1, -1) if not is_sub(rs[k])), None)
            if p is not None:
                out.add(p)
                if context is not None and p not in idx:
                    context.add(p)
            continue
        k = i + 1
        while k < len(rs) and is_sub(rs[k]):
            out.add(k)
            k += 1
    return sorted(out)


def _brief(r: list) -> list:
    """A field row with its description cut to the first sentence (shown as the parent of a bit row)."""
    d = clean(r[-1])
    m = re.match(r"(.{20,300}?[.:])\s", d)
    return [*r[:-1], (m.group(1) + " …") if m and len(m.group(1)) < len(d) else d]


def _row_name(rs: list[list], i: int) -> str:
    """How a row is named in a list: "77 MDTS", or for a bit row "CTRATT bit 16 (MEM)"."""
    name, abbr, _ = _split_field(_desc(rs[i]))
    me = abbr or name or _desc(rs[i])[:40]
    if is_sub(rs[i]):
        p = next((k for k in range(i - 1, -1, -1) if not is_sub(rs[k])), None)
        pn = (_split_field(_desc(rs[p]))[1] or clean(rs[p][0])) if p is not None else ""
        return f"{pn} bit {_sub_bits(rs[i])} ({me})".strip()
    return f"{clean(rs[i][0])} {me}".strip()


def structure_data(d: dict, ir: dict, t: dict, shown: list[list], narrowed: bool) -> dict:
    """The rows of a data structure answer as data: one entry per field with its range (from / to / size in the unit of the
    first column), name, abbreviation and the other columns (O/M ...), its bit rows under it. Descriptions only when the
    answer was narrowed to a field or an offset: for a whole structure they would double the answer."""
    cols = t.get("header") or []
    unit = (cols[0] if cols else "").lower()
    fields: list[dict] = []
    cur: dict | None = None
    for r in shown:
        name, abbr, _ = _split_field(_desc(r))
        if is_sub(r):
            e = {"bits": _sub_bits(r), "name": name or _desc(r)[:120], "abbreviation": abbr}
            if narrowed:
                e["description"] = _desc(r)
            if cur is not None:
                cur.setdefault("bits", []).append(e)
            continue
        cur = {"range": clean(r[0]), "name": name or _desc(r)[:120], "abbreviation": abbr}
        rg = parse_byte_range(clean(r[0])) if r else None
        if rg:
            cur.update({"from": min(rg), "to": max(rg), "size": max(rg) - min(rg) + 1})
        other = {cols[i]: clean(r[i]) for i in range(1, min(len(r), len(cols)) - 1) if cols[i] and clean(r[i])}
        if other:
            cur["columns"] = other
        if narrowed:
            cur["description"] = _desc(r)
        fields.append(cur)
    sec = ir["sections"].get(t.get("section") or "")
    return {"spec": d["family"], "version": d["version"], "figure": fig_no(t["label"]), "caption": clean(t.get("caption")),
            "section": sec["number"] if sec else None, "uri": uri(d, "fig:" + t["label"]), "unit": unit, "columns": cols,
            "rows_total": len(t.get("rows") or []), "narrowed": narrowed, "fields": fields}


class SpecLibrary:
    """Parsed documents of one workspace database, with the loaded models cached (a parse run never changes once it is done)."""

    def __init__(self, db_file: str | None = None, conn_factory: Callable[[], sqlite3.Connection] | None = None, cache_runs: int = 12):
        self.db_file = str(db_file) if db_file else None
        self._conn_factory = conn_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._irs: OrderedDict[tuple, dict] = OrderedDict()
        self._cache_runs = cache_runs
        self._index_locks: dict[int, threading.Lock] = {}
        self._compare_lock = threading.Lock()
        self.compare_wait_s = 60.0
        self.library_search = True   # admin switch: search_spec also searches converted-only documents (they are indexed either way)
        self.lib_index_wait = 5.0    # seconds a search waits for converted documents not indexed yet (indexing goes on behind it)
        self._lib_indexer: threading.Thread | None = None
        self._raw_only: tuple[tuple, list[dict]] = ((), [])
        self._md: OrderedDict[str, dict] = OrderedDict()     # Markdown files read_raw opened lately: text, lines, outline

    # -- connections (per thread: tools run on a thread pool)
    def conn(self) -> sqlite3.Connection:
        if self._conn_factory:
            return self._conn_factory()
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = connect(self.db_file)
        return c

    def db(self) -> str:
        return self.db_file or jobs.db_file_of(self.conn())

    # -- documents
    def documents(self) -> list[dict]:
        out = []
        for d in rows(self.conn(), """
                SELECT d.id AS doc_id, d.family, d.version, d.title, d.filename, d.pages, d.sha256, d.format,
                       r.id AS run_id, r.created AS run_created, r.parser_version, r.active
                FROM spec_document d
                JOIN parse_run r ON r.id = (SELECT id FROM parse_run WHERE document_id=d.id AND status='done' ORDER BY active DESC, id DESC LIMIT 1)"""):
            out.append(d)
        out.sort(key=lambda d: (d["family"], version_key(d["version"]), d["doc_id"]))
        return out

    def families(self) -> dict[str, list[dict]]:
        fam: dict[str, list[dict]] = {}
        for d in self.documents():
            fam.setdefault(d["family"], []).append(d)
        return fam

    def resolve_family(self, spec: str, fams: dict[str, list[dict]]) -> str:
        s = clean(spec).lower()
        if s in fams:
            return s
        s2 = re.sub(r"[_\s]+", " ", s.replace("-", " ")).strip()
        s2 = re.sub(r"\b(specification|spec|revision|rev|version)\b", " ", s2)
        s2 = _VERSION_IN.sub(" ", s2)
        s2 = re.sub(r"\s+", " ", s2).strip()
        for cand in (s2, s2.replace("nvm express", "nvme").strip()):
            if cand.replace(" ", "-") in fams:
                return cand.replace(" ", "-")
            if FAMILY_ALIASES.get(cand) in fams:
                return FAMILY_ALIASES[cand]
        m = _ALIAS_RE.search(s)
        if m and SPEC_ALIASES[m.group(0).lower()] in fams:
            return SPEC_ALIASES[m.group(0).lower()]
        # the words of the request against each family's id, name, document titles and file names
        want = _words(s2) - {"nvme", "nvm", "express", "specification"} or _words(s2)
        scored = []
        for f, docs in fams.items():
            words = _words(f.replace("-", " ")) | _words(FAMILY_NAMES.get(f, "")) | set().union(*(_words(d["title"]) | _words(d["filename"]) for d in docs))
            hit = len(want & words)
            if hit:
                scored.append((hit, f))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        options = ", ".join(sorted(fams))
        if scored:
            raise QueryError(f"'{spec}' matches several specifications ({', '.join(f for _, f in scored[:6])}); name one of: {options}")
        raise NotFound(f"no parsed specification matches '{spec}'. Parsed: {options or 'none yet'}")

    def resolve_doc(self, spec: str | None, version: str | None = None, *, default_family: str | None = None) -> tuple[dict, str]:
        """(document, note): note says which version was used when it was not asked for."""
        fams = self.families()
        if not fams:
            raise NotFound("no specification has been parsed on this server yet (upload and parse one on the Specs page)")
        spec = (spec or "").strip()
        if "@" in spec and not version:
            spec, version = spec.split("@", 1)
        if spec and not version and (m := _VERSION_IN.search(spec)):
            version = m.group(1)
        if spec:
            fam = self.resolve_family(spec, fams)
        elif default_family:
            fam = default_family
        elif version:
            fs = [f for f, ds in fams.items() if any(_norm_version(d["version"]) == _norm_version(version) for d in ds)]
            if len(fs) != 1:
                raise QueryError(f"name the specification for version {version} ({', '.join(fs) or 'no parsed document has it'})")
            fam = fs[0]
        elif len(fams) == 1:
            fam = next(iter(fams))
        else:
            raise QueryError(f"name the specification (one of: {', '.join(sorted(fams))})")
        return self.pick_version(fams[fam], version)

    @staticmethod
    def pick_version(docs: list[dict], version: str | None) -> tuple[dict, str]:
        v = _norm_version(version or "")
        if not v or v in ("latest", "newest", "current", "last"):
            d = docs[-1]
            note = "" if version else f"_Version not given: using the latest parsed version, {spec_name(d)}._"
            return d, note
        if v in ("previous", "prior", "prev", "older"):
            if len(docs) < 2:
                raise NotFound(f"only one version of {docs[0]['family']} is parsed ({docs[0]['version']})")
            return docs[-2], ""
        for d in docs:
            if _norm_version(d["version"]) == v:
                return d, ""
        raise NotFound(f"{docs[0]['family']} version {version} is not parsed. Parsed versions: {', '.join(d['version'] for d in docs)}")

    def docs_for(self, spec: str | None, version: str | None) -> tuple[list[dict], str]:
        """The documents a lookup without a named spec goes through: the latest version of every family (or every family that has
        the asked version); with a spec, just that one."""
        if spec:
            d, note = self.resolve_doc(spec, version)
            return [d], note
        fams = self.families()
        if not fams:
            raise NotFound("no specification has been parsed on this server yet")
        if version and _norm_version(version) not in ("latest", "all"):
            out = [d for ds in fams.values() for d in ds if _norm_version(d["version"]) == _norm_version(version)]
            if not out:
                raise NotFound(f"no parsed specification has version {version}")
            return out, ""
        if version and _norm_version(version) == "all":
            return [d for ds in fams.values() for d in ds], ""
        return [ds[-1] for ds in fams.values()], ""

    # -- loaded models
    def ir(self, d: dict) -> dict:
        key = (d["run_id"], d["run_created"])
        with self._lock:
            if key in self._irs:
                self._irs.move_to_end(key)
                return self._irs[key]
        ir = store.load_ir(self.conn(), d["run_id"])
        ir["_version"] = _Version(ir)
        ir["_notes"] = self._notes(d["run_id"])
        ir["_children"] = {}
        for n in ir["section_order"]:
            p = ir["sections"][n]["parent"]
            ir["_children"].setdefault(p, []).append(n)
        V = ir["_version"]
        for k, c in V.cmds.items():
            if not c.get("section"):
                want = norm_title(c["name"])
                n = next((n for n in ir["section_order"] if norm_title(ir["sections"][n]["title"]) == want and n not in V.cmd_section), None)
                if n:
                    c["section"] = n
                    V.cmd_section[n] = k
        ir["raw_tables"] = ir["tables"]
        ir["tables"], ir["_alias"] = logical_tables(ir["raw_tables"])
        ir["_tables_in"] = {}
        for lab, t in ir["tables"].items():
            ir["_tables_in"].setdefault(t.get("section"), []).append(lab)
        with self._lock:
            self._irs[key] = ir
            while len(self._irs) > self._cache_runs:
                self._irs.popitem(last=False)
        return ir

    @staticmethod
    def fig(ir: dict, label: str) -> dict | None:
        """The table a printed figure number names. The PDF parser can give one number to several tables ('336' a caption
        fragment, '336#3' the real 'Identify – CNS Values'), and a table continued over pages is several pieces: prefer the
        captioned table with rows."""
        cands = []
        for lab in [label] + [f"{label}#{i}" for i in range(2, 30)]:
            lead = ir["_alias"].get(lab)
            if lead and ir["tables"][lead] not in cands:
                cands.append(ir["tables"][lead])
        if not cands:
            return None
        return max(cands, key=lambda t: (bool(t.get("rows")), not _sentence_like(t.get("caption") or ""), len(t.get("rows") or [])))

    def _notes(self, run_id: int) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows(self.conn(), "SELECT node_key, message FROM parse_issue WHERE run_id=? AND severity IN ('warning','error') "
                                   "AND state='open' AND node_key IS NOT NULL", (run_id,)):
            k = r["node_key"].rsplit("/", 1)[-1] if "/" in r["node_key"] else r["node_key"]
            out.setdefault(k, []).append(r["message"])
        return out

    _ROUTINE_NOTE = re.compile(r"below threshold|^Figure/Table \S+ appears more than once", re.I)   # not "Section number ... appears more than once"

    def notes_for(self, ir: dict, node: str, table: dict | None = None) -> list[str]:
        """Parser notes worth an agent's attention. Left out: table-quality scores and figure numbers seen twice (pieces of one
        table, which logical_tables joins), found on most tables of a PDF; a table extracted really badly gets one line."""
        msgs = [m for m in dict.fromkeys(ir["_notes"].get(node, [])) if not self._ROUTINE_NOTE.search(m)][:3]
        if table is not None and (table.get("quality") or 1.0) < LOW_QUALITY:
            msgs.append(f"table extraction quality is low ({table.get('quality'):.2f}): check the columns against the PDF")
        return [f"> Parser note: {m}" for m in msgs] + ([""] if msgs else [])

    # ------------------------------------------------------------------------- list_specs
    def list_specs(self) -> str:
        fams = self.families()
        lines = ["# Parsed specifications", ""]
        if not fams:
            lines.append("None yet: upload and parse specifications on the SpecForge Specs page.")
        for f, docs in sorted(fams.items()):
            lines.append(f"## {FAMILY_NAMES.get(f, f)} (`{f}`)")
            lines.append("| Version | Title | Pages | Parse warnings | Resource |")
            lines.append("|---|---|---|---|---|")
            for d in reversed(docs):
                n = self.conn().execute("SELECT COUNT(*) FROM parse_issue WHERE run_id=? AND severity IN ('warning','error') AND state='open'",
                                        (d["run_id"],)).fetchone()[0]
                tag = " (latest)" if d is docs[-1] else ""
                lines.append(f"| {d['version']}{tag} | {_cell(d['title'] or d['filename'])} | {d['pages'] or ''} | {n} | `{uri(d, '')}` |")
            lines.append("")
        raw = self.raw_only()
        if raw:
            lines += ["## Converted to Markdown only (not parsed: use read_raw)", ""]
            lines += [f"- {r['filename']}" for r in raw]
            lines.append("")
        lines.append("Name a specification by its id (`nvme-base`), a short name (`base`, `mi`, `nvm`, `zns`) or its title; "
                     "a version as printed (`2.4`), `latest` (the default) or `previous`.")
        return "\n".join(lines)

    # ------------------------------------------------------------------------- search index
    def index_state(self) -> dict[int, dict]:
        c = self.conn()
        return {r["run_id"]: r for r in rows(c, "SELECT * FROM mcp_index")}

    def ensure_index(self, d: dict, force: bool = False) -> bool:
        """Index one parse run for search (once; again when the run or INDEX_VERSION changed). True when it was (re)built."""
        c = self.conn()
        with self._lock:
            lock = self._index_locks.setdefault(d["run_id"], threading.Lock())
        with lock:
            have = row(c, "SELECT run_created, version FROM mcp_index WHERE run_id=?", (d["run_id"],))
            if have and have["run_created"] == d["run_created"] and have["version"] == INDEX_VERSION and not force:
                return False
            t0 = time.monotonic()
            ir = self.ir(d)
            entries = []
            for n in ir["section_order"]:
                s = ir["sections"][n]
                entries.append((n, s["title"], s["text"], d["run_id"], s["key"], "section", s["page_from"]))
            for lab, t in ir["tables"].items():
                header = " | ".join(clean(h) for h in t.get("header") or [])
                body = header + "\n" + "\n".join(" | ".join(clean(x) for x in r) for r in t.get("rows") or [])
                entries.append((lab, t.get("caption") or "", body, d["run_id"], t["key"], "table", t.get("page_from")))
                for i, r in enumerate(t.get("rows") or []):
                    text = " | ".join(clean(x) for x in r)
                    if text.strip(" |"):
                        entries.append((lab, t.get("caption") or "", text, d["run_id"], f"{t['key']}#r{i}", "row", t.get("page_from")))
            c.execute("DELETE FROM mcp_fts WHERE run_id=?", (d["run_id"],))
            c.executemany("INSERT INTO mcp_fts(number,title,body,run_id,key,kind,page) VALUES(?,?,?,?,?,?,?)", entries)
            c.execute("INSERT OR REPLACE INTO mcp_index(run_id, run_created, version, entries, seconds, built) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                      (d["run_id"], d["run_created"], INDEX_VERSION, len(entries), round(time.monotonic() - t0, 2)))
            c.commit()
            return True

    def prune_index(self) -> int:
        """Drop index entries of parse runs that no longer exist or are no longer the one served."""
        c = self.conn()
        keep = {d["run_id"] for d in self.documents()}
        stale = [r["run_id"] for r in rows(c, "SELECT run_id FROM mcp_index") if r["run_id"] not in keep]
        for rid in stale:
            c.execute("DELETE FROM mcp_fts WHERE run_id=?", (rid,))
            c.execute("DELETE FROM mcp_index WHERE run_id=?", (rid,))
        c.commit()
        return len(stale)

    def index_all(self, progress: Callable[[str, int, int], None] | None = None, force: bool = False) -> dict:
        docs, libs = self.documents(), self.raw_only()
        built = 0
        for i, d in enumerate(docs):
            if progress:
                progress(f"indexing {d['family']} {d['version']}", i, len(docs) + len(libs))
            built += self.ensure_index(d, force)
        for i, r in enumerate(libs):
            if progress:
                progress(f"indexing {r['filename']}", len(docs) + i, len(docs) + len(libs))
            built += self.ensure_lib_index(r, force)
        pruned = self.prune_index() + self.prune_lib_index(libs)
        return {"documents": len(docs) + len(libs), "built": built, "pruned": pruned}

    # -- converted documents that are not parsed specs (To Markdown library): indexed from their Markdown, keyed lib:<folder>
    def lib_index_state(self) -> dict[str, dict]:
        return {r["dir"]: r for r in rows(self.conn(), "SELECT * FROM mcp_lib_index")}

    def ensure_lib_index(self, r: dict, force: bool = False) -> bool:
        c, rid = self.conn(), f"lib:{r['dir']}"
        with self._lock:
            lock = self._index_locks.setdefault(rid, threading.Lock())
        with lock:
            have = row(c, "SELECT version FROM mcp_lib_index WHERE dir=?", (r["dir"],))
            if have and have["version"] == LIB_INDEX_VERSION and not force:
                return False
            f = self._markdown_file(dir_=r["dir"])
            t0 = time.monotonic()
            entries = library_entries(f.read_text(encoding="utf-8", errors="replace"), rid, r["filename"]) if f else []
            c.execute("DELETE FROM mcp_fts WHERE run_id=?", (rid,))
            c.executemany("INSERT INTO mcp_fts(number,title,body,run_id,key,kind,page) VALUES(?,?,?,?,?,?,?)", entries)
            c.execute("INSERT OR REPLACE INTO mcp_lib_index(dir, filename, version, entries, seconds, built) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                      (r["dir"], r["filename"], LIB_INDEX_VERSION, len(entries), round(time.monotonic() - t0, 2)))
            c.commit()
            return True

    def index_library(self, libs: list[dict], wait: float) -> list[dict]:
        """Index the converted documents that are not indexed yet on a background thread, waiting up to `wait` seconds for it.
        Returns the ones still not indexed (a search leaves them out and says so)."""
        def todo() -> list[dict]:
            state = self.lib_index_state()
            return [r for r in libs if (state.get(r["dir"]) or {}).get("version") != LIB_INDEX_VERSION]

        missing = todo()
        if not missing:
            return []

        def work() -> None:
            for r in missing:
                try:
                    self.ensure_lib_index(r)
                except Exception:  # noqa: BLE001  that file stays unsearched (read_raw still reads it); the next search tries again
                    pass
        with self._lock:
            t = self._lib_indexer
            if t is None or not t.is_alive():
                t = self._lib_indexer = threading.Thread(target=work, name="sf-mcp-lib-index", daemon=True)
                t.start()
        if wait > 0:
            t.join(wait)
        return todo()

    def prune_lib_index(self, libs: list[dict] | None = None) -> int:
        """Drop the entries of converted documents that were deleted from the library or have since been parsed as a spec."""
        c = self.conn()
        keep = {r["dir"] for r in (self.raw_only() if libs is None else libs)}
        stale = [d for d in self.lib_index_state() if d not in keep]
        for d in stale:
            c.execute("DELETE FROM mcp_fts WHERE run_id=?", (f"lib:{d}",))
            c.execute("DELETE FROM mcp_lib_index WHERE dir=?", (d,))
        if stale:
            c.commit()
        return len(stale)

    @staticmethod
    def fts_query(q: str) -> tuple[list[str], list[str]]:
        """(groups, words): FTS5 groups, one per query word (the word OR its spelled-out abbreviation)."""
        q = _SPLIT.sub(r"\1 \2", q)
        groups, words = [], []
        for w in re.findall(r"[A-Za-z0-9]+", q):
            lw = w.lower()
            if lw in _STOP:
                continue
            words.append(lw)
            alts = [f'"{lw}"']
            if lw in ABBREVIATIONS:
                alts.append('"' + ABBREVIATIONS[lw] + '"')
            groups.append("(" + " OR ".join(alts) + ")" if len(alts) > 1 else alts[0])
        return groups, words

    # ------------------------------------------------------------------------- search
    def search(self, query: str, spec: str | None = None, version: str | None = None, kinds: str | None = None, limit: int = MAX_HITS) -> tuple[str, int]:
        """(markdown, hit count)."""
        if not (query or "").strip():
            raise QueryError("give a query, e.g. 'Identify CNS values' or 'Get Log Page NUMDL'")
        all_libs = self.raw_only()
        libs = [r] if spec and (r := self._lib_doc(spec, all_libs)) else []
        unversioned = _norm_version(version or "latest") in ("latest", "all")
        if libs and not self.library_search:
            raise QueryError(f"searching converted documents is turned off on this server: read {libs[0]['filename']} with "
                             f"read_raw(spec=\"{libs[0]['filename']}\", around=\"<phrase>\") or section=\"<number>\"")
        if not spec and unversioned and self.library_search:
            libs = all_libs                         # a converted document has no version: it is in every unversioned search
        docs, note = [], ""
        if not (spec and libs):
            try:
                docs, note = self.docs_for(spec, version)
            except NotFound:
                if spec or not libs:
                    raise
        for d in docs:
            self.ensure_index(d)
        self.prune_lib_index(all_libs)
        # every converted document is kept indexed (also while the switch is off); a search waits a little for the ones it needs
        pending = self.index_library(all_libs, self.lib_index_wait if libs else 0)
        if libs and pending:
            wait_for = {r["dir"] for r in pending}
            pending = [r for r in libs if r["dir"] in wait_for]
            libs = [r for r in libs if r["dir"] not in wait_for]
            if spec and not libs:
                raise NotFound(f"{pending[0]['filename']} is still being indexed: search again in a few seconds, or read it now with "
                               f"read_raw(spec=\"{pending[0]['filename']}\", around=\"<phrase>\")")
        else:
            pending = []
        by_run: dict[Any, dict] = {d["run_id"]: d for d in docs} | {f"lib:{r['dir']}": r for r in libs}
        groups, words = self.fts_query(query)
        want_kinds = {k.strip().rstrip("s") for k in (kinds or "").split(",") if k.strip()} or {"section", "table", "row", "doc"}
        want_kinds = {"row" if k in ("table row", "row") else k for k in want_kinds}
        if "table" in want_kinds:
            want_kinds.add("row")
        limit = max(1, min(int(limit or MAX_HITS), 100))
        hits: list[dict] = []
        direct = self._direct_hits(query, docs)
        if groups:
            ph = ",".join("?" * len(by_run))
            for joiner in (" AND ", " OR "):
                fts = joiner.join(groups)
                try:
                    found = rows(self.conn(), f"""
                        SELECT key, kind, number, title, run_id, page, bm25(mcp_fts, 4.0, 8.0, 1.0) AS score,
                               snippet(mcp_fts, 2, '**', '**', ' … ', 24) AS snip
                        FROM mcp_fts WHERE mcp_fts MATCH ? AND run_id IN ({ph}) ORDER BY score LIMIT 400""", [fts, *by_run])
                except sqlite3.OperationalError as e:
                    raise QueryError(f"could not search for '{query}': {e}") from None
                found = [h for h in found if h["kind"] in want_kinds]
                if found:
                    hits = found
                    break
        # rank: whole-word title matches first, then BM25; at most 3 rows of one table
        wset = set(words)
        for h in hits:
            tw = _words(h["title"]) | _words(h["number"])
            h["rank"] = h["score"] - 3.0 * len(wset & tw) - (4.0 if wset and wset <= tw else 0.0) + (0.5 if h["kind"] == "row" else 0.0)
        hits.sort(key=lambda h: h["rank"])
        seen_keys = {x["key"] for x in direct}
        out, per_table = list(direct), {}
        for h in hits:
            if h["key"] in seen_keys:
                continue
            if h["kind"] == "row":
                tk = h["key"].split("#", 1)[0]
                if tk in seen_keys:
                    continue
                per_table[tk] = per_table.get(tk, 0) + 1
                if per_table[tk] > 3:
                    continue
            seen_keys.add(h["key"])
            out.append(h)
            if len(out) >= limit:
                break
        lines = [f"# Search: {query}", ""]
        if note:
            lines += [note, ""]
        scope = ", ".join([f"{d['family']} {d['version']}" for d in docs] + ([libs[0]["filename"]] if len(libs) == 1 else
                                                                              [f"{len(libs)} converted documents"] if libs else []))
        if not spec:
            what = ("the latest version of each specification" if _norm_version(version or "latest") == "latest" else
                    "every version of each specification" if unversioned else f"the specifications with version {version}")
            if not self.library_search:
                what += "; converted documents are not searched on this server: use read_raw"
            elif libs:
                what += " and every converted document"
            elif all_libs and not unversioned:
                what += "; converted documents have no version: search without one to include them"
            scope += f" ({what}; pass spec / version to narrow or widen)"
        lines.append(f"Searched: {scope}")
        if pending:
            lines.append("Still being indexed, not searched yet (search again in a few seconds, or use read_raw): "
                         + ", ".join(r["filename"] for r in pending))
        lines.append("")
        if not out:
            lines.append("No matches. Try other words, a figure or section number, or read_raw with a phrase.")
            return "\n".join(lines), 0
        for h in out:
            d = by_run[h["run_id"]]
            if "dir" in d:
                lines.append(self._lib_hit(h, d))
                continue
            where = f"{d['family']}@{d['version']}"
            if h["kind"] == "section":
                head = f"§{h['number']} {h['title']}"
                node = f"sec:{h['number']}"
            elif h["kind"] == "table":
                head = f"Figure {fig_no(h['number'])}: {_name_of(h['title'])}"
                node = f"fig:{h['number']}"
            else:
                head = f"Figure {fig_no(h['number'])} row ({_name_of(h['title'])})"
                node = f"fig:{h['number']}"
            p = f", p. {h['page']}" if h.get("page") else ""
            snip = clean(h.get("snip") or "")
            lines.append(f"- **{head}** — {where}{p} · `{uri(d, node)}`" + (f"\n  {snip}" if snip else ""))
        lines += ["", "Open a hit with get_section (section number) or get_table (figure number); a hit in a converted document "
                      "(not parsed: no command or structure lookups) with the read_raw call shown."]
        return "\n".join(lines), len(out)

    @staticmethod
    def _lib_hit(h: dict, r: dict) -> str:
        """A search hit in a converted document: where it is (section, lines) and the read_raw call that opens just that part."""
        name = r["filename"].replace('"', "'")
        k = parse_lib_key(h["key"])
        if h["kind"] == "doc" or k is None:
            return f"- **{h['title']}** — {r['filename']} (converted, not parsed) · `read_raw(spec=\"{name}\")`"
        sec = k["sec"]
        if h["kind"] == "section":
            head = f"§{sec} {h['title']}" if sec else h["title"]
        else:
            head = (f"{h['number']}: {h['title']}" if h["number"] else f"Table in {h['title']}") + (" (row)" if h["kind"] == "row" else "")
        span = str(k["row"]) if k["row"] is not None else f"{k['lo']}-{k['hi']}"
        where = f"{r['filename']} (converted, not parsed)" + (f", §{sec}" if sec and h["kind"] != "section" else "")
        at = f", line {k['row']}" if k["row"] is not None else f", lines {k['lo']}–{k['hi']}"
        snip = clean(h.get("snip") or "")
        return f"- **{head}** — {where}{at} · `read_raw(spec=\"{name}\", lines=\"{span}\")`" + (f"\n  {snip}" if snip else "")

    def _direct_hits(self, query: str, docs: list[dict]) -> list[dict]:
        """A query that is a section or figure number ('5.2.1', 'Figure 101') finds that node first."""
        q = query.strip()
        m_fig = re.fullmatch(r"(?:fig(?:ure)?|table)\.?\s*(\d+[A-Za-z]?)", q, re.I)
        m_sec = re.fullmatch(r"(?:§|sec(?:tion)?\.?\s*)?(\d+(?:\.\d+)+|\d+)", q, re.I)
        if not (m_fig or m_sec):
            return []
        out = []
        for d in docs:
            ir = self.ir(d)
            if m_fig and (t := self.fig(ir, m_fig.group(1))):
                out.append({"key": t["key"], "kind": "table", "number": t["label"], "title": t.get("caption") or "", "run_id": d["run_id"],
                            "page": t.get("page_from"), "snip": ""})
            elif m_sec and not m_fig and (s := ir["sections"].get(m_sec.group(1))):
                out.append({"key": s["key"], "kind": "section", "number": s["number"], "title": s["title"], "run_id": d["run_id"],
                            "page": s["page_from"], "snip": clean(s["text"])[:200]})
        return out

    # ------------------------------------------------------------------------- sections and tables
    def find_section(self, ir: dict, ref: str) -> str:
        r = clean(ref)
        m = re.fullmatch(r"(?:§|sec(?:tion)?\.?\s*)?(\d+(?:\.\d+)*)\.?", r, re.I)
        if m:
            if m.group(1) in ir["sections"]:
                return m.group(1)
            raise NotFound(f"{ir['family']} {ir['version']} has no section {m.group(1)}")
        best = sorted(((_similar(r, s["title"]), n) for n, s in ir["sections"].items()), key=lambda x: -x[0])
        if best and best[0][0] >= 0.5:
            return best[0][1]
        raise NotFound(f"no section of {ir['family']} {ir['version']} is titled like '{ref}' (use search to find it)")

    def render_table(self, d: dict, ir: dict, t: dict, level: int = 2, rows_filter: Callable[[list], bool] | None = None,
                     shown: list[list] | None = None, extra: list[str] = ()) -> str:
        data = t.get("rows") or []
        if shown is None:
            shown = [r for r in data if rows_filter(r)] if rows_filter else data
        sec = ir["sections"].get(t.get("section") or "")
        cite = [f"Figure {fig_no(t['label'])}", spec_name(d)]
        if sec:
            cite.append(f"§{sec['number']} {sec['title']}")
        if pages(t.get("page_from"), t.get("page_to")):
            cite.append(pages(t.get("page_from"), t.get("page_to")))
        lines = [f"{'#' * level} {clean(t.get('caption')) or 'Figure ' + fig_no(t['label'])}", "",
                 " · ".join(cite) + f" · `{uri(d, 'fig:' + t['label'])}`", ""]
        lines += self.notes_for(ir, f"fig:{t['label']}", t)
        if shown is not data:
            lines.append(f"_{len(shown)} of {len(data)} rows match._")
        lines += [md_table(t.get("header") or [], shown), ""] + list(extra)
        return "\n".join(lines)

    def ref_table(self, d: dict, ir: dict, t: dict, level: int) -> str:
        """A table an answer refers to: printed when it is small (value tables), named with its link when it is a whole structure."""
        if len(t.get("rows") or []) <= REF_ROWS:
            return self.render_table(d, ir, t, level=level)
        return (f"{'#' * level} {clean(t.get('caption')) or 'Figure ' + fig_no(t['label'])}\n\nFigure {fig_no(t['label'])} · {spec_name(d)} · "
                f"{len(t['rows'])} rows, not printed here: open it with get_table / get_structure (a field or offset narrows it) · "
                f"`{uri(d, 'fig:' + t['label'])}`\n")

    def section(self, spec: str | None, version: str | None, ref: str, depth: int = 0, tables: bool = True, page: int = 1) -> str:
        d, note = self.resolve_doc(spec, version)
        ir = self.ir(d)
        n = self.find_section(ir, ref)
        parts = [note] if note else []
        self._render_section(d, ir, n, max(0, min(int(depth or 0), 6)), tables, parts, top=True)
        return paginate("\n\n".join(p for p in parts if p), page)

    def _render_section(self, d: dict, ir: dict, n: str, depth: int, tables: bool, parts: list[str], top: bool = False, level: int = 1) -> None:
        s = ir["sections"][n]
        head = f"{'#' * min(level, 6)} {n} {s['title']}"
        cite = f"{spec_name(d)} · §{n}" + (f" · {pages(s['page_from'], s['page_to'])}" if s["page_from"] else "") + f" · `{uri(d, 'sec:' + n)}`"
        parent = ir["sections"].get(s["parent"] or "")
        if top and parent:
            cite += f" · in §{parent['number']} {parent['title']}"
        parts.append(head + "\n\n" + cite + ("\n\n" + "\n".join(self.notes_for(ir, f"sec:{n}")) if self.notes_for(ir, f"sec:{n}") else ""))
        if s["text"].strip():
            parts.append(s["text"].strip())
        if tables:
            for lab in ir["_tables_in"].get(n, []):
                parts.append(self.render_table(d, ir, ir["tables"][lab], level=min(level + 1, 6)))
        kids = ir["_children"].get(n, [])
        if kids and depth > 0:
            for k in kids:
                self._render_section(d, ir, k, depth - 1, tables, parts, level=level + 1)
        elif kids:
            parts.append("Sub-sections (pass depth=1 or more to include them):\n" +
                         "\n".join(f"- {k} {ir['sections'][k]['title']}" for k in kids))

    def find_table(self, ir: dict, ref: str) -> dict:
        r = clean(ref)
        m = re.fullmatch(r"(?:fig(?:ure)?|table)?\.?\s*(\d+[A-Za-z]?(?:#\d+)?|p\d+t\d+)", r, re.I)
        if m:
            t = self.fig(ir, m.group(1))
            if t:
                return t
            raise NotFound(f"{ir['family']} {ir['version']} has no Figure {m.group(1)}")
        best = sorted(((name_score(r, _name_of(t.get("caption"))), lab) for lab, t in ir["tables"].items()
                       if t.get("captioned", True) and t.get("rows")), key=lambda x: -x[0])
        if best and best[0][0] >= 0.75:
            return ir["tables"][best[0][1]]
        raise NotFound(f"no figure of {ir['family']} {ir['version']} is captioned like '{ref}' (use search to find it)")

    def table(self, spec: str | None, version: str | None, ref: str, page: int = 1) -> str:
        d, note = self.resolve_doc(spec, version)
        ir = self.ir(d)
        t = self.find_table(ir, ref)
        return paginate((note + "\n\n" if note else "") + self.render_table(d, ir, t, level=1), page)

    # ------------------------------------------------------------------------- commands
    def _find_command(self, name: str, spec: str | None, version: str | None) -> tuple[dict, dict, str, str]:
        """(document, ir, command key, note). An opcode ('06h', 'admin 06h') or a name ('Identify', 'Get Log Page command')."""
        docs, note = self.docs_for(spec, version)
        q = clean(name)
        m_op = re.fullmatch(r"(?:(admin|i/?o|io|nvm|mi)\s+)?(?:opcode\s+)?(?:0x)?([0-9a-f]{1,2})h?", q, re.I)
        found: list[tuple[float, dict, str]] = []
        for d in docs:
            ir = self.ir(d)
            V = ir["_version"]
            if m_op:
                want = f"{int(m_op.group(2), 16):02X}h"
                route = (m_op.group(1) or "").lower()
                for (rl, opc), k in V.by_opcode.items():
                    if opc == want and (not route or rl.lower().startswith(route.replace("io", "i/o").replace("nvm", "i/o")[:3])):
                        found.append((2.0, d, k))
                continue
            k = _ck(q)
            if k in V.cmds:
                found.append((3.0, d, k))
                continue
            for ck, c in V.cmds.items():
                sc = _similar(q, c["name"])
                if sc >= 0.6:
                    found.append((sc, d, ck))
        if not found:
            where = ", ".join(f"{d['family']} {d['version']}" for d in docs)
            raise NotFound(f"no command named '{name}' in {where}. Try search, or list_commands to see the commands of a specification")
        found.sort(key=lambda x: (-x[0], 0 if x[1]["family"] == "nvme-base" else 1))
        if m_op and len({(x[1]["run_id"], x[2]) for x in found}) > 1:
            opts = "; ".join(f"{x[1]['family']}: {self.ir(x[1])['_version'].cmds[x[2]]['name']} ({self.ir(x[1])['_version'].cmds[x[2]]['opcode']})"
                             for x in found[:8])
            raise QueryError(f"opcode {name} is used by several commands ({opts}); name the command or the queue (e.g. 'admin 06h')")
        _, d, k = found[0]
        return d, self.ir(d), k, note

    def command_tables(self, ir: dict, k: str) -> tuple[list[tuple[str, dict]], list[dict]]:
        """([(dword label, table)], [other tables of the command]) in document order."""
        V = ir["_version"]
        dwords, other = [], []
        for t in ir["tables"].values():
            if V.command_of_table(t) != k:
                continue
            cap = _name_of(t.get("caption"))
            m = _CAP_DWORDS.search(cap) if re.search(r"dword|NMD", cap, re.I) else None
            if m and (m.group(1) or m.group(2)):
                lab = f"NMD{m.group(1)}" if m.group(1) else (f"CDW{m.group(2)}" + (f"-{m.group(3)}" if m.group(3) else ""))
                dwords.append((lab, t))
            else:
                other.append(t)
        return dwords, other

    def list_commands(self, spec: str | None, version: str | None) -> str:
        d, note = self.resolve_doc(spec, version)
        ir = self.ir(d)
        V = ir["_version"]
        lines = [f"# Commands of {spec_name(d)}", ""] + ([note, ""] if note else [])
        lines += ["| Command | Opcode | Section | Command Dword tables |", "|---|---|---|---|"]
        for k, c in sorted(V.cmds.items(), key=lambda kv: (kv[1]["route"] or "~", kv[1]["opcode"] or "", kv[1]["name"].lower())):
            dw, _ = self.command_tables(ir, k)
            sec = c.get("section") or ""
            lines.append(f"| {_cell(c['name'])} | {c['opcode'] or ''} | {('§' + sec) if sec else ''} | {', '.join(x for x, _ in dw)} |")
        return "\n".join(lines)

    def command(self, name: str, spec: str | None = None, version: str | None = None, detail: str = "full", page: int = 1) -> str:
        d, ir, k, note = self._find_command(name, spec, version)
        V = ir["_version"]
        c = V.cmds[k]
        sec_n = c.get("section")
        lines = [f"# {c['name']} command — {spec_name(d)}", ""] + ([note, ""] if note else [])
        facts = []
        if c.get("opcode"):
            row_ = next((r for (rl, opc), r in V.opcode_rows.items() if V.by_opcode.get((rl, opc)) == k), None)
            facts.append(f"**Opcode:** {c['opcode']}" + (f" (Figure {row_['table']})" if row_ else ""))
            if row_:
                facts.append("**Opcode table row:** " + " · ".join(f"{_cell(h) or 'col'}: {_cell(v)}" for h, v in zip(row_["header"], row_["cells"]) if v))
        if sec_n and sec_n in ir["sections"]:
            s = ir["sections"][sec_n]
            facts.append(f"**Section:** §{sec_n} {s['title']} ({pages(s['page_from'], s['page_to'])}) · `{uri(d, 'sec:' + sec_n)}`")
        lines += facts + [""]
        dwords, other = self.command_tables(ir, k)
        if sec_n and sec_n in ir["sections"] and detail != "brief":
            lines += ["## Description", "", ir["sections"][sec_n]["text"].strip() or "_(no text in the section itself)_", ""]
            kids = ir["_children"].get(sec_n, [])
            if kids:
                lines += ["## Sub-sections", ""] + [f"- §{x} {ir['sections'][x]['title']}" for x in kids] + [""]
        if dwords:
            lines += ["## Command Dwords", ""]
            for lab, t in dwords:
                if detail == "brief":
                    lines.append(f"- {lab}: Figure {fig_no(t['label'])} {_name_of(t.get('caption'))} ({pages(t.get('page_from'), t.get('page_to'))})")
                else:
                    lines.append(self.render_table(d, ir, t, level=3))
            lines.append("")
        else:
            lines += ["_No Command Dword tables are linked to this command in the parsed model; search for its fields or read its section._", ""]
        if other:
            lines += ["## Other tables of this command", ""]
            for t in [t for t in other if t.get("rows") and t.get("captioned", True)]:
                lines.append(f"- Figure {fig_no(t['label'])}: {_name_of(t.get('caption'))} (§{t.get('section') or '?'}, {pages(t.get('page_from'), t.get('page_to'))})"
                             f" · `{uri(d, 'fig:' + t['label'])}`")
            lines.append("")
        elsewhere = self._mentions_elsewhere(c["name"], d)
        if elsewhere:
            lines += ["## Also specified in", ""] + elsewhere + [""]
        lines.append("Use get_command_field for one Command Dword or field, with the value tables it refers to.")
        return paginate("\n".join(lines), page)

    def _mentions_elsewhere(self, cmd_name: str, d: dict) -> list[str]:
        """Sections of the other specifications (latest versions) whose title names this command: command-set specific parts."""
        base = re.sub(r"\s+command$", "", cmd_name.strip(), flags=re.I)
        if len(base) < 4:
            return []
        # a one-word name ('Read', 'Identify') only counts when followed by 'command'; multi-word names are distinctive (as in catalog.py)
        pat = re.compile(rf"\b{re.escape(base)}\b" + (r"" if " " in base else r"\s+command\b"), re.I)
        out = []
        for f, docs in self.families().items():
            if f == d["family"]:
                continue
            o = docs[-1]
            ir = self.ir(o)
            for n in ir["section_order"]:
                s = ir["sections"][n]
                if pat.search(s["title"] or ""):
                    out.append(f"- {spec_name(o)} §{n} {s['title']} · `{uri(o, 'sec:' + n)}`")
                    if len(out) >= 12:
                        return out
        return out

    def command_field(self, command: str, dword: str | None = None, field: str | None = None, spec: str | None = None,
                      version: str | None = None, page: int = 1) -> str:
        d, ir, k, note = self._find_command(command, spec, version)
        c = ir["_version"].cmds[k]
        dwords, other = self.command_tables(ir, k)
        lines = [f"# {c['name']} command" + (f" — {self._dword_label(dword)}" if dword else "") + (f" — {field}" if field else "") +
                 f" — {spec_name(d)}", ""] + ([note, ""] if note else [])
        if not dwords:
            raise NotFound(f"no Command Dword tables are linked to {c['name']} in {spec_name(d)}; try get_command or search")
        chosen = dwords
        if dword:
            want = self._dword_label(dword)
            chosen = [(lab, t) for lab, t in dwords if self._covers(lab, want)]
            if not chosen and want.startswith("CDW"):        # "dword 0" of an NVMe-MI command is its NMD0
                chosen = [(lab, t) for lab, t in dwords if lab == "NMD" + want[3:]]
            if not chosen:
                raise NotFound(f"{c['name']} has no {want} table in {spec_name(d)}. Its Command Dword tables: {', '.join(x for x, _ in dwords)}")
        rows_filter = None
        if field:
            fq = clean(field).lower()

            def rows_filter(r: list, fq=fq) -> bool:
                desc = _desc(r)
                name, abbr, _ = _split_field(desc)
                return fq in (abbr.lower(), name.lower()) or bool(abbr and re.fullmatch(rf"{re.escape(fq)}", abbr, re.I)) or \
                    (len(fq) > 3 and fq in name.lower()) or clean(r[0]).lower() == fq
            chosen = [(lab, t) for lab, t in chosen if any(rows_filter(r) for r in t.get("rows") or [])]
            if not chosen:
                names = sorted({_split_field(_desc(r))[1] or _split_field(_desc(r))[0] for _, t in dwords for r in t.get("rows") or []} - {""})
                raise NotFound(f"no field '{field}' in the Command Dword tables of {c['name']} ({spec_name(d)}). Fields: {', '.join(names[:60])}")
        referenced: list[tuple[dict, dict, dict]] = []
        for lab, t in chosen:
            lines.append(self.render_table(d, ir, t, level=2, rows_filter=rows_filter))
            texts = [clean(" ".join(r)) for r in t.get("rows") or [] if not rows_filter or rows_filter(r)]
            for txt in texts:
                referenced += self._figure_refs(txt, d, ir, exclude={t["label"]})
        if field:        # what the command's own text says about the field
            sec_text = " ".join(ir["sections"][n]["text"] for n in self._subtree(ir, c.get("section")))
            abbr = clean(field)
            said = [s for s in re.split(r"(?<=[.;])\s+", sec_text) if re.search(rf"\b{re.escape(abbr)}\b", s, re.I)][:12]
            if said:
                lines += ["## What the command's sections say about it", ""] + [f"- {clean(s)}" for s in said] + [""]
        seen = set()
        refs_out = []
        for rd, rir, rt in referenced:
            key = (rd["run_id"], rt["label"])
            if key in seen or len(seen) >= 8:
                continue
            seen.add(key)
            refs_out.append(self.ref_table(rd, rir, rt, level=3))
        if refs_out:
            lines += ["## Referenced tables", ""] + refs_out
        value_tables = [t for t in other if t["label"] not in {x[2]["label"] for x in referenced} and t.get("rows") and t.get("captioned", True)]
        if value_tables and not field:
            lines += ["## Other tables of this command", ""] + [f"- Figure {fig_no(t['label'])}: {_name_of(t.get('caption'))}" for t in value_tables] + [""]
        return paginate("\n".join(lines), page)

    @staticmethod
    def _dword_label(dword: str) -> str:
        s = clean(str(dword))
        m = _DWORD.search(s) if not s.isdigit() else None
        if s.isdigit():
            return f"CDW{int(s)}"
        if m:
            if m.group(4) is not None:
                return f"NMD{m.group(4)}"
            if re.search(r"management", s, re.I):
                return f"NMD{next(g for g in m.groups()[:3] if g)}"
            return f"CDW{int(next(g for g in m.groups()[:3] if g))}"
        return s.upper()

    @staticmethod
    def _covers(lab: str, want: str) -> bool:
        if lab == want:
            return True
        m = re.fullmatch(r"CDW(\d+)-(\d+)", lab)
        w = re.fullmatch(r"CDW(\d+)", want)
        return bool(m and w and int(m.group(1)) <= int(w.group(1)) <= int(m.group(2)))

    @staticmethod
    def _subtree(ir: dict, n: str | None) -> list[str]:
        if not n or n not in ir["sections"]:
            return []
        out, stack = [], [n]
        while stack:
            x = stack.pop(0)
            out.append(x)
            stack += ir["_children"].get(x, [])
        return out

    def _figure_refs(self, text: str, d: dict, ir: dict, exclude: set[str] = frozenset()) -> list[tuple[dict, dict, dict]]:
        """Tables a piece of text refers to ('Refer to Figure 103', '... Figure 20 in the NVM Command Set Specification')."""
        out = []
        for r in extract_references(text, d["family"]):
            if r.kind != "figure" or r.label in exclude:
                continue
            if r.family is None:
                t = self.fig(ir, r.label)
                if t:
                    out.append((d, ir, t))
                continue
            docs = self.families().get(r.family)
            if docs:
                o = docs[-1]
                oir = self.ir(o)
                if (t := self.fig(oir, r.label)):
                    out.append((o, oir, t))
        return out

    # ------------------------------------------------------------------------- data structures
    def structure(self, name: str, spec: str | None = None, version: str | None = None, field: str | None = None,
                  offset: str | None = None, page: int = 1) -> str:
        docs, note = self.docs_for(spec, version)
        cands: list[tuple[float, dict, dict]] = []
        q = clean(name)
        m_fig = re.fullmatch(r"(?:fig(?:ure)?|table)\.?\s*(\d+[A-Za-z]?)", q, re.I)
        for d in docs:
            ir = self.ir(d)
            V = ir["_version"]
            if m_fig:
                if (t := self.fig(ir, m_fig.group(1))):
                    cands.append((9.0, d, t))
                continue
            for t in ir["tables"].values():
                if not t.get("rows"):
                    continue
                cap = _name_of(t.get("caption"))
                sc = name_score(q, cap)
                sec = ir["sections"].get(t.get("section") or "")
                if sec:
                    sc = max(sc, name_score(q, sec["title"]) - 0.1)
                if sc >= 0.75:
                    sc += (0.3 if V.is_struct_table(t) else 0.0) + (0.05 if d["family"] == "nvme-base" else 0.0)
                    cands.append((sc, d, t))
        if not cands:
            raise NotFound(f"no data structure, log page or descriptor named like '{name}' (use search to find its figure)")
        cands.sort(key=lambda x: -x[0])
        lines = [note] if note else []
        pick = None
        context: set[int] = set()            # field rows shown only as the parent of a bit row that matched
        if offset not in (None, ""):
            off = int(str(offset).lower().rstrip("h"), 16) if re.search(r"h$|^0x", str(offset).lower()) else int(str(offset))

            def pick(rs: list[list], off=off) -> tuple[list[int], list[int]]:
                hit = [i for i, r in enumerate(rs) if not is_sub(r) and (rg := parse_byte_range(clean(r[0]))) and min(rg) <= off <= max(rg)]
                return _with_subs(rs, hit), []
        elif field:
            fq = clean(field).lower()
            word = re.compile(rf"\b{re.escape(fq)}\b", re.I)

            def pick(rs: list[list], fq=fq, word=word) -> tuple[list[int], list[int]]:
                """(rows to show, rows that only mention the field): the rows that define it (its abbreviation or name) with the bit
                rows under them; only when none does, every row that mentions it."""
                defining = [i for i, r in enumerate(rs) if fq in {x.lower() for x in _split_field(_desc(r))[:2] if x}]
                mention = [i for i, r in enumerate(rs) if i not in defining and word.search(" ".join(clean(x) for x in r))]
                if defining:
                    show = _with_subs(rs, defining, context)
                    return show, [i for i in mention if i not in show]
                return mention, []
        # the best-named table, or with a filter the best-named one that has a matching row
        _, d, t = next(((sc, cd, ct) for sc, cd, ct in cands if not pick or pick(ct.get("rows") or [])[0]), cands[0])
        ir = self.ir(d)
        rs = t.get("rows") or []
        idx, others = pick(rs) if pick else (list(range(len(rs))), [])
        if pick and not idx:
            raise NotFound(f"no row of Figure {fig_no(t['label'])} ({_name_of(t.get('caption'))}) matches " + (f"offset {offset}" if offset not in (None, "") else f"'{field}'"))
        shown = [(_brief(rs[i]) if i in context else rs[i]) for i in idx] if pick else rs
        extra = []
        if others:
            names = [_row_name(rs, i) for i in others]
            extra = [f"_Also mentioned in {len(others)} other row{'s' if len(others) > 1 else ''}: " + "; ".join(names[:8])
                     + (" …" if len(names) > 8 else "") + " (ask for that field to see it)._", ""]
        lines.append(self.render_table(d, ir, t, level=1, shown=shown, extra=extra))
        if pick:
            for r in shown:
                for rd, rir, rt in self._figure_refs(" ".join(clean(x) for x in r), d, ir, exclude={t["label"]})[:4]:
                    lines.append(self.ref_table(rd, rir, rt, level=2))
        alts = [(sc, od, ot) for sc, od, ot in cands[:6] if ot is not t]
        if alts:
            lines.append("Other matches:\n" + "\n".join(f"- {od['family']} {od['version']} Figure {fig_no(ot['label'])}: {_name_of(ot.get('caption'))}"
                                                     for _, od, ot in alts[:5]))
        text = paginate("\n\n".join(lines), page)
        return answer(text, structure_data(d, ir, t, shown, narrowed=bool(pick)) if page == 1 else None)

    # ------------------------------------------------------------------------- references
    def reference(self, text: str, spec: str | None = None, version: str | None = None, page: int = 1) -> str:
        d, note = self.resolve_doc(spec, version)
        ir = self.ir(d)
        refs = extract_references(text, d["family"])
        q = clean(text)
        if not refs:
            if re.fullmatch(r"(?:§|sec(?:tion)?\.?\s*)?\d+(?:\.\d+)*", q, re.I):
                return self.section(d["family"], d["version"], q, page=page)
            if re.fullmatch(r"(?:fig(?:ure)?|table)\.?\s*\d+[A-Za-z]?", q, re.I):
                return self.table(d["family"], d["version"], q, page=page)
            raise NotFound(f"no section, figure or specification reference found in '{text}'")
        parts = [note] if note else []
        for r in refs[:6]:
            if r.family is None:
                td, tir = d, ir
            else:
                docs = self.families().get(r.family)
                if not docs:
                    parts.append(f"## {r.raw}\n\n{FAMILY_NAMES.get(r.family, r.family)} is not parsed on this server.")
                    continue
                td = docs[-1]
                tir = self.ir(td)
            if r.kind == "spec":
                parts.append(f"## {r.raw}\n\nRefers to {spec_name(td)} as a whole · `{uri(td, '')}`")
            elif r.kind == "figure" and (t := self.fig(tir, r.label)):
                parts.append(self.render_table(td, tir, t, level=2))
            elif r.kind == "section" and r.label in tir["sections"]:
                sub: list[str] = []
                self._render_section(td, tir, r.label, 0, True, sub, top=True, level=2)
                parts.append("\n\n".join(sub))
            else:
                parts.append(f"## {r.raw}\n\n{spec_name(td)} has no {r.kind} {r.label}.")
        return paginate("\n\n".join(parts), page)

    # ------------------------------------------------------------------------- comparisons
    def _pair(self, spec: str | None, from_version: str | None, to_version: str | None) -> tuple[dict, dict]:
        if not spec:
            raise QueryError("name the specification to compare (e.g. 'nvme-base')")
        d_to, _ = self.resolve_doc(spec, to_version)          # "nvme-base@2.0" / "NVMe Base 2.0" name the version compared
        docs = self.families()[d_to["family"]]
        if from_version and _norm_version(from_version) not in ("previous", "prior", "prev", "older"):
            d_from, _ = self.pick_version(docs, from_version)
        else:
            older = [x for x in docs if version_key(x["version"]) < version_key(d_to["version"])]
            if not older:
                raise NotFound(f"no parsed version of {d_to['family']} is older than {d_to['version']} "
                               f"(parsed: {', '.join(x['version'] for x in docs)})")
            d_from = older[-1]
        if d_from["run_id"] == d_to["run_id"]:
            raise QueryError("choose two different versions")
        return d_from, d_to

    def start_comparison(self, d_from: dict, d_to: dict) -> tuple[str, int]:
        """("done", comparison id) when the pair is compared, else ("running", job id) of the job computing it: queued here when
        there is none (one job per pair, however many agents ask at once). A comparison that failed in the last FAILED_FOR is
        reported again rather than started again, so asking repeatedly cannot keep the queue busy with it."""
        c = self.conn()
        sql = "SELECT id FROM spec_compare WHERE run_a=? AND run_b=? AND algo=? ORDER BY id DESC LIMIT 1"
        pair = (d_from["run_id"], d_to["run_id"])
        if r := row(c, sql, (*pair, compare.ALGO_VERSION)):
            return "done", r["id"]
        with self._compare_lock:
            if r := row(c, sql, (*pair, compare.ALGO_VERSION)):
                return "done", r["id"]
            of_pair = "type='compare' AND json_extract(payload_json,'$.from_run')=? AND json_extract(payload_json,'$.to_run')=?"
            if busy := row(c, f"SELECT id FROM job WHERE {of_pair} AND state IN ('queued','running') ORDER BY id DESC LIMIT 1", pair):
                return "running", busy["id"]
            if failed := row(c, f"SELECT error FROM job WHERE {of_pair} AND state='failed' AND updated >= datetime('now', ?) "
                                f"ORDER BY id DESC LIMIT 1", (*pair, f"-{FAILED_FOR} seconds")):
                raise QueryError(f"comparing {spec_name(d_from)} with {d_to['version']} failed recently (not retried for "
                                 f"{FAILED_FOR // 60} minutes): {str(failed['error'] or '').splitlines()[0][:300]}")
            a, b = pair
            job_id = jobs.submit(c, "compare", lambda cc, prog: {"compare_id": compare.run_comparison(cc, a, b, progress=prog)},
                                 {"from_run": a, "to_run": b, "source": "mcp"}, self.db(), user="mcp",
                                 label=f"Compare {d_from['family']} {d_from['version']} → {d_to['version']} (MCP)")
            return "running", job_id

    def poll_comparison(self, job_id: int, d_from: dict, d_to: dict) -> int | None:
        """The comparison id once the job is done, None while it runs; raises when it failed."""
        j = row(self.conn(), "SELECT state, error, result_json FROM job WHERE id=?", (job_id,))
        if j and j["state"] == "done":
            return (jloads(j["result_json"], {}) or {}).get("compare_id")
        if not j or j["state"] in ("failed", "cancelled"):
            raise QueryError(f"comparing {spec_name(d_from)} with {d_to['version']} failed: "
                             f"{str((j or {}).get('error') or 'job missing').splitlines()[0][:300]}")
        return None

    def comparison(self, d_from: dict, d_to: dict, wait: float | None = None) -> int | None:
        """The stored comparison of two parse runs, waiting up to `wait` seconds for one being computed (None if still running).
        Blocks its thread while it waits: the MCP server waits asynchronously instead (mcp_server.wait_comparison)."""
        state, n = self.start_comparison(d_from, d_to)
        deadline = time.monotonic() + (self.compare_wait_s if wait is None else wait)
        while state == "running":
            if (cid := self.poll_comparison(n, d_from, d_to)) is not None:
                return cid
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)
        return n

    def _computing(self, d_from: dict, d_to: dict) -> str:
        return (f"The comparison of {spec_name(d_from)} with {d_to['version']} is being computed on the server (it is done once, then kept). "
                f"Retry this call in a minute.")

    def changes(self, spec: str, from_version: str | None = None, to_version: str | None = None, impact: str | None = None,
                kind: str | None = None, query: str | None = None, page: int = 1) -> str:
        d_from, d_to = self._pair(spec, from_version, to_version)
        cid = self.comparison(d_from, d_to)
        if cid is None:
            return self._computing(d_from, d_to)
        comp = compare.get_comparison(self.conn(), cid) or {}
        s = comp.get("summary") or {}
        lines = [f"# Changes: {spec_name(d_from)} → {d_to['version']}", ""]
        bi = s.get("by_impact") or {}
        lines.append(f"{s.get('total', 0)} changes: " + ", ".join(f"{bi.get(i, 0)} {i}" for i in compare.IMPACTS) + ".")
        ai = comp.get("ai") or {}
        if ai.get("overview") and not (impact or kind or query):
            lines += ["", "**AI summary (written by a model, check against the changes below):** " + clean(ai["overview"])]
            if ai.get("breaking"):
                lines += [""] + [f"- breaking: {clean(b)}" for b in ai["breaking"][:8]]
        ents = s.get("entities") or {}
        if ents and not (impact or kind or query):
            for g, title in (("commands_added", "New commands"), ("commands_modified", "Commands modified"), ("commands_removed", "Commands removed"),
                             ("structures_added", "New data structures"), ("structures_modified", "Data structures modified"),
                             ("structures_removed", "Data structures removed")):
                items = (ents.get(g) or {}).get("items") or []
                if items:
                    lines += ["", f"## {title} ({(ents.get(g) or {}).get('total', len(items))})", ""]
                    for e in items[:60]:
                        lines.append(f"- **{e['name']}**" + (f" ({e['opcode']})" if e.get("opcode") else "") + f" [{e['impact']}] — " +
                                     "; ".join(x["text"] for x in e["changes"][:3]))
        if kind and kind not in ("section", "table"):
            raise QueryError("kind is 'section' or 'table'")
        if impact and impact not in compare.IMPACTS:
            raise QueryError(f"impact is one of {', '.join(compare.IMPACTS)}")
        res = compare.query_changes(self.conn(), cid, kind=kind, impact=impact, q=query, nested=True, limit=2000, sort="importance" if not query else "document")
        lines += ["", f"## Change list ({res['total']}" + (" matching" if (impact or kind or query) else "") + ")", ""]
        for ch in res["items"]:
            lines.append(f"- #{ch['id']} [{ch['impact']}] {describe(ch)}")
        lines += ["", "Use diff with item=<command, structure, section number or figure number> for the details of one item."]
        return paginate("\n".join(lines), page)

    def diff(self, spec: str, item: str, from_version: str | None = None, to_version: str | None = None, page: int = 1) -> str:
        d_from, d_to = self._pair(spec, from_version, to_version)
        cid = self.comparison(d_from, d_to)
        if cid is None:
            return self._computing(d_from, d_to)
        conn = self.conn()
        comp = compare.get_comparison(conn, cid) or {}
        ents = (comp.get("summary") or {}).get("entities") or {}
        q = clean(item)
        ids: list[int] | None = None
        label = q
        best = (0.0, None, None)
        for g, grp in ents.items():
            for e in grp.get("items") or []:
                sc = 3.0 if _ck(e["name"]) == _ck(q) else _similar(q, e["name"])
                if sc > best[0]:
                    best = (sc, g, e)
        if best[0] >= 0.75:
            g, e = best[1], best[2]
            ids = e.get("change_ids") or [x["id"] for x in e["changes"]]
            label = f"{e['name']}" + (f" ({e['opcode']})" if e.get("opcode") else "") + f" — {g.replace('_', ' ')}"
        elif (m := re.fullmatch(r"(?:fig(?:ure)?|table)\.?\s*(\d+[A-Za-z]?)", q, re.I)):
            f = m.group(1)
            ids = [r["ord"] for r in rows(conn, "SELECT ord FROM compare_change WHERE compare_id=? AND kind='table' AND (number_from=? OR number_to=? "
                                                "OR number_from LIKE ? OR number_to LIKE ?)", (cid, f, f, f + "#%", f + "#%"))]
            label = f"Figure {m.group(1)}"
        elif (m := re.fullmatch(r"(?:§|sec(?:tion)?\.?\s*)?(\d+(?:\.\d+)*)", q, re.I)):
            n = m.group(1)
            ids = [r["ord"] for r in rows(conn, "SELECT ord FROM compare_change WHERE compare_id=? AND (number_from=? OR number_to=? OR "
                                                "number_from LIKE ? OR number_to LIKE ? OR json_extract(detail_json,'$.sec')=?)",
                                          (cid, n, n, n + ".%", n + ".%", n))]
            label = f"§{n} and its sub-sections"
        lines = [f"# {label}: {spec_name(d_from)} → {d_to['version']}", ""]
        if ids is None and self._names_entity(q, d_from) and self._names_entity(q, d_to):
            moved = ""
            ka = self.ir(d_from)["_version"].cmds.get(_ck(q), {})
            kb = self.ir(d_to)["_version"].cmds.get(_ck(q), {})
            if ka.get("section") and kb.get("section") and ka["section"] != kb["section"]:
                moved = f" Only its section number changed: §{ka['section']} → §{kb['section']}."
            lines.append(f"No change: '{item}' is the same in both versions.{moved}")
            return "\n".join(lines)
        res = compare.query_changes(conn, cid, ids=ids, limit=500) if ids is not None else compare.query_changes(conn, cid, q=q, limit=200)
        if not res["items"]:
            existed = self._exists_in(q, d_from), self._exists_in(q, d_to)
            if existed == (True, True):
                lines.append(f"No change: '{item}' is the same in both versions.")
                return "\n".join(lines)
            raise NotFound(f"'{item}' is not a command, structure, section or figure that changed between {d_from['version']} and "
                           f"{d_to['version']}, and it was not found in both versions (try list_changes with query=...)")
        if ids is None:
            lines.append(f"_Matched by text: every change mentioning '{item}'._\n")
        for ch in res["items"]:
            lines += self._render_change(ch)
        return paginate("\n".join(lines), page)

    def _names_entity(self, q: str, d: dict) -> bool:
        """q is the name of a command or data structure of this version (so a diff without changes to it means 'no change')."""
        V = self.ir(d)["_version"]
        return _ck(q) in V.cmds or any(_similar(q, name) >= 1.0 for name in V.structs.values())

    def _exists_in(self, q: str, d: dict) -> bool:
        ir = self.ir(d)
        V = ir["_version"]
        return _ck(q) in V.cmds or any(_similar(q, _name_of(t.get("caption"))) >= 0.75 for t in ir["tables"].values()) or \
            any(_similar(q, s["title"]) >= 0.75 for s in ir["sections"].values())

    @staticmethod
    def _render_change(ch: dict) -> list[str]:
        where = ("Figure " if ch["kind"] == "table" else "§") + str(ch.get("number_to") or ch.get("number_from"))
        title = _name_of(ch["title"]) if ch["kind"] == "table" else ch["title"]
        head = f"## [{ch['impact']}] {where} {title} — {ch['change']}"
        if ch.get("number_from") and ch.get("number_to") and ch["number_from"] != ch["number_to"]:
            head += f" (was {('Figure ' if ch['kind'] == 'table' else '§')}{ch['number_from']})"
        out = [head]
        d = ch.get("details") or {}
        if ch.get("title_from") and _name_of(ch["title_from"]) != _name_of(ch["title"]):
            out.append(f"- renamed from “{_name_of(ch['title_from']) if ch['kind'] == 'table' else ch['title_from']}”")
        if ch["change"] in ("added", "removed"):
            if ch["kind"] == "table":
                hdr = d.get("header") or []
                rs = d.get("rows") or []
                if rs:
                    out.append(md_table(hdr, rs[:12]) + ("\n…" if d.get("row_count", 0) > 12 else ""))
            else:
                out += [f"> {clean(x)}" for x in (d.get("excerpt") or [])[:3]]
        elif ch["kind"] == "section":
            for a in d.get("added") or []:
                out.append(f"- added{' (' + a['strength'] + ')' if a.get('strength') else ''}: {clean(a['text'])}")
            for r in d.get("removed") or []:
                out.append(f"- removed{' (' + r['strength'] + ')' if r.get('strength') else ''}: {clean(r['text'])}")
            for m in d.get("modified") or []:
                st = m.get("strength") or [None, None]
                lvl = f" ({st[0] or 'informative'} → {st[1] or 'informative'})" if st[0] != st[1] else ""
                out.append(f"- changed{lvl}: “{clean(m['before'])}” → “{clean(m['after'])}”")
            if d.get("truncated"):
                out.append("- … more sentences changed than are kept per section")
        else:
            if d.get("header_changed"):
                out.append(f"- columns: {' | '.join(d.get('header_before') or [])} → {' | '.join(d.get('header_after') or [])}")
            for r in d.get("rows_added") or []:
                out.append("- row added: " + " | ".join(_cell(x) for x in r))
            for r in d.get("rows_removed") or []:
                out.append("- row removed: " + " | ".join(_cell(x) for x in r))
            for r in d.get("rows_changed") or []:
                key = r.get("key") or ""
                moved = f" (was {r['key_before']})" if r.get("rekeyed") and r.get("key_before") != key else ""
                cells = "; ".join(f"{c['column']}: “{_cell(c['before'])}” → “{_cell(c['after'])}”" for c in r.get("cells") or [])
                out.append(f"- row {key}{moved} changed: {cells}")
            if d.get("truncated"):
                out.append(f"- … {d.get('rows_added_count', 0)} added, {d.get('rows_removed_count', 0)} removed, "
                           f"{d.get('rows_changed_count', 0)} changed rows in all")
        out.append("")
        return out

    # ------------------------------------------------------------------------- the Docling Markdown
    def raw_only(self) -> list[dict]:
        """Finished To Markdown conversions whose file is not a parsed spec (served by read_raw only)."""
        docs = self.documents()
        fp = tuple(self.conn().execute("SELECT COUNT(*), MAX(id), TOTAL(length(payload_json)) FROM job "
                                       "WHERE type='convert-markdown' AND state='done'").fetchone()) + tuple(d["sha256"] for d in docs)
        if self._raw_only[0] == fp:              # every search asks: read the jobs again only when a conversion came or went
            return list(self._raw_only[1])
        parsed = {d["sha256"] for d in docs}
        out, seen = [], set()
        for j in rows(self.conn(), "SELECT payload_json, result_json FROM job WHERE type='convert-markdown' AND state='done' "
                                   "AND json_extract(payload_json, '$.hidden') IS NULL ORDER BY id DESC"):
            p, r = jloads(j["payload_json"], {}) or {}, jloads(j["result_json"], {}) or {}
            sha = p.get("sha256")
            if not r.get("dir") or sha in parsed or sha in seen:
                continue
            seen.add(sha)
            out.append({"filename": p.get("filename") or r.get("filename") or r["dir"], "dir": r["dir"], "sha256": sha})
        self._raw_only = (fp, out)
        return list(out)

    def _markdown_file(self, sha: str | None = None, dir_: str | None = None) -> Path | None:
        root = (workspace_dir() / "conversions").resolve()
        cands = [dir_] if dir_ else []
        if sha:
            for j in rows(self.conn(), "SELECT result_json FROM job WHERE type='convert-markdown' AND state='done' "
                                       "AND json_extract(payload_json, '$.sha256') = ? ORDER BY id DESC", (sha,)):
                cands.append((jloads(j["result_json"], {}) or {}).get("dir"))
        for c in cands:
            if not c:
                continue
            f = (root / c / "out.md").resolve()
            if f.parent.parent == root and f.is_file():
                return f
        return None

    def _lib_doc(self, spec: str | None, libs: list[dict]) -> dict | None:
        """The converted document a name means: its whole file name, else a part of it unless the name is a parsed
        specification's id, short name or title ("base", "nvm", "nvme-base@2.4" stay specs even when a file is called
        "nvme-base-ecn-042.pdf"). Tried before the fuzzy spec-name matching, which would take "mi-ecn-123" for NVMe-MI."""
        want = clean(spec or "").lower()
        if len(want) < 3:
            return None
        if exact := next((r for r in libs if want == r["filename"].lower()), None):
            return exact
        bare = clean(_VERSION_IN.sub(" ", want.split("@", 1)[0]))
        names = set(FAMILY_ALIASES) | set(FAMILY_NAMES) | {n.lower() for n in FAMILY_NAMES.values()}
        for fam, ds in self.families().items():
            names |= {fam.lower(), *((d.get("title") or "").lower() for d in ds)}
        if want in names or bare in names:
            return None
        return next((r for r in libs if want in r["filename"].lower()), None)

    def raw(self, spec: str | None, version: str | None = None, around: str | None = None, context: int = 40, page: int = 1,
            section: str | None = None, lines: str | None = None) -> str:
        f, title, note = None, "", ""
        lib_hit = self._lib_doc(spec, self.raw_only())
        if lib_hit:
            f, title = self._markdown_file(dir_=lib_hit["dir"]), lib_hit["filename"]
        else:
            try:
                d, note = self.resolve_doc(spec, version)
                f, title = self._markdown_file(sha=d["sha256"]), spec_name(d)
            except QueryError:
                raise QueryError(f"no parsed specification or converted file matches '{spec}' (list_specs lists both)") from None
        if f is None:
            raise NotFound(f"{title} has no Docling Markdown on this server (it was parsed without a To Markdown conversion)")
        if section and lines:
            raise QueryError("give section or lines, not both")
        md = self.markdown(f)
        text, all_lines = md["text"], md["lines"]
        lo, hi, part = 1, len(all_lines), ""
        if section or lines:
            lo, hi, part = self._raw_range(md, title, section, lines)
        note_md = note + "\n\n" if note else ""
        if not around:
            if part:
                return paginate(f"# {title} — {part}\n\n{note_md}" + "\n".join(all_lines[lo - 1:hi]).strip("\n"), page)
            return paginate(f"# {title} (Docling Markdown)\n\n{note_md}" + text, page)
        head = f"# {title} (Docling Markdown, around “{around}”{' in ' + part if part else ''})\n\n{note_md}"
        lines_ = all_lines
        pat = re.compile(re.escape(around.strip()), re.I)
        hits = [i for i in range(lo - 1, hi) if pat.search(lines_[i])]
        if not hits:
            raise NotFound(f"'{around}' does not occur in " + (f"{part} of {title}" if part else f"the Markdown of {title}"))
        ctx = max(5, min(int(context or 40), 400))
        blocks, last = [], -1
        for i in hits[:20]:
            a, b = max(lo - 1, i - ctx), min(hi, i + ctx + 1)
            if a <= last:
                a = last + 1
            if a >= b:
                continue
            blocks.append(f"_lines {a + 1}–{b}_\n\n" + "\n".join(lines_[a:b]))
            last = b - 1
        extra = f"\n\n_{len(hits)} occurrences; the first 20 are shown._" if len(hits) > 20 else ""
        return paginate(head + "\n\n---\n\n".join(blocks) + extra, page)

    def markdown(self, f: Path) -> dict:
        """{text, lines, outline} of a Markdown file, kept for the MD_CACHE files read last while the file is unchanged (its
        size and modification time): a spec's Markdown is megabytes, read and split again by every read_raw otherwise."""
        st = f.stat()
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            e = self._md.get(str(f))
            if e and e["key"] == key:
                self._md.move_to_end(str(f))
                return e
        text = f.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        e = {"key": key, "text": text, "lines": lines, "outline": md_outline(lines)}
        with self._lock:
            self._md[str(f)] = e
            while len(self._md) > MD_CACHE:
                self._md.popitem(last=False)
        return e

    @staticmethod
    def _raw_range(md: dict, title: str, section: str | None, span: str | None) -> tuple[int, int, str]:
        """(first line, last line, label) of one section of the Markdown (by number or heading, with its sub-sections) or of a
        line range ("120-180"). Lines count from 1."""
        lines = md["lines"]
        n = len(lines)
        if span:
            m = re.fullmatch(r"\s*(\d+)\s*(?:[-–:]\s*(\d+))?\s*", span)
            if not m:
                raise QueryError("lines is a line number or a range like 120-180 (as search hits give them)")
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if a > b:
                raise QueryError(f"lines {a}-{b} is reversed: give the lower line first ({b}-{a})")
            if a < 1 or a > n:
                raise NotFound(f"{title} has {n} lines: line {a} is not one of them")
            b = min(n, b)
            return a, b, f"lines {a}–{b}"
        heads = md["outline"]
        want = re.sub(r"^sec(?:tion)?\.?\s*", "", clean(section or "").lstrip("§").strip(), flags=re.I).rstrip(".")
        same = [h for h in heads if h["number"] and h["number"] == want]
        # the same number twice (a stray bold line, a heading the converter repeated): the one that spans the most is the section
        h = max(same, key=lambda h: h["end"] - h["line"], default=None)
        if h is None and not re.fullmatch(r"[\d.]+", want):
            best = max(heads, key=lambda h: _similar(want, h["title"]), default=None)
            h = best if best and _similar(want, best["title"]) >= 0.5 else None
        if h is None:
            raise NotFound(f"{title} has no section '{section}' (search_spec with spec=\"{title}\" finds where a topic is)")
        label = f"§{h['number']} {h['title']}" if h["number"] else h["title"]
        return h["line"] + 1, h["end"], f"{label}, lines {h['line'] + 1}–{h['end']}"

    # ------------------------------------------------------------------------- resources
    def resource(self, spec_at_version: str, node: str) -> str:
        if "@" not in spec_at_version:
            raise QueryError("resource URIs look like specforge://nvme-base@2.4/sec:5.2 or specforge://nvme-base@2.4/fig:101")
        fam, ver = spec_at_version.split("@", 1)
        d, _ = self.resolve_doc(fam, ver)
        kind, _, label = node.partition(":")
        if kind == "sec":
            return self.section(d["family"], d["version"], label, depth=0)
        if kind == "fig":
            return self.table(d["family"], d["version"], label)
        raise QueryError("node is sec:<number> or fig:<number>")


def summarize_args(args: dict[str, Any], limit: int = 160) -> str:
    s = json.dumps({k: v for k, v in args.items() if v not in (None, "")}, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"
