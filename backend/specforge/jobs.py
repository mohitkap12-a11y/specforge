"""Small persistent job runner: jobs are rows in SQLite; work runs on a thread pool."""
from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

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

    def __call__(self, message: str, done: int = 0, total: int = 0) -> None:
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        with _plock:
            _progress[self.key] = (message, done, total)


def submit(conn, type_: str, fn: Callable[..., Any], payload: dict | None = None, db_file: str | None = None) -> int:
    cur = conn.execute("INSERT INTO job(type,state,payload_json) VALUES(?, 'queued', ?)", (type_, json.dumps(payload or {})))
    conn.commit()
    job_id = cur.lastrowid
    db = db_file or db_file_of(conn)             # the worker must use the database the job row was written to
    key = (db, job_id)

    def run():
        c = connect(db)
        prog = Progress(job_id, c, db)
        try:
            if prog.cancel.is_set():            # cancelled while still queued: never start the work
                raise InterruptedError("cancelled before start")
            c.execute("UPDATE job SET state='running', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            c.commit()
            result = fn(c, prog)
            msg, done, total = _progress.get(key, (None, 0, 0))
            c.execute("UPDATE job SET state='done', result_json=?, message=COALESCE(?,message), progress=?, total=?, updated=CURRENT_TIMESTAMP WHERE id=?",
                      (json.dumps(result, default=str), msg, done, total, job_id))
        except InterruptedError:
            c.rollback()  # discard the aborted job's partial writes
            c.execute("UPDATE job SET state='cancelled', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        except Exception as e:  # noqa: BLE001
            c.rollback()
            c.execute("UPDATE job SET state='failed', error=?, updated=CURRENT_TIMESTAMP WHERE id=?", (f"{e!r}\n{traceback.format_exc()[-1500:]}", job_id))
        finally:
            c.commit()
            c.close()
            with _plock:
                _cancel.pop(key, None)
                _progress.pop(key, None)

    _pool.submit(run)
    return job_id  # type: ignore[return-value]


def cancel(job_id: int, db_file: str | None = None) -> None:
    from .core.config import db_path

    with _plock:
        _cancel.setdefault((str(db_file or db_path()), job_id), threading.Event()).set()


def _overlay(j: dict | None, conn) -> dict | None:
    if j and j["state"] == "running":
        with _plock:
            p = _progress.get((db_file_of(conn), j["id"]))
        if p:
            j["message"], j["progress"], j["total"] = p
    return j


def get(conn, job_id: int) -> dict | None:
    return _overlay(row(conn, "SELECT * FROM job WHERE id=?", (job_id,)), conn)


def recent(conn, limit: int = 50) -> list[dict]:
    return [_overlay(x, conn) for x in rows(conn, "SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,))]


def recover_interrupted(conn) -> int:
    cur = conn.execute("UPDATE job SET state='failed', error='interrupted by restart' WHERE state IN ('queued','running')")
    conn.commit()
    return cur.rowcount
