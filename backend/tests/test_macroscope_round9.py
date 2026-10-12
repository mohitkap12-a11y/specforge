"""Regression tests for the ninth round of Macroscope findings (review of 9b63e8d)."""
import sqlite3


from specforge import cmddb
from specforge.core.db import rows
from specforge.devtools.synth import sqe


def _db(path, n=3):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)")
    for i in range(1, n + 1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (100 + i, 10 * i, "pcie", "admin", sqe(6, cid=i, cdw10=i)))
    con.commit(); con.close()


BASE = {"table": "commands", "id": "id", "seq": "seq", "interface": "interface", "queue": "queue", "sqe": "sqe", "cs": {"const": "nvm"}}


def test_constant_bookkeeping_mappings_are_honoured(conn, tmp_path):
    _db(tmp_path / "c.db")
    sid = cmddb.register_source(conn, str(tmp_path / "c.db"), {**BASE, "test": {"const": "nightly"}, "script": {"const": "smoke.py"}})
    assert cmddb.import_source(conn, sid)["decoded"] == 3
    got = rows(conn, "SELECT src_row_id,test_id,script,seq FROM decoded_command WHERE source_id=? ORDER BY id", (sid,))
    assert [(g["src_row_id"], g["test_id"], g["script"], g["seq"]) for g in got] == [(101, "nightly", "smoke.py", 10), (102, "nightly", "smoke.py", 20),
                                                                                    (103, "nightly", "smoke.py", 30)]


def test_constant_sequence_and_id_do_not_break_ordering_or_error_reporting(conn, tmp_path):
    _db(tmp_path / "c.db", 4)
    con = sqlite3.connect(tmp_path / "c.db")
    con.execute("UPDATE commands SET sqe=x'00' WHERE id=103")                         # one undecodable row
    con.commit(); con.close()
    sid = cmddb.register_source(conn, str(tmp_path / "c.db"), {**BASE, "seq": {"const": 5}, "id": {"const": 7}, "test": "id"})
    r = cmddb.import_source(conn, sid)
    assert (r["decoded"], r["undecodable"]) == (3, 1) and r["errors"][0]["row"] == 7        # constant id is reported, not a KeyError
    assert {x["seq"] for x in rows(conn, "SELECT seq FROM decoded_command WHERE source_id=?", (sid,))} == {5}

