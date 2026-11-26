"""The chatbot: per-user sessions, retrieval from the knowledge base, a streamed answer from a locally hosted LLM and
follow-up question suggestions.

One request to the model does it all: the reply is streamed to the user, and the model ends it with a marker line followed by
three follow-up questions, which are held back from the stream. A model that skips the marker gets one short extra request.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any, Iterator

from . import eventlog, knowledge
from .core.db import jloads, row, rows
from .llm.base import LLMError

MARK = "<<FOLLOWUPS>>"
MAX_USER_CHARS = 8000
HISTORY_CHARS = 6000
CONTEXT_CHARS = 1600        # per excerpt shown to the model

SYSTEM = """You are the SpecForge assistant: an expert, concise technical assistant for engineers working with specifications and project documents (storage, PCIe, NVMe, MCTP and similar).

Rules:
- Answer from the numbered CONTEXT excerpts of the knowledge base. Cite the excerpts you used inline as [1], [2] (the number of the excerpt).
- Quote exact names, opcodes, field names, bit positions and values from the context; never invent them.
{strict}
- Format with Markdown. Put every code, JSON, YAML, HTML, shell or register-layout example in a fenced block with its language (```json, ```html, ```c, ```python ...). Use tables for fields and registers. Be brief: lead with the answer.
- When asked for code, write complete, runnable code and keep prose short.
{extra}
After the answer, on a new line, write {mark} and then exactly 3 short follow-up questions the user could ask next about this topic, one per line, without numbering or bullets."""

STRICT = "- If the CONTEXT does not contain the answer, say so plainly in one sentence and name what to look for or upload; do not guess. Never present anything that is not in the context as a fact from the documents."
LOOSE = "- If the CONTEXT does not contain the answer, you may answer from general knowledge, but say clearly that it is not from the knowledge base."


class StreamParser:
    """Filters a model's stream: drops <think>…</think> blocks and splits off everything after the follow-up marker."""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.in_think = False
        self.tail = False
        self.followups = ""

    def feed(self, s: str) -> str:
        if self.tail:
            self.followups += s
            return ""
        self.buf += s
        out = ""
        while True:
            if self.in_think:
                i = self.buf.find(self.CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(self.CLOSE) - 1):]
                    return out
                self.buf = self.buf[i + len(self.CLOSE):].lstrip("\n")
                self.in_think = False
                continue
            hits = [(i, t) for t in (self.OPEN, MARK) if (i := self.buf.find(t)) >= 0]
            if hits:
                i, t = min(hits)
                out += self.buf[:i]
                self.buf = self.buf[i + len(t):]
                if t == self.OPEN:
                    self.in_think = True
                    continue
                self.tail, self.followups, self.buf = True, self.buf, ""
                return out
            keep = max((k for t in (self.OPEN, MARK) for k in range(min(len(t) - 1, len(self.buf)), 0, -1) if self.buf.endswith(t[:k])), default=0)
            out += self.buf[:len(self.buf) - keep]
            self.buf = self.buf[len(self.buf) - keep:]
            return out

    def finish(self) -> str:
        r = "" if (self.tail or self.in_think) else self.buf
        self.buf = ""
        return r


def parse_followups(raw: str) -> list[str]:
    out = []
    for ln in raw.splitlines():
        q = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln).strip().strip("\"'*_ ")
        if len(q) >= 8 and q not in out:
            out.append(q[:160])
    return out[:3]


# ---------------------------------------------------------------------------------------------------------- sessions
def _session(c: sqlite3.Connection, sid: int, user: str) -> dict | None:
    return row(c, "SELECT * FROM chat_session WHERE id=? AND user=?", (sid, user))


def list_sessions(c, user: str, q: str = "") -> list[dict]:
    sql = ("SELECT s.id, s.title, s.pinned, s.created, s.updated, (SELECT COUNT(*) FROM chat_message m WHERE m.session_id=s.id) AS messages "
           "FROM chat_session s WHERE s.user=?")
    args: list[Any] = [user]
    if q.strip():
        sql += " AND (s.title LIKE ? OR EXISTS (SELECT 1 FROM chat_message m WHERE m.session_id=s.id AND m.content LIKE ?))"
        args += [f"%{q.strip()}%"] * 2
    return rows(c, sql + " ORDER BY s.pinned DESC, s.updated DESC, s.id DESC", args)


