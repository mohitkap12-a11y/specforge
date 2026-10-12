"""Regression tests for the ninth round of Macroscope findings (review of 9b63e8d)."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from llm_helpers import Fake, settings

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


def test_limit_zero_means_none_and_negative_is_rejected(conn, parsed_set, ws):
    from specforge.ai import generate_ai_scenarios
    from specforge.llm.service import LLMService

    calls = []
    svc = LLMService(conn, settings(), Fake([lambda m: calls.append(1) or json.dumps({"testable": False})]))
    stats = generate_ai_scenarios(conn, 1, svc, None, limit=0)
    assert stats["requirements"] == 0 and calls == []                                     # nothing was sent to the model
    with pytest.raises(ValueError):
        generate_ai_scenarios(conn, 1, svc, None, limit=-1)
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        assert client.post("/api/sets/1/scenarios/ai", json={"limit": -1}).status_code == 422
        assert client.post("/api/sets/1/scenarios/ai", json={"limit": "x"}).status_code == 422
