"""Deterministic normalisation helpers: bit ranges, tables, normative statements, references."""
from __future__ import annotations

import re
from dataclasses import dataclass

# ----------------------------------------------------------------------------- text helpers

_WS = re.compile(r"\s+")
_DASHES = str.maketrans({"\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2010": "-", "\u2011": "-"})
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})


# PDF hyperlinks put the visible text in the page twice ("Figure 336Figure 336"); left in, the reference reads as "Figure 336F"
_DOUBLED_LINK = re.compile(r"\b((?:Figure|Table|[Ss]ection)\s+\d+(?:\.\d+)*[A-Za-z]?)\s?\1(?!\w|\.\d)")


def clean(s: str | None) -> str:
    if s is None:
        return ""
    return _DOUBLED_LINK.sub(r"\1", _WS.sub(" ", s.translate(_QUOTES)).strip())


def norm_text(s: str) -> str:
    """Whitespace / quote / dash normalisation WITHOUT case folding (used for verbatim-quote checks)."""
    return _WS.sub(" ", s.translate(_DASHES).translate(_QUOTES)).strip()


def norm_title(s: str) -> str:
    return _WS.sub(" ", s.translate(_DASHES).translate(_QUOTES)).strip().lower()


def nospace_len(s: str) -> int:
    return len(_WS.sub("", s))


# ----------------------------------------------------------------------------- bit ranges

_BITS = re.compile(r"^\s*(?:bits?\s*)?(\d{1,3})\s*(?::|-|\u2013|to)\s*(\d{1,3})\s*$", re.I)
_BIT1 = re.compile(r"^\s*(?:bit\s*)?(\d{1,3})\s*$", re.I)
_WRAPPED_DIGITS = re.compile(r"(:\s*\d+) (\d+\s*)$")   # '1791:102 4': the low end wrapped inside its cell
_BYTES = re.compile(r"^\s*(?:bytes?\s*)?(\d{1,5})\s*(?::|-|\u2013|to)\s*(\d{1,5})\s*$", re.I)


def parse_bit_range(cell: str) -> tuple[int, int] | None:
    """'31:16' -> (31, 16); '07' -> (7, 7); returns (hi, lo)."""
    c = clean(cell).translate(_DASHES)
    m = _BITS.match(c)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (max(a, b), min(a, b))
    m = _BIT1.match(c)
    if m:
        v = int(m.group(1))
        return (v, v)
    return None


def parse_byte_range(cell: str) -> tuple[int, int] | None:
    """'03:00' -> (3, 0); '1023' -> (1023, 1023). Returns (hi, lo)."""
    c = _WRAPPED_DIGITS.sub(r"\1\2", clean(cell).translate(_DASHES))
    m = _BYTES.match(c)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (max(a, b), min(a, b))
    m = re.match(r"^\s*(?:byte\s*)?(\d{1,5})\s*$", c, re.I)
    if m:
        v = int(m.group(1))
        return (v, v)
    return None


_HEX = re.compile(r"^\s*(?:0x)?([0-9A-Fa-f]{1,8})\s*h?\s*$")
_HEX_STRICT = re.compile(r"^\s*(?:0x([0-9A-Fa-f]+)|([0-9A-Fa-f]+)h)\s*$", re.I)


def parse_hex_value(cell: str) -> int | None:
    """'01h' / '0x01' -> 1.  Plain digits are read as hex only when suffixed; bare '10' -> decimal 10."""
    c = clean(cell)
    m = _HEX_STRICT.match(c)
    if m:
        return int(m.group(1) or m.group(2), 16)
    if re.fullmatch(r"[01]+b", c, re.I):
        return int(c[:-1], 2)
    if re.fullmatch(r"\d+", c):
        return int(c)
    return None