def create_session(c, user: str, title: str = "New chat") -> dict:
    cur = c.execute("INSERT INTO chat_session(user, title) VALUES(?,?)", (user, title[:120] or "New chat"))
    c.commit()
    return _session(c, cur.lastrowid, user)  # type: ignore[arg-type,return-value]


def messages(c, sid: int) -> list[dict]:
    out = rows(c, "SELECT id, role, content, sources_json, suggestions_json, stats_json, feedback, created FROM chat_message WHERE session_id=? ORDER BY id", (sid,))
    for m in out:
        m["sources"] = jloads(m.pop("sources_json"), [])
        m["suggestions"] = jloads(m.pop("suggestions_json"), [])
        m["stats"] = jloads(m.pop("stats_json"), None)
    return out


def get_session(c, sid: int, user: str) -> dict | None:
    s = _session(c, sid, user)
    return {**s, "messages": messages(c, sid)} if s else None


def update_session(c, sid: int, user: str, title: str | None = None, pinned: bool | None = None) -> dict | None:
    if not _session(c, sid, user):
        return None
    if title is not None and title.strip():
        c.execute("UPDATE chat_session SET title=? WHERE id=?", (title.strip()[:120], sid))
    if pinned is not None:
        c.execute("UPDATE chat_session SET pinned=? WHERE id=?", (1 if pinned else 0, sid))
    c.commit()
    return _session(c, sid, user)


def delete_session(c, sid: int, user: str) -> bool:
    n = c.execute("DELETE FROM chat_session WHERE id=? AND user=?", (sid, user)).rowcount
    c.commit()
    return bool(n)


def delete_all(c, user: str) -> int:
    n = c.execute("DELETE FROM chat_session WHERE user=?", (user,)).rowcount
    c.commit()
    return n


def set_feedback(c, mid: int, user: str, value: int | None) -> bool:
    n = c.execute("UPDATE chat_message SET feedback=? WHERE id=? AND role='assistant' AND session_id IN (SELECT id FROM chat_session WHERE user=?)",
                  (value, mid, user)).rowcount
    c.commit()
    return bool(n)


def export_markdown(c, sid: int, user: str) -> tuple[str, str] | None:
    s = get_session(c, sid, user)
    if not s:
        return None
    parts = [f"# {s['title']}\n"]
    for m in s["messages"]:
        parts.append(f"## {'You' if m['role'] == 'user' else 'Assistant'}\n\n{m['content']}\n")
    return s["title"], "\n".join(parts)


