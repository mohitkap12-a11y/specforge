"""Structured activity log: what jobs, the parser and the AI agents are doing, stored in SQLite (table event_log).

`emit()` never blocks the caller and never touches the caller's database connection. Events go on a queue and a single
writer thread inserts them with its own connection, so logging cannot commit (or wait on) a job's open transaction.
Levels: debug < info < warn < error.  Sources name the subsystem: job, parser, catalog, scenarios, agent, llm, import,
coverage, api, system.
"""
from __future__ import annotations

import contextvars
import json
import queue
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .core.config import db_path
from .core.db import connect

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
SOURCES = ("job", "parser", "catalog", "scenarios", "agent", "llm", "import", "coverage", "api", "system")
MAX_QUEUE = 20000          # beyond this the oldest queued events are dropped rather than growing memory without bound
KEEP_ROWS = 100_000        # retention: the newest rows kept in the table
PRUNE_EVERY = 2000         # inserts into one database between retention passes
MAX_DATA = 60_000         # characters of JSON detail kept per event
FLUSH_EVERY = 0.15         # seconds between writer passes while events are queued

_job: contextvars.ContextVar[tuple[str, int] | None] = contextvars.ContextVar("sf_log_job", default=None)
_q: queue.Queue = queue.Queue()
_lock = threading.Lock()
_writer: threading.Thread | None = None
_enq = 0                  # events accepted by emit()
_done = 0                 # events written, or given up on (dropped / database gone)
_cfg = {"min_level": "info", "capture_prompts": True, "preview_chars": 4000}


def configure(settings: dict | None) -> None:
    """Apply the `logging` section of the settings (called at startup, on settings save and when a job starts)."""
    s = settings.get("logging") if isinstance(settings, dict) else None
    s = s if isinstance(s, dict) else {}            # never raises: a bad stored value must not stop the app from starting
    lvl = s.get("min_level", "info")
    _cfg["min_level"] = lvl if isinstance(lvl, str) and lvl in LEVELS else "info"
    _cfg["capture_prompts"] = bool(s.get("capture_prompts", True))
    try:
        _cfg["preview_chars"] = max(200, min(int(s.get("preview_chars", 4000)), 50_000))
    except (TypeError, ValueError, OverflowError):
        _cfg["preview_chars"] = 4000


def capture_prompts() -> bool:
    return bool(_cfg["capture_prompts"])


def clip(text: Any, n: int | None = None) -> str:
    """Truncate long text for storage, saying how much was cut."""
    s = text if isinstance(text, str) else str(text)
    n = n or int(_cfg["preview_chars"])
    return s if len(s) <= n else s[:n] + f"… [+{len(s) - n} chars]"


def bind_job(db: str, job_id: int):
    """Tag every event emitted on this thread (and its context) with a job id and database; returns a reset token."""
    return _job.set((db, job_id))


def unbind_job(token) -> None:
    _job.reset(token)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}Z"


def emit(level: str, source: str, message: str, *, job_id: int | None = None, db: str | None = None, **data: Any) -> None:
    """Record an event. Cheap and non-blocking; safe to call from any thread. Never raises: logging must not abort the work it describes."""
    try:
        _emit(level, source, message, job_id, db, data)
    except Exception:  # noqa: BLE001
        pass


def _emit(level: str, source: str, message: str, job_id: int | None, db: str | None, data: dict) -> None:
    if LEVELS.get(level, 20) < LEVELS[_cfg["min_level"]]:
        return
    ctx = _job.get()
    if ctx is not None:
        db = db or ctx[0]
        job_id = job_id if job_id is not None else ctx[1]
    try:
        db = db or str(db_path())
    except Exception:  # noqa: BLE001  logging must never break the work it describes
        return
    try:
        blob = json.dumps(data, default=str) if data else None
    except Exception:  # noqa: BLE001  unserialisable details (tuple keys, cycles...): keep the event, describe the details as text
        blob = json.dumps({"_unserializable": repr(data)[:MAX_DATA]})
    if blob is not None and len(blob) > MAX_DATA:       # e.g. a job result carrying hundreds of issues
        blob = json.dumps({"_truncated": blob[:MAX_DATA]})
    ev = (db, _now(), level if level in LEVELS else "info", source, job_id, str(message)[:2000], blob)
    global _enq, _done
    with _lock:
        _enq += 1
    if _q.qsize() >= MAX_QUEUE:
        try:
            _q.get_nowait()
            with _lock:
                _done += 1
        except queue.Empty:
            pass
    _q.put(ev)
    _ensure_writer()


