"""Regression test for the eleventh round of Macroscope findings (review of 821ae7a)."""
import sqlite3

import pytest

from specforge import cmddb
from specforge.devtools.synth import sqe


def _rows(path, n, bad=0):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)")
    for i in range(1, n + 1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)", (i, "t", i, "pcie", "admin", b"x" if i <= bad else sqe(6, cdw10=i)))
    con.commit(); con.close()


def test_import_honours_cancellation_for_small_and_all_bad_inputs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cmddb, "PROGRESS_EVERY", 100)
    _rows(tmp_path / "small.db", 5)                       # far fewer than 20,000 rows: the old code never polled
    sid = cmddb.register_source(conn, str(tmp_path / "small.db"))
    calls = []

    def cancel(n):
        calls.append(n)
        raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        cmddb.import_source(conn, sid, progress=cancel)
    conn.rollback()                                       # what the job runner does for a cancelled job
    assert calls == [5]                                   # polled once before finalising
    assert conn.execute("SELECT status FROM cmd_source WHERE id=?", (sid,)).fetchone()[0] == "new"
    assert conn.execute("SELECT COUNT(*) FROM cmd_signature WHERE source_id=?", (sid,)).fetchone()[0] == 0

    _rows(tmp_path / "bad.db", 250, bad=250)              # nothing decodes: still polled while scanning
    sid2 = cmddb.register_source(conn, str(tmp_path / "bad.db"), cmddb.suggest_mapping(str(tmp_path / "small.db"))["mapping"])
    seen = []
    cmddb.import_source(conn, sid2, progress=seen.append)
    assert seen == [100, 200, 250]