def _title_from(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip().strip("#>*`- ")
    return (t[:57] + "…") if len(t) > 58 else t or "New chat"


# ---------------------------------------------------------------------------------------------------------- one turn
def build_messages(question: str, history: list[dict], chunks: list[dict], chat_cfg: dict) -> list[dict]:
    system = SYSTEM.format(strict=STRICT if chat_cfg.get("strict", True) else LOOSE, mark=MARK,
                           extra=("- " + chat_cfg["system_prompt"].strip()) if (chat_cfg.get("system_prompt") or "").strip() else "")
    if chunks:
        ctx = "\n\n".join(f"[{i}] {c['title']}" + (f" › {c['heading']}" if c["heading"] else "") + "\n" + c["text"][:CONTEXT_CHARS]
                          for i, c in enumerate(chunks, 1))
    else:
        ctx = "(no excerpt of the knowledge base matched this question)"
    hist, used = [], 0
    for m in reversed(history[-(chat_cfg.get("history_messages") or 8):]):
        used += len(m["content"])
        if used > HISTORY_CHARS and hist:
            break
        hist.append({"role": m["role"], "content": m["content"]})
    hist.reverse()
    return [{"role": "system", "content": system}, *hist,
            {"role": "user", "content": f"CONTEXT:\n{ctx}\n\nQUESTION:\n{question}"}]


def _retrieval_query(question: str, history: list[dict]) -> str:
    """A short follow-up ("and its fields?") is searched together with the question before it."""
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
    return f"{prev[:300]} {question}" if prev and len(knowledge.terms(question)) < 4 else question


def _fallback_suggestions(prov, model: str, question: str, answer: str) -> list[str]:
    msgs = [{"role": "system", "content": "Write exactly 3 short follow-up questions a user could ask next. One per line, no numbering, no other text."},
            {"role": "user", "content": f"Question: {question}\n\nAnswer: {answer[:1500]}"}]
    return parse_followups(prov.generate(msgs, model=model, temperature=0.4, max_tokens=120))


def sse(ev: dict) -> bytes:
    return (json.dumps(ev, ensure_ascii=False) + "\n").encode()


def _passage(body: str, terms: list[str], size: int = CONTEXT_CHARS) -> str:
    """At most `size` characters of a long section or table, starting just before the first place a query word occurs."""
    if len(body) <= size:
        return body
    low = body.lower()
    at = min((i for t in terms if (i := low.find(t.lower())) >= 0), default=0)
    start = max(0, at - size // 4)
    return ("… " if start else "") + body[start:start + size]


def spec_hits(speclib, query: str, limit: int) -> list[dict]:
    """Passages of the parsed specs (latest version of each; the Knowledge base's "Parsed specs" tab), as the chat reads them."""
    ts = knowledge.terms(query)
    if not ts or limit < 1:
        return []
    try:
        docs, _ = speclib.docs_for(None, None)
    except Exception:  # noqa: BLE001  nothing parsed yet
        return []
    by_run = {d["run_id"]: d for d in docs}
    for d in docs:
        speclib.ensure_index(d)
    match = " OR ".join('"' + t.replace('"', "").lower() + '"' for t in ts)
    ph = ",".join("?" * len(by_run))
    try:
        found = rows(speclib.conn(), f"SELECT number, title, body, run_id, kind, page, bm25(mcp_fts, 4.0, 8.0, 1.0) AS score FROM mcp_fts "
                                     f"WHERE mcp_fts MATCH ? AND run_id IN ({ph}) AND kind IN ('section','table') ORDER BY score LIMIT ?",
                     [match, *by_run, limit])
    except sqlite3.OperationalError:
        return []
    out = []
    for h in found:
        d = by_run[h["run_id"]]
        head = (f"Figure {h['number']}" if h["kind"] == "table" else f"§{h['number']}") + (f" {h['title']}" if h["title"] else "")
        out.append({"doc_id": None, "title": f"{d['family']} {d['version']}", "heading": head + (f" (p. {h['page']})" if h.get("page") else ""),
                    "text": _passage(h["body"], ts)})
    return out


def run_turn(conn_fn, user: str, sid: int, text: str, cfg: dict, provider, *, regenerate: bool = False, extra_search=None) -> Iterator[dict]:
    """Events: {type: start, user_message, sources} · {type: delta, text} · {type: done, message} · {type: suggestions, message_id, items}
    · {type: error, message}. `conn_fn()` returns this thread's database connection (the generator hops between threads)."""
    chat = cfg["chat"]
    c = conn_fn()
    if not _session(c, sid, user):
        yield {"type": "error", "message": "chat not found"}
        return
    if regenerate:      # answer the last question again: drop the last answer, re-use the last question
        last = row(c, "SELECT id, role FROM chat_message WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,))
        if last and last["role"] == "assistant":
            c.execute("DELETE FROM chat_message WHERE id=?", (last["id"],))
        q = row(c, "SELECT id, content FROM chat_message WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1", (sid,))
        if not q:
            yield {"type": "error", "message": "nothing to regenerate"}
            return
        text = q["content"]
        c.execute("DELETE FROM chat_message WHERE id=?", (q["id"],))
    text = text.strip()[:MAX_USER_CHARS]
    if not text:
        yield {"type": "error", "message": "empty message"}
        return
    history = messages(c, sid)
    first = not history
    cur = c.execute("INSERT INTO chat_message(session_id, role, content) VALUES(?,?,?)", (sid, "user", text))
    umid = cur.lastrowid
    if first:
        c.execute("UPDATE chat_session SET title=? WHERE id=?", (_title_from(text), sid))
    c.commit()
    model = chat.get("model") or ""
    t0 = time.monotonic()
    rq, k = _retrieval_query(text, history), int(chat.get("top_k") or 6)
    chunks = knowledge.search(c, rq, k)
    if extra_search:        # parsed specs: the best passages of both sources, interleaved
        try:
            more = extra_search(rq, k)
        except Exception as e:  # noqa: BLE001  a broken index must not stop the chat
            eventlog.emit("warn", "chat", f"parsed-spec search failed: {e}")
            more = []
        mixed = [x for pair in zip(chunks + [None] * k, more + [None] * k) for x in pair if x]
        chunks = mixed[:k + 2]
    sources = [{"n": i, "doc_id": ch["doc_id"], "title": ch["title"], "heading": ch["heading"], "text": ch["text"][:CONTEXT_CHARS]}
               for i, ch in enumerate(chunks, 1)]
    yield {"type": "start", "session_id": sid, "user_message": {"id": umid, "role": "user", "content": text},
           "sources": sources, "title": row(c, "SELECT title FROM chat_session WHERE id=?", (sid,))["title"]}  # type: ignore[index]
    parser, answer, err, stats = StreamParser(), [], None, {}
    prov = provider
    try:
        if not model:
            raise LLMError("No chat model is set. An admin can choose one in Settings → Chatbot.")
        msgs = build_messages(text, history, chunks, chat)
        eventlog.emit("info", "chat", f"{user}: question ({len(chunks)} excerpts, {sum(len(m['content']) for m in msgs)} chars) to {prov.name}/{model}")
        if hasattr(prov, "stream"):
            pieces = prov.stream(msgs, model=model, temperature=float(chat.get("temperature", 0.2)),
                                 max_tokens=int(chat.get("max_tokens") or 1500), think=False if chat.get("disable_thinking", True) else None)
        else:      # e.g. the OpenCode CLI answers in one piece
            pieces = iter([prov.generate(msgs, model=model, temperature=float(chat.get("temperature", 0.2)))])
        for piece in pieces:
            if vis := parser.feed(piece):
                answer.append(vis)
                yield {"type": "delta", "text": vis}
        if tail := parser.finish():
            answer.append(tail)
            yield {"type": "delta", "text": tail}
        stats = dict(getattr(prov, "last_stats", {}) or {})
    except LLMError as e:
        err = str(e)
    except GeneratorExit:        # the user pressed Stop (or left): keep what was written so far
        if "".join(answer).strip():
            _save(conn_fn(), sid, "".join(answer).rstrip(), sources, [], {"stopped": True, "model": model})
        raise
    full = "".join(answer).rstrip()
    if err and not full:
        eventlog.emit("error", "chat", f"{user}: chat failed: {err}")
        yield {"type": "error", "message": err}
        return
    stats.update(model=model, seconds=round(time.monotonic() - t0, 2), excerpts=len(chunks))
    if err:
        stats["interrupted"] = err
    if not stats.get("tps") and stats.get("tokens") is None:
        stats["tokens"] = max(1, len(full) // 4)
    c = conn_fn()
    sugg = parse_followups(parser.followups) if chat.get("suggestions", True) else []
    mid = _save(c, sid, full, sources, sugg, stats)
    if not mid:
        yield {"type": "error", "message": "this chat was deleted while the answer was being written"}
        return
    yield {"type": "done", "message": {"id": mid, "role": "assistant", "content": full, "sources": sources, "suggestions": sugg, "stats": stats, "feedback": None}}
    if chat.get("suggestions", True) and not sugg and full and not err:
        try:
            sugg = _fallback_suggestions(prov, model, text, full)
        except LLMError:
            sugg = []
        if sugg:
            conn_fn().execute("UPDATE chat_message SET suggestions_json=? WHERE id=?", (json.dumps(sugg), mid))
            conn_fn().commit()
            yield {"type": "suggestions", "message_id": mid, "items": sugg}


def _save(c, sid: int, content: str, sources: list, suggestions: list, stats: dict) -> int:
    """Store an answer; 0 when its chat was deleted meanwhile (ids are never reused, so it cannot land in someone else's chat)."""
    if not c.execute("SELECT 1 FROM chat_session WHERE id=?", (sid,)).fetchone():
        return 0
    cur = c.execute("INSERT INTO chat_message(session_id, role, content, sources_json, suggestions_json, stats_json) VALUES(?,?,?,?,?,?)",
                    (sid, "assistant", content, json.dumps(sources), json.dumps(suggestions), json.dumps(stats)))
    c.execute("UPDATE chat_session SET updated=CURRENT_TIMESTAMP WHERE id=?", (sid,))
    c.commit()
    return cur.lastrowid  # type: ignore[return-value]