def parse_value_range(cell: str) -> tuple[int, int] | None:
    """'01h' -> (1,1); '04h to 0Fh' / '04h-0Fh' / '04h:0Fh' -> (4,15)."""
    c = clean(cell).translate(_DASHES)
    parts = re.split(r"\s*(?:to|-|:|\.\.)\s*", c, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        a, b = parse_hex_value(parts[0]), parse_hex_value(parts[1])
        if a is not None and b is not None:
            return (min(a, b), max(a, b))
        return None
    v = parse_hex_value(c)
    return (v, v) if v is not None else None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


# ----------------------------------------------------------------------------- captions

CAPTION_RE = re.compile(r"^\s*(Figure|Table)\s+(\d+[A-Za-z]?)\s*[:.\u2013\u2014-]\s*(.+?)\s*$", re.I)
CONTINUED_RE = re.compile(r"\(\s*continued\s*\)|\bcontinued\b\s*$", re.I)


def parse_caption(line: str) -> tuple[str, str, str, bool] | None:
    """-> (kind, label, title, continued)"""
    m = CAPTION_RE.match(clean(line))
    if not m:
        return None
    title = m.group(3)
    cont = bool(CONTINUED_RE.search(title))
    if cont:
        title = CONTINUED_RE.sub("", title).strip(" -:")
    return (m.group(1).lower(), m.group(2), title, cont)


# ----------------------------------------------------------------------------- table classification

def classify_table(header: list[str], caption: str) -> str:
    """Return one of: cdw | bytes | values | opcode | other."""
    h = [norm_title(x) for x in header]
    cap = norm_title(caption)
    first = h[0] if h else ""
    if first in ("bits", "bit") or first.startswith("bits "):
        return "cdw"
    if first in ("bytes", "byte") or first.startswith("bytes "):
        return "bytes"
    if any("opcode" in x for x in h) and any(x.startswith("command") or "command name" in x for x in h):
        return "opcode"
    if len(h) >= 2 and ("value" in first or first in ("code", "id", "identifier")
                        or re.search(r"\b(cns|fid|lid|sel|lsp|opcode|feature identifier|log page identifier)\b", first)):
        return "values"
    if "command dword" in cap and (first.startswith("bit") or "bit" in first):
        return "cdw"
    return "other"


def compact_table(header: list[str], rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Undo merged-cell padding from PDF table extraction.

    One logical column often arrives as several physical ones, the extras empty in every data row.  Such columns are
    dropped.  A header with a heading over every kept column loses the same columns; otherwise (its cells sit over the merged
    span, not over the data) it is compacted separately and padded to the new width.  A table with no always-empty column is returned unchanged, so genuinely blank cells stay put.
    """
    width = max([len(header), *(len(r) for r in rows)] or [0])
    pad = lambda r: [*r, *[""] * (width - len(r))]  # noqa: E731
    keep = [i for i in range(width) if any(clean(pad(r)[i]) for r in rows)]
    if not rows or len(keep) == width:
        return header, rows
    data = [[pad(r)[i] for i in keep] for r in rows]
    if len(header) == width and all(clean(header[i]) for i in keep):
        return [header[i] for i in keep], data         # every kept column has its own heading: drop the same columns
    head = [h for h in header if clean(h)]              # header cells sit over merged spans: compact them on their own
    return (head + [""] * (len(keep) - len(head)))[:len(keep)], data


def find_col(header: list[str], *needles: str) -> int | None:
    for i, h in enumerate(header):
        hn = norm_title(h)
        if any(n in hn for n in needles):
            return i
    return None


# ----------------------------------------------------------------------------- normative statements

_ABBREV = ["e.g.", "i.e.", "etc.", "vs.", "Fig.", "No.", "approx.", "cf.", "Sec.", "Ref."]
_NORMATIVE = [
    (re.compile(r"\bshall not\b", re.I), "prohibited", "shall not"),
    (re.compile(r"\bshould not\b", re.I), "prohibited", "should not"),
    (re.compile(r"\bmust not\b", re.I), "prohibited", "must not"),
    (re.compile(r"\bis prohibited\b", re.I), "prohibited", "is prohibited"),
    (re.compile(r"\bshall\b", re.I), "mandatory", "shall"),
    (re.compile(r"\bmust\b", re.I), "mandatory", "must"),
    (re.compile(r"\bis required\b", re.I), "mandatory", "is required"),
    (re.compile(r"\bshould\b", re.I), "recommended", "should"),
    (re.compile(r"\bmay\b", re.I), "optional", "may"),
]


def split_sentences(text: str) -> list[str]:
    t = clean(text)
    if not t:
        return []
    protected = t
    for i, a in enumerate(_ABBREV):
        if a == "etc.":     # "etc." often ENDS a sentence: protect it only when the text goes on in lower case
            protected = re.sub(r"etc\.(?!\s+[A-Z0-9(\"'])", f"etc\u0001{i}\u0001", protected)
        else:
            protected = protected.replace(a, a.replace(".", f"\u0001{i}\u0001"))
    # protect section numbers like 5.1.13 and hex like 01h.
    protected = re.sub(r"(\d)\.(\d)", "\\1\u0002\\2", protected)
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9(\"'])", protected)
    out = []
    for p in parts:
        p = p.replace("\u0002", ".")
        p = re.sub(r"\u0001(\d+)\u0001", lambda m: ".", p)
        p = p.strip()
        if p:
            out.append(p)
    return out


def normative_info(sentence: str) -> tuple[str, str] | None:
    for rx, strength, word in _NORMATIVE:
        if rx.search(sentence):
            return strength, word
    return None


# ----------------------------------------------------------------------------- references

SPEC_ALIASES: dict[str, str] = {
    # alias (lower-case)            -> family
    "nvm express base specification": "nvme-base",
    "nvme base specification": "nvme-base",
    "base specification": "nvme-base",
    "nvm express nvm command set specification": "nvme-nvm-cs",
    "nvme nvm command set specification": "nvme-nvm-cs",
    "nvm command set specification": "nvme-nvm-cs",
    "nvm express zoned namespace command set specification": "nvme-zns-cs",
    "zoned namespace command set specification": "nvme-zns-cs",
    "nvm express key value command set specification": "nvme-kv-cs",
    "key value command set specification": "nvme-kv-cs",
    "nvm express subsystem local memory command set specification": "nvme-slm-cs",
    "nvm express management interface specification": "nvme-mi",
    "nvme management interface specification": "nvme-mi",
    "nvme-mi specification": "nvme-mi",
    "management interface specification": "nvme-mi",
    "nvm express pcie transport specification": "nvme-pcie",
    "pcie transport specification": "nvme-pcie",
    "nvm express tcp transport specification": "nvme-tcp",
    "tcp transport specification": "nvme-tcp",
    "nvm express rdma transport specification": "nvme-rdma",
    "rdma transport specification": "nvme-rdma",
}
_ALIAS_RE = re.compile(
    "|".join(re.escape(a) for a in sorted(SPEC_ALIASES, key=len, reverse=True)), re.I
)
_LIST_SEP = r"(?:\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+)"      # "1, 2", "1 and 2", "1, 2, and 3", "1, 2, or 3"
_SEC_REF = re.compile(rf"\b(?:sections?|clauses?|subclauses?)\s+(\d+(?:\.\d+)*)(?:{_LIST_SEP}(\d+(?:\.\d+)*))*", re.I)
_SEC_NUM = re.compile(r"\d+(?:\.\d+)*")
# not followed by ":<digit>": "Figure 07:00 63" is a bit range a table extractor interleaved with "Figure 63"
_FIG_REF = re.compile(rf"\b(?:Figure|Table)s?\s+(\d+[A-Za-z]?)(?!\d|:\d)(?:{_LIST_SEP}(\d+[A-Za-z]?))*", re.I)


@dataclass
class RawRef:
    raw: str
    kind: str
    family: str | None
    label: str


def extract_references(text: str, own_family: str) -> list[RawRef]:
    """Extract section / figure / spec references; attach the nearest named spec in the same sentence."""
    out: list[RawRef] = []
    for sent in split_sentences(text) or [clean(text)]:
        specs = [(m.start(), m.end(), SPEC_ALIASES[m.group(0).lower()]) for m in _ALIAS_RE.finditer(sent)]
        refs: list[tuple[int, str, str, str]] = []  # (pos, kind, label, raw)
        for m in _SEC_REF.finditer(sent):
            for n in _SEC_NUM.findall(m.group(0)):
                refs.append((m.start(), "section", n, m.group(0)))
        for m in _FIG_REF.finditer(sent):
            labels = re.findall(r"\d+[A-Za-z]?", m.group(0))
            for lab in labels:
                refs.append((m.start(), "figure", lab, m.group(0)))
        for pos, kind, label, raw in refs:
            fam = None
            if specs:
                # nearest spec mention (before or after) within the sentence
                near = min(specs, key=lambda s: min(abs(s[0] - pos), abs(s[1] - pos)))
                fam = near[2]
            fam = None if fam in (None, own_family) else fam
            out.append(RawRef(raw=clean(raw), kind=kind, family=fam, label=label))
        if specs and not refs:
            for _, _, fam in specs:
                if fam != own_family:
                    out.append(RawRef(raw=sent[:80], kind="spec", family=fam, label=""))
    # de-duplicate preserving order
    seen = set()
    uniq = []
    for r in out:
        k = (r.kind, r.family, r.label)
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


# ----------------------------------------------------------------------------- spec identity

FAMILY_PATTERNS = [
    (re.compile(r"management\s+interface", re.I), "nvme-mi"),
    (re.compile(r"zoned\s+namespace", re.I), "nvme-zns-cs"),
    (re.compile(r"key\s*value", re.I), "nvme-kv-cs"),
    (re.compile(r"subsystem\s+local\s+memory", re.I), "nvme-slm-cs"),
    (re.compile(r"nvm\s+command\s+set", re.I), "nvme-nvm-cs"),
    (re.compile(r"pcie\s+transport", re.I), "nvme-pcie"),
    (re.compile(r"tcp\s+transport", re.I), "nvme-tcp"),
    (re.compile(r"rdma\s+transport", re.I), "nvme-rdma"),
    (re.compile(r"\bbase\s+specification", re.I), "nvme-base"),
]
_VERSION_RE = re.compile(r"\bRevision\s+(\d+(?:\.\d+)*[a-z]?)\b", re.I)


def _identity_from(text: str) -> str | None:
    best = None
    for rx, f in FAMILY_PATTERNS:
        m = rx.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), f)
    return best[1] if best else None


def detect_identity(first_pages_text: str, filename: str = "") -> tuple[str | None, str | None]:
    """first_pages_text: text of page 1, optionally followed by a form-feed and pages 2-3."""
    parts = first_pages_text.split("\f")
    title_page = clean(parts[0][:3000])
    fam = _identity_from(title_page) or _identity_from(clean(first_pages_text[:6000]))
    fname = filename.replace("-", " ").replace("_", " ")
    if fam is None:
        fam = _identity_from(fname)
    m = _VERSION_RE.search(title_page) or _VERSION_RE.search(clean(first_pages_text[:6000])) or _VERSION_RE.search(fname)
    return fam, (m.group(1) if m else None)
