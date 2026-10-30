"""Small persistent job runner: jobs are rows in SQLite; work runs on a thread pool."""
from __future__ import annotations

import json
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from . import eventlog
from .core.db import connect, row, rows

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sf-job")
_cancel: dict[tuple[str, int], threading.Event] = {}
_progress: dict[tuple[str, int], tuple[str, int, int]] = {}   # live progress is kept in memory: writing it to SQLite from the
_plock = threading.Lock()                          # worker's connection would commit that worker's open transaction


def db_file_of(conn) -> str:
    """Absolute path of the database a connection is attached to (job state is keyed by it, ids repeat across databases)."""
    for r in conn.execute("PRAGMA database_list"):
        if r[1] == "main":
            return r[2]
    raise RuntimeError("connection has no main database")


class Progress:
    def __init__(self, job_id: int, conn, db: str):
        self.job_id, self.conn, self.key = job_id, conn, (db, job_id)
        with _plock:
            self.cancel = _cancel.setdefault(self.key, threading.Event())
        self._stage: str | None = None
        self._beat = 0.0

    def __call__(self, message: str, done: int = 0, total: int = 0) -> None:
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        with _plock:
            _progress[self.key] = (message, done, total)
        stage = re.sub(r"\d+", "#", message)       # "extract 12/200" and "extract 13/200" are the same stage
        now = time.monotonic()
        if stage != self._stage:
            self._stage, self._beat = stage, now
            eventlog.emit("info", "job", message, done=done, total=total)
        elif now - self._beat >= 3:                 # heartbeat inside a long stage, not one line per tick
            self._beat = now
            eventlog.emit("debug", "job", message, done=done, total=total)


def submit(conn, type_: str, fn: Callable[..., Any], payload: dict | None = None, db_file: str | None = None,
           pool: ThreadPoolExecutor | None = None, user: str | None = None, label: str | None = None,
           queue: str | None = None) -> int:
    """Queue fn(conn, progress) as a job; `pool` runs it on a dedicated executor instead of the shared one. `queue` names
    that executor's first-come-first-served line, so a waiting job can say how many are ahead of it (see position)."""
    cur = conn.execute("INSERT INTO job(type,state,payload_json,user,label,queue) VALUES(?, 'queued', ?, ?, ?, ?)",
                       (type_, json.dumps(payload or {}), user, label, queue))
    conn.commit()
    job_id = cur.lastrowid
    db = db_file or db_file_of(conn)             # the worker must use the database the job row was written to
    eventlog.emit("info", "job", f"Queued {type_}", job_id=job_id, db=db, type=type_, payload=payload or {})
    schedule(job_id, type_, fn, db, pool)
    return job_id  # type: ignore[return-value]


