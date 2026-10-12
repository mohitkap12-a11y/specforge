"""Regression tests for the tenth round of Macroscope findings (review of 821ae7a)."""
import json
import sqlite3

from fastapi.testclient import TestClient
from test_macroscope_round2 import _scn, _step

from specforge import cmddb, coverage
from specforge.core.db import connect
from specforge.devtools.synth import sqe


def _manual(conn):
    conn.execute("INSERT INTO scenario(set_id,sid,origin,title,observability,predicate_json,state) VALUES(1,'M:p','manual','orig','submission',?, 'approved')",
                 (json.dumps({"scope": "global", "order": "strict", "steps": [{"command": "cmd:admin:06", "interface": "pcie", "queue": "admin",
                                                                               "cs": None, "opcode": 6, "fields": []}]}),))
    sid = conn.execute("SELECT id FROM scenario WHERE sid='M:p'").fetchone()[0]
    conn.execute("INSERT INTO scenario_item VALUES(?, 'cmd:admin:06')", (sid,))
    conn.commit()
    return sid


def test_a_failed_patch_leaves_nothing_pending(ws, conn, parsed_set):
    from specforge.api.app import create_app

    sid = _manual(conn)
    edited = {"scope": "global", "order": "strict", "steps": [{"command": "cmd:admin:06", "interface": "pcie", "queue": "admin", "cs": None,
                                                              "opcode": 6, "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 1}]}]}
    with TestClient(create_app()) as client:
        for bad in ({"predicate": edited, "title": {}}, {"predicate": edited, "state": "bogus"}, {"predicate": edited, "observability": 5},
                    {"predicate": edited, "state": ["approved"]}):
            assert client.patch(f"/api/scenarios/{sid}", json=bad).status_code == 422, bad
        for i in range(25):                                                      # later requests on the same worker threads commit
            assert client.patch(f"/api/scenarios/{sid}", json={"description": f"d{i}"}).status_code == 200
    row = connect().execute("SELECT predicate_json, state, title FROM scenario WHERE id=?", (sid,)).fetchone()
    assert json.loads(row[0])["steps"][0]["fields"] == [] and row[2] == "orig"           # the rejected edit never reached the database
    assert row[1] == "approved"


def test_bulk_validates_before_writing(ws, conn, parsed_set):
    from specforge.api.app import create_app

    sid = _manual(conn)
    with TestClient(create_app()) as client:
        assert client.post("/api/scenarios/bulk", json={"ids": [sid, "x"], "state": "rejected"}).status_code == 422
        assert client.post("/api/scenarios/bulk", json={"ids": [sid], "state": ["rejected"]}).status_code == 422
        assert client.post("/api/scenarios/bulk", json={"ids": [sid], "state": "nope"}).status_code == 422
        assert connect().execute("SELECT state FROM scenario WHERE id=?", (sid,)).fetchone()[0] == "approved"
        assert client.post("/api/scenarios/bulk", json={"ids": [sid], "state": "rejected"}).json()["updated"] == [sid]


def test_equal_test_and_seq_keep_command_order_across_signature_chunks(conn, parsed_set, tmp_path):
    n = 600                                                                        # 1200 distinct signatures -> several 500-signature chunks
    con = sqlite3.connect(tmp_path / "big.db")
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, interface TEXT, queue TEXT, sqe BLOB)")
    for i in range(n):
        con.execute("INSERT INTO commands VALUES(?,?,?,?)", (i + 1, "pcie", "admin", sqe(0x09, cid=i, cdw10=7, cdw11=i)))          # Set Features FID 7
    for i in range(n):
        con.execute("INSERT INTO commands VALUES(?,?,?,?)", (n + i + 1, "pcie", "admin", sqe(0x0A, cid=i, cdw10=7, cdw11=i)))      # Get Features FID 7
    con.commit(); con.close()
    mapping = {"table": "commands", "id": "id", "test": {"const": "t"}, "seq": {"const": 1}, "interface": "interface", "queue": "queue",
               "sqe": "sqe", "cs": {"const": "nvm"}}
    src = cmddb.register_source(conn, str(tmp_path / "big.db"), mapping); assert cmddb.import_source(conn, src)["decoded"] == 2 * n
    s = _scn(conn, 101, [_step(0x09, 7), _step(0x0A, 7)])                           # every Set precedes every Get in command order
    run = coverage.run_coverage(conn, 1, src)
    got = conn.execute("SELECT state, match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, s)).fetchone()
    assert tuple(got) == ("matched", n)                                              # n disjoint Set->Get pairs; chunk order must not reorder them
