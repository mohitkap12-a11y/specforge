"""Markdown building blocks shared by the PDF and Word converters.

Both converters produce a flat list of blocks (Heading, Para, Item, Code, Table, Quote); `render` turns it into
clean GitHub-flavoured Markdown: text is escaped where it would otherwise be read as markup, emphasis markers never
swallow surrounding whitespace, list items stay tight, and blank lines are normalised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Seg:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    sup: bool = False
    sub: bool = False
    link: str | None = None


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Para:
    segs: list[Seg]


@dataclass
class Item:
    level: int
    segs: list[Seg]
    ordered: bool = False
    number: str = "1"
    label: str = ""   # non-decimal label ("a)", "(ii)") kept as text because Markdown cannot number with it


@dataclass
class Code:
    lines: list[str]


@dataclass
class Table:
    rows: list[list[str]]   # cells already carry inline markdown; "\n" inside a cell becomes <br>
    caption: str = ""


@dataclass
class Quote:
    segs: list[Seg]


@dataclass
class Raw:
    text: str   # already Markdown


Block = Heading | Para | Item | Code | Table | Quote | Raw

_LINE_START = re.compile(r"^(\s*)(#{1,6}(?=\s|$)|[-+*](?=\s|$)|>|\d{1,9}(?=[.)](?:\s|$))|={2,}$|-{2,}$)")
_UNDERSCORE = re.compile(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def escape_text(s: str) -> str:
    """Escape characters that would be read as Markdown/HTML. Intra-word underscores (snake_case) are left alone."""
    s = s.replace("\\", "\\\\").replace("*", "\\*").replace("`", "\\`").replace("<", "\\<")
    return _UNDERSCORE.sub(r"\\_", s)


def _wrap(text: str, marker: str) -> str:
    core = text.strip(" ")
    if not core:
        return text
    lead, trail = text[: len(text) - len(text.lstrip(" "))], text[len(text.rstrip(" ")):]
    return f"{lead}{marker}{core}{marker}{trail}"


def _longest_run(text: str, ch: str = "`") -> int:
    return max((len(m) for m in re.findall(f"{re.escape(ch)}+", text)), default=0)


def _fmt(s: Seg) -> str:
    if s.code:
        t = s.text.replace("\n", " ")
        if not t.strip():
            return t
        fence = "`" * (_longest_run(t) + 1)   # longer than any backtick run inside, so the span cannot close early
        core = t.strip(" ")
        lead, trail = t[: len(t) - len(t.lstrip(" "))], t[len(t.rstrip(" ")):]
        pad = " " if core.startswith("`") or core.endswith("`") else ""
        return f"{lead}{fence}{pad}{core}{pad}{fence}{trail}"
    out = escape_text(s.text)
    # collapse runs of spaces in prose only; code keeps its literal spacing
    out = re.sub(r"(?<=\S)[ ]{2,}(?=\S)", " ", out.replace("\t", " "))
    if s.sup:
        out = f"<sup>{out}</sup>" if out.strip() else out
    elif s.sub:
        out = f"<sub>{out}</sub>" if out.strip() else out
    if s.bold and s.italic:
        out = _wrap(out, "***")
    elif s.bold:
        out = _wrap(out, "**")
    elif s.italic:
        out = _wrap(out, "*")
    return out


def merge_segs(segs: list[Seg]) -> list[Seg]:
    """Join neighbours with identical formatting so a bold phrase is one **...** and not one per run."""
    segs = [s for s in segs if s.text]
    # a space between two bold words belongs to the bold phrase: **a b**, not **a** **b**
    for i in range(1, len(segs) - 1):
        a, s, c = segs[i - 1], segs[i], segs[i + 1]
        if not s.text.strip() and not s.code and _same(a, c) and (a.bold or a.italic or a.link):
            segs[i] = Seg(s.text, a.bold, a.italic, a.code, a.sup, a.sub, a.link)
    out: list[Seg] = []
    for s in segs:
        if out and _same(out[-1], s):
            out[-1] = Seg(out[-1].text + s.text, s.bold, s.italic, s.code, s.sup, s.sub, s.link)
        else:
            out.append(s)
    return out


def _same(a: Seg, b: Seg) -> bool:
    return (a.bold, a.italic, a.code, a.sup, a.sub, a.link) == (b.bold, b.italic, b.code, b.sup, b.sub, b.link)


def inline(segs: list[Seg]) -> str:
    out = []
    for s in merge_segs(segs):
        # whitespace-only runs carry no formatting
        if not s.text.strip() and not s.code:
            s = Seg(s.text)
        piece = _fmt(s)
        if s.link and s.text.strip():
            lead, trail = piece[: len(piece) - len(piece.lstrip())], piece[len(piece.rstrip()):]
            core = piece.strip().replace("]", "\\]")
            piece = f"{lead}[{core}]({s.link.replace(' ', '%20').replace(')', '%29')}){trail}"
        out.append(piece)
    text = "".join(out).replace("\r", "").strip(" \n")
    return re.sub(r" *\n *", "  \n", text)


def _escape_start(text: str) -> str:
    m = _LINE_START.match(text)
    if not m:
        return text
    i = m.end(2) if m.group(2)[:1].isdigit() else m.end(1)
    return text[:i] + "\\" + text[i:]


def table_md(rows: list[list[str]]) -> str:
    n = max((len(r) for r in rows), default=0)
    rows = [[(c or "").strip() for c in r] + [""] * (n - len(r)) for r in rows]

    def cell(c: str) -> str:
        c = re.sub(r"(?<!\\)\|", r"\\|", c)
        return re.sub(r"\s*\n\s*", "<br>", c)

    head, body = rows[0], rows[1:]
    lines = ["| " + " | ".join(cell(c) for c in head) + " |", "|" + "|".join(" --- " for _ in head) + "|"]
    lines += ["| " + " | ".join(cell(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def render(blocks: list[Block]) -> str:
    out: list[str] = []
    prev: Block | None = None
    for b in blocks:
        if isinstance(b, Item):
            text = inline(b.segs)
            if not text:
                continue
            ind = "    " * b.level
            if b.ordered and not b.label:
                chunk = f"{ind}{b.number}. {text}"
            else:
                chunk = f"{ind}- {(b.label + ' ') if b.label else ''}{text}"
            text_line = chunk.replace("  \n", "  \n" + ind + "  ")
            if isinstance(prev, Item) and (prev.ordered == b.ordered or b.level > 0):
                out[-1] += "\n" + text_line
            else:
                out.append(text_line)
        elif isinstance(b, Heading):
            t = re.sub(r"\s+", " ", b.text).strip()
            if not t:
                continue
            out.append("#" * max(1, min(6, b.level)) + " " + escape_text(t).replace("\n", " "))
        elif isinstance(b, Para):
            t = inline(b.segs)
            if t:
                out.append(_escape_start(t))
        elif isinstance(b, Quote):
            t = inline(b.segs)
            if t:
                out.append("\n".join("> " + ln for ln in t.split("\n")))
        elif isinstance(b, Code):
            lines = list(b.lines)
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            if lines:
                body = "\n".join(lines)
                fence = "`" * max(3, _longest_run(body) + 1)
                out.append(f"{fence}\n{body}\n{fence}")
        elif isinstance(b, Raw):
            if b.text.strip():
                out.append(b.text)
        elif isinstance(b, Table):
            if b.rows:
                out.append(table_md(b.rows))
        prev = b
    return "\n\n".join(out).strip() + "\n"
