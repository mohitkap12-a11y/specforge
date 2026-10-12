"""Small persistent job runner: jobs are rows in SQLite; work runs on a thread pool."""
from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .core.db import connect, row, rows

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sf-job")
_cancel: dict[int, threading.Event] = {}


class Progress:
    def __init__(self, job_id: int, conn):
        self.job_id, self.conn = job_id, conn
        self.cancel = _cancel.setdefault(job_id, threading.Event())

    def __call__(self, message: str, done: int = 0, total: int = 0) -> None:
        if self.cancel.is_set():
            raise InterruptedError("cancelled")
        self.conn.execute("UPDATE job SET message=?, progress=?, total=?, updated=CURRENT_TIMESTAMP WHERE id=?", (message, done, total, self.job_id))
        self.conn.commit()


def submit(conn, type_: str, fn: Callable[..., Any], payload: dict | None = None, db_file: str | None = None) -> int:
    cur = conn.execute("INSERT INTO job(type,state,payload_json) VALUES(?, 'queued', ?)", (type_, json.dumps(payload or {})))
    conn.commit()
    job_id = cur.lastrowid

    def run():
        c = connect(db_file)
        prog = Progress(job_id, c)
        try:
            c.execute("UPDATE job SET state='running', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
            c.commit()
            result = fn(c, prog)
            c.execute("UPDATE job SET state='done', result_json=?, updated=CURRENT_TIMESTAMP WHERE id=?", (json.dumps(result, default=str), job_id))
        except InterruptedError:
            c.execute("UPDATE job SET state='cancelled', updated=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        except Exception as e:  # noqa: BLE001
            c.execute("UPDATE job SET state='failed', error=?, updated=CURRENT_TIMESTAMP WHERE id=?", (f"{e!r}\n{traceback.format_exc()[-1500:]}", job_id))
        finally:
            c.commit()
            c.close()
            _cancel.pop(job_id, None)

    _pool.submit(run)
    return job_id  # type: ignore[return-value]


def cancel(job_id: int) -> None:
    _cancel.setdefault(job_id, threading.Event()).set()


def get(conn, job_id: int) -> dict | None:
    return row(conn, "SELECT * FROM job WHERE id=?", (job_id,))


def recent(conn, limit: int = 50) -> list[dict]:
    return rows(conn, "SELECT * FROM job ORDER BY id DESC LIMIT ?", (limit,))


def recover_interrupted(conn) -> int:
    cur = conn.execute("UPDATE job SET state='failed', error='interrupted by restart' WHERE state IN ('queued','running')")
    conn.commit()
    return cur.rowcount