def _ensure_writer() -> None:
    global _writer
    with _lock:
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(target=_run, name="sf-eventlog", daemon=True)
            _writer.start()


def _write(pending: list[tuple], conns: dict[str, sqlite3.Connection], inserted: dict[str, int]) -> tuple[list[tuple], int]:
    """Write one batch. Returns (events to retry, events finished: written or given up on). Pure of threads, so it can be tested directly."""
    by_db: dict[str, list[tuple]] = {}
    for ev in pending:
        by_db.setdefault(ev[0], []).append(ev)
    retry: list[tuple] = []
    finished = 0
    for db, evs in by_db.items():
        try:
            c = conns.get(db) or conns.setdefault(db, connect(db))
            c.executemany("INSERT INTO event_log(ts,level,source,job_id,message,data_json) VALUES(?,?,?,?,?,?)", [e[1:7] for e in evs])
            c.commit()
            finished += len(evs)                       # committed: from here on these events are never retried or recounted
            inserted[db] = inserted.get(db, 0) + len(evs)
            if inserted[db] >= PRUNE_EVERY:
                try:                                    # retention is best effort and separate from the inserts above
                    c.execute("DELETE FROM event_log WHERE id <= (SELECT MAX(id) FROM event_log) - ?", (KEEP_ROWS,))
                    c.commit()
                    inserted[db] = 0
                except sqlite3.Error:
                    c.rollback()                        # try again after the next batch
        except sqlite3.OperationalError as e:
            conns.pop(db, None)
            busy = "locked" in str(e) or "busy" in str(e)
            keep = [(*e_[:7], e_[7] + 1) for e_ in evs if busy and e_[7] < 100]     # a busy database is retried; a missing one is not
            retry += keep
            finished += len(evs) - len(keep)
        except Exception:  # noqa: BLE001
            conns.pop(db, None)
            finished += len(evs)
    return retry, finished


def _run() -> None:
    global _done
    conns: dict[str, sqlite3.Connection] = {}
    pending: list[tuple] = []          # (db, ts, level, source, job_id, message, data_json, attempts)
    inserted: dict[str, int] = {}      # per database: each one is pruned on its own schedule
    while True:
        if not pending:
            try:
                pending.append((*_q.get(timeout=1.0), 0))
            except queue.Empty:
                continue
            time.sleep(FLUSH_EVERY / 3)             # let a burst accumulate so it is written in one transaction
        while len(pending) < 500:
            try:
                pending.append((*_q.get_nowait(), 0))
            except queue.Empty:
                break
        retry, finished = _write(pending, conns, inserted)
        with _lock:
            _done += finished
        pending = retry[-MAX_QUEUE:]
        if retry:
            time.sleep(0.3)


def drain(timeout: float = 5.0) -> bool:
    """Wait until every accepted event has been written or given up on (tests, shutdown)."""
    end = time.time() + timeout
    while time.time() < end:
        with _lock:
            if _done >= _enq:
                return True
        time.sleep(0.02)
    return False


def row_to_event(r: sqlite3.Row | dict) -> dict:
    d = dict(r)
    raw = d.pop("data_json", None)
    d["data"] = json.loads(raw) if raw else {}
    return d


def query(conn: sqlite3.Connection, *, after_id: int | None = None, before_id: int | None = None, limit: int = 300,
          level: str | None = None, sources: list[str] | None = None, job_id: int | None = None, q: str | None = None) -> list[dict]:
    """Events oldest-first. With after_id: the next `limit` events after it. Otherwise the newest `limit` (optionally before before_id)."""
    sql, args = "SELECT id,ts,level,source,job_id,message,data_json FROM event_log WHERE 1=1", []
    if after_id is not None:
        sql += " AND id>?"; args.append(after_id)
    if before_id is not None:
        sql += " AND id<?"; args.append(before_id)
    if level in LEVELS:
        names = [k for k, v in LEVELS.items() if v >= LEVELS[level]]
        sql += f" AND level IN ({','.join('?' * len(names))})"; args += names
    if sources:
        sql += f" AND source IN ({','.join('?' * len(sources))})"; args += sources
    if job_id is not None:
        sql += " AND job_id=?"; args.append(job_id)
    if q:
        sql += " AND (message LIKE ? OR data_json LIKE ?)"; args += [f"%{q}%"] * 2
    limit = max(1, min(limit, 2000))
    if after_id is not None:
        out = [row_to_event(r) for r in conn.execute(sql + " ORDER BY id ASC LIMIT ?", [*args, limit]).fetchall()]
    else:
        out = [row_to_event(r) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit]).fetchall()][::-1]
    return out
