"""The knowledge base the chatbot answers from.

Documents (Markdown from the To Markdown converter, or plain text / Markdown / HTML / JSON / CSV uploaded directly) are cut into
chunks along their headings and indexed in SQLite FTS5 (BM25, English stemming). No embedding model is needed: retrieval is
instant, fully offline and leaves the GPU to the chat model.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

from .core import config
from .core.db import row, rows

CHUNK_CHARS = 1400          # target size of a chunk; a chunk never splits a table row or a short code block
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".log", ".json", ".yaml", ".yml", ".csv", ".tsv", ".xml", ".html", ".htm"}
CONVERT_SUFFIXES = {".pdf", ".docx", ".doc"}      # converted to Markdown first (To Markdown queue), then added
STOP = set("a an the and or of to in on at for from by with is are was were be been it its this that these those as do does did how what which who "
           "when where why can could should would i you we they me my our your their there here about into than then so if not no any "
           "please tell show give explain describe".split())


class KnowledgeError(ValueError):
    """The document cannot be added; the message is safe to show."""


def kb_dir() -> Path:
    d = config.workspace_dir() / "kb"
    d.mkdir(exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------------------- reading text
class _Html(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "table", "ul", "ol", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div"):
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("latin-1")


def to_markdown(filename: str, raw: bytes) -> str:
    """Text of a directly uploadable file as Markdown. Structured data becomes a fenced block so its lines stay together."""
    suffix = Path(filename).suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        raise KnowledgeError(f"{filename}: unsupported file type. Text, Markdown, HTML, JSON, YAML, CSV and XML are added directly; "
                             "PDF, DOC and DOCX are converted to Markdown first.")
    text = decode(raw)
    if "\x00" in text[:4096]:
        raise KnowledgeError(f"{filename}: not a text file")
    if suffix in (".html", ".htm"):
        p = _Html()
        p.feed(text)
        return re.sub(r"\n{3,}", "\n\n", html.unescape("".join(p.out))).strip()
    if suffix == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
        return f"```json\n{text.strip()}\n```"
    if suffix in (".yaml", ".yml"):
        return f"```yaml\n{text.strip()}\n```"
    if suffix in (".csv", ".tsv", ".xml"):
        return f"```{suffix[1:]}\n{text.strip()}\n```"
    return text.strip()


FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def split_front_matter(md: str) -> tuple[dict[str, str], str]:
    m = FRONT.match(md)
    if not m:
        return {}, md
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            meta[k.strip().lower()] = v.strip().strip("\"'")
    return meta, md[m.end():]


def guess_title(md: str, filename: str) -> str:
    meta, body = split_front_matter(md)
    if meta.get("title"):
        return meta["title"]
    m = re.search(r"^#\s+(.+)$", body, re.M)
    return m.group(1).strip() if m else Path(filename).stem


# ---------------------------------------------------------------------------------------------------------- chunking
def _blocks(body: str):
    """(kind, text) blocks: heading, code (a whole fence), table (consecutive pipe rows), para."""
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
        elif ln.lstrip().startswith(("```", "~~~")):
            fence, j = ln.lstrip()[:3], i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(fence):
                j += 1
            yield "code", "\n".join(lines[i:j + 1])
            i = j + 1
        elif re.match(r"^#{1,6}\s", ln):
            yield "heading", ln
            i += 1
        elif ln.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            yield "table", "\n".join(lines[i:j])
            i = j
        else:
            j = i
            while j < len(lines) and lines[j].strip() and not re.match(r"^#{1,6}\s", lines[j]) and not lines[j].lstrip().startswith(("```", "|")):
                j += 1
            yield "para", "\n".join(lines[i:j])
            i = j


def _split_big(kind: str, text: str, limit: int) -> list[str]:
    """A block longer than limit, cut on line boundaries; a table keeps its header rows on every piece."""
    if len(text) <= limit:
        return [text]
    lines = text.splitlines()
    head = lines[:2] if kind == "table" else []
    out, cur = [], list(head)
    for ln in lines[len(head):]:
        if sum(len(x) + 1 for x in cur) + len(ln) > limit and len(cur) > len(head):
            out.append("\n".join(cur))
            cur = list(head)
        while len(ln) > limit:           # one enormous line: hard cut
            out.append("\n".join(cur + [ln[:limit]]))
            cur, ln = list(head), ln[limit:]
        cur.append(ln)
    if len(cur) > len(head):
        out.append("\n".join(cur))
    return out


def chunk_markdown(md: str, limit: int = CHUNK_CHARS) -> list[tuple[str, str]]:
    """[(heading path, text)] in document order: each chunk sits under one section and is about `limit` characters."""
    _, body = split_front_matter(md)
    path: list[tuple[int, str]] = []
    chunks: list[tuple[str, str]] = []
    cur: list[str] = []
    cur_head = ""

    def flush():
        nonlocal cur
        t = "\n\n".join(cur).strip()
        if t:
            chunks.append((cur_head, t))
        cur = []

    for kind, text in _blocks(body):
        if kind == "heading":
            flush()
            level = len(text) - len(text.lstrip("#"))
            title = text.lstrip("#").strip()
            path = [p for p in path if p[0] < level] + [(level, title)]
            cur_head = " › ".join(t for _, t in path)
            continue
        for piece in _split_big(kind, text, limit):
            if cur and sum(len(x) for x in cur) + len(piece) > limit:
                flush()
            cur.append(piece)
    flush()
    return chunks


# ---------------------------------------------------------------------------------------------------------- storing
def _index(conn: sqlite3.Connection, doc_id: int, title: str, md: str) -> int:
    n = 0
    for ord_, (heading, text) in enumerate(chunk_markdown(md)):
        cur = conn.execute("INSERT INTO kb_chunk(doc_id, ord, heading, text) VALUES(?,?,?,?)", (doc_id, ord_, heading, text))
        conn.execute("INSERT INTO kb_fts(heading, text, doc_id, chunk_id) VALUES(?,?,?,?)",
                     (f"{title} {heading}".strip(), text, doc_id, cur.lastrowid))
        n += 1
    return n


def add_markdown(conn: sqlite3.Connection, md: str, *, filename: str, source: str, user: str | None, job_id: int | None = None,
                 title: str | None = None) -> dict:
    """Add one document. A file already in the knowledge base (same text) is refused, not added twice."""
    md = md.strip()
    if not md:
        raise KnowledgeError(f"{filename}: the document has no text")
    sha = hashlib.sha256(md.encode()).hexdigest()
    dup = row(conn, "SELECT id, title FROM kb_doc WHERE sha256=? OR (job_id IS NOT NULL AND job_id=?)", (sha, job_id))
    if dup:
        raise KnowledgeError(f"{filename}: already in the knowledge base as “{dup['title']}”")
    title = title or guess_title(md, filename)
    cur = conn.execute("INSERT INTO kb_doc(title, filename, source, job_id, sha256, chars, path, added_by) VALUES(?,?,?,?,?,?,?,?)",
                       (title, filename, source, job_id, sha, len(md), "", user))
    doc_id = cur.lastrowid
    path = kb_dir() / f"{doc_id}.md"
    path.write_text(md, encoding="utf-8")
    n = _index(conn, doc_id, title, md)
    conn.execute("UPDATE kb_doc SET chunks=?, path=? WHERE id=?", (n, path.name, doc_id))
    conn.commit()
    return get(conn, doc_id)


def add_file(conn: sqlite3.Connection, filename: str, raw: bytes, user: str | None) -> dict:
    return add_markdown(conn, to_markdown(filename, raw), filename=filename, source="upload", user=user)


def remove(conn: sqlite3.Connection, doc_id: int) -> bool:
    d = row(conn, "SELECT path FROM kb_doc WHERE id=?", (doc_id,))
    if not d:
        return False
    conn.execute("DELETE FROM kb_fts WHERE doc_id=?", (doc_id,))
    conn.execute("DELETE FROM kb_doc WHERE id=?", (doc_id,))
    conn.commit()
    (kb_dir() / d["path"]).unlink(missing_ok=True)
    return True


def set_enabled(conn: sqlite3.Connection, doc_id: int, enabled: bool) -> dict | None:
    conn.execute("UPDATE kb_doc SET enabled=? WHERE id=?", (1 if enabled else 0, doc_id))
    conn.commit()
    return get(conn, doc_id)


def get(conn: sqlite3.Connection, doc_id: int) -> dict | None:
    return row(conn, "SELECT id, title, filename, source, job_id, chars, chunks, added_by, enabled, created FROM kb_doc WHERE id=?", (doc_id,))


def listing(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn, "SELECT id, title, filename, source, job_id, chars, chunks, added_by, enabled, created FROM kb_doc ORDER BY id DESC")


def text(conn: sqlite3.Connection, doc_id: int) -> str | None:
    d = row(conn, "SELECT path FROM kb_doc WHERE id=?", (doc_id,))
    f = kb_dir() / d["path"] if d else None
    return f.read_text(encoding="utf-8") if f and f.is_file() else None


def added_jobs(conn: sqlite3.Connection) -> dict[int, int]:
    """{conversion job id: knowledge base document id} of every conversion already added."""
    return {r["job_id"]: r["id"] for r in rows(conn, "SELECT id, job_id FROM kb_doc WHERE job_id IS NOT NULL")}


# ---------------------------------------------------------------------------------------------------------- searching
def terms(q: str) -> list[str]:
    out, seen = [], set()
    for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*[A-Za-z0-9_]|[A-Za-z0-9_]", q):
        for part in {w, *re.split(r"[.\-]", w)} if re.search(r"[.\-]", w) else {w}:
            k = part.lower()
            if k and k not in seen and k not in STOP and (len(k) > 1 or k.isdigit()):
                seen.add(k)
                out.append(part)
    return out[:24]


def search(conn: sqlite3.Connection, query: str, limit: int = 6) -> list[dict]:
    """The best chunks for a question, best first: {id, doc_id, title, heading, text, score}."""
    ts = terms(query)
    if not ts:
        return []
    match = " OR ".join('"' + t.replace('"', "") + '"' for t in ts)
    try:
        found = rows(conn, "SELECT chunk_id, doc_id, bm25(kb_fts, 4.0, 1.0) AS score FROM kb_fts WHERE kb_fts MATCH ? "
                           "ORDER BY score LIMIT ?", (match, limit * 4))
    except sqlite3.OperationalError:
        return []
    out = []
    for f in found:
        r = row(conn, "SELECT c.id, c.doc_id, d.title, c.heading, c.text FROM kb_chunk c JOIN kb_doc d ON d.id=c.doc_id "
                      "WHERE c.id=? AND d.enabled=1", (f["chunk_id"],))
        if r:
            out.append({**r, "score": round(-f["score"], 3)})
        if len(out) >= limit:
            break
    return out