def schedule(job_id: int, type_: str, fn: Callable[..., Any], db: str, pool: ThreadPoolExecutor | None = None) -> None:
    """Run an existing queued job row (a new one from submit, or one resumed after a restart)."""
    key = (db, job_id)

    def run():
        c = connect(db)
        prog = Progress(job_id, c, db)
        tok = eventlog.bind_job(db, job_id)
        t0 = time.monotonic()
        try:
            if prog.cancel.is_set():            # cancelled while still queued: never start the work
                raise InterruptedError("cancelled before start")
            c.execute("UPDATE job SET state='running', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            c.commit()
            eventlog.emit("info", "job", f"Started {type_}", type=type_)
            result = fn(c, prog)
            msg, done, total = _progress.get(key, (None, 0, 0))
            c.execute("UPDATE job SET state='done', result_json=?, message=COALESCE(?,message), progress=?, total=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                      (json.dumps(result, default=str), msg, done, total, job_id))
            eventlog.emit("info", "job", f"Finished {type_}", type=type_, seconds=round(time.monotonic() - t0, 2), result=result)
        except InterruptedError:
            c.rollback()  # discard the aborted job's partial writes
            c.execute("UPDATE job SET state='cancelled', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            eventlog.emit("warn", "job", f"Cancelled {type_}", type=type_, seconds=round(time.monotonic() - t0, 2))
        except Exception as e:  # noqa: BLE001
            c.rollback()
            tb = traceback.format_exc()
            c.execute("UPDATE job SET state='failed', error=?, updated=CURRENT_TIMESTAMP WHERE id=?", (f"{e!r}\n{tb[-1500:]}", job_id))
            eventlog.emit("error", "job", f"Failed {type_}: {e!r}", type=type_, seconds=round(time.monotonic() - t0, 2), traceback=tb[-3000:])
        finally:
            eventlog.unbind_job(tok)
            c.commit()
            c.close()
            with _plock:
                _cancel.pop(key, None)
                _progress.pop(key, None)

    (pool or _pool).submit(run)


def cancel(job_id: int, db_file: str | None = None) -> None:
    from .core.config import db_path

    db = str(db_file or db_path())
    c = connect(db)
    try:
        job = row(c, "SELECT state FROM job WHERE id=?", (job_id,))
    finally:
        c.close()
    if not job or job["state"] not in ("queued", "running"):
        return          # unknown or finished: a stale event would cancel whichever job later gets this id
    with _plock:
        _cancel.setdefault((db, job_id), threading.Event()).set()
    # A queued job may wait behind a long one: show it cancelled now. The event is set first, so a worker that starts the job
    # afterwards never runs it; one that was already starting may set 'running' again, and then stops at its first step.
    c = connect(db)
    try:
        c.execute("UPDATE job SET state='cancelled', updated=CURRENT_TIMESTAMP WHERE id=? AND state='queued'", (job_id,))
        c.commit()
    finally:
        c.close()


def cancel_requested(job_id: int, db_file: str) -> bool:
    """A cancel was asked for and the job has not stopped yet (a running step that cannot be interrupted)."""
    with _plock:
        e = _cancel.get((db_file, job_id))
    return bool(e and e.is_set())


def _overlay(j: dict | None, conn, lines: dict[str, list[dict]] | None = None) -> dict | None:
    """The job row with the live progress of a running job and, for a queued one, its place in line. `lines` (from
    _lines) lets a list of jobs share one query instead of one per queued job."""
    if j and j["state"] == "running":
        with _plock:
            p = _progress.get((db_file_of(conn), j["id"]))
        if p:
            j["message"], j["progress"], j["total"] = p
    if j:
        if lines is None:
            lines = _lines(conn, j["queue"]) if j["state"] == "queued" and j.get("queue") else {}
        j.update(_position(j, lines))
    return j


def _lines(conn, queue: str | None = None) -> dict[str, list[dict]]:
    """Queued and running jobs per queue, in the order their executor starts them (id order). A job whose cancel is
    pending has given up its place: it is skipped when it reaches the worker."""
    db = db_file_of(conn)
    sql = "SELECT id, state, label, type, user, queue FROM job WHERE state IN ('queued','running') AND queue IS NOT NULL"
    found = rows(conn, sql + (" AND queue=?" if queue else "") + " ORDER BY id", (queue,) if queue else ())
    with _plock:
        live = [j for j in found if j["state"] == "running" or not ((e := _cancel.get((db, j["id"]))) and e.is_set())]
    out: dict[str, list[dict]] = {}
    for j in live:
        out.setdefault(j["queue"], []).append(j)
    return out


def _position(j: dict, lines: dict[str, list[dict]]) -> dict:
    """Where a queued job stands in its line: {"ahead": jobs that run before it, "ahead_of": the one running now}."""
    if j.get("state") != "queued" or not j.get("queue"):
        return {"ahead": 0, "ahead_of": None}
    before = [b for b in lines.get(j["queue"], []) if b["id"] < j["id"]]
    run = next((b for b in before if b["state"] == "running"), None)
    return {"ahead": len(before), "ahead_of": {"label": run["label"] or run["type"], "user": run["user"]} if run else None}


def with_progress(j: dict | None, conn) -> dict | None:
    """A job row read elsewhere, with the live progress of a running job filled in."""
    return _overlay(j, conn)


def with_progress_all(found: list[dict], conn) -> list[dict]:
    """with_progress for a list of job rows, reading the queues once."""
    lines = _lines(conn) if any(j["state"] == "queued" and j.get("queue") for j in found) else {}
    return [_overlay(j, conn, lines) for j in found]


def get(conn, job_id: int) -> dict | None:
    return _overlay(row(conn, "SELECT * FROM job WHERE id=?", (job_id,)), conn)


def recent(conn, limit: int = 50) -> list[dict]:
    return with_progress_all(rows(conn, "SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,)), conn)


def active(conn) -> list[dict]:
    """Every queued or running job (not limited like recent())."""
    return with_progress_all(rows(conn, "SELECT * FROM job WHERE state IN ('queued','running') ORDER BY id DESC"), conn)


def recover_interrupted(conn, resumable: Iterable[str] = ()) -> tuple[int, list[dict]]:
    """After a restart: jobs of the `resumable` types that were queued or running go back to 'queued' and are returned
    oldest first, to be scheduled again in their original order (a job that was running starts over); every other
    unfinished job is marked failed. Returns (number failed, jobs to resume)."""
    kinds = list(resumable)
    marks = ",".join("?" * len(kinds))
    keep = f" AND type NOT IN ({marks})" if kinds else ""
    cur = conn.execute(f"UPDATE job SET state='failed', error='interrupted by restart' WHERE state IN ('queued','running'){keep}", kinds)
    again = rows(conn, f"SELECT * FROM job WHERE state IN ('queued','running') AND type IN ({marks}) ORDER BY id", kinds) if kinds else []
    if again:
        conn.execute(f"UPDATE job SET state='queued', updated=CURRENT_TIMESTAMP WHERE id IN ({','.join('?' * len(again))})",
                     [j["id"] for j in again])
    conn.commit()
    return cur.rowcount, [{**j, "state": "queued"} for j in again]
