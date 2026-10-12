"""Regression tests for the twelfth round of Macroscope findings (review of 2608583)."""
import sqlite3
import time

from fastapi.testclient import TestClient
from test_macroscope_round2 import _scn, _step

from specforge import cmddb, coverage
from specforge.core.db import connect
from specforge.devtools.synth import sqe


def test_disjoint_matches_scale_linearly_on_large_logs(conn, parsed_set, tmp_path):
    n = 15_000                                                    # 30,000 commands, 15,000 disjoint Set->Get pairs
    con = sqlite3.connect(tmp_path / "huge.db")
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, interface TEXT, queue TEXT, sqe BLOB)")
    con.executemany("INSERT INTO commands VALUES(?,?,?,?)", [(i + 1, "pcie", "admin", sqe(0x09, cid=i & 0xFFFF, cdw10=7, cdw11=i)) for i in range(n)])
    con.executemany("INSERT INTO commands VALUES(?,?,?,?)", [(n + i + 1, "pcie", "admin", sqe(0x0A, cid=i & 0xFFFF, cdw10=7, cdw11=i)) for i in range(n)])
    con.commit(); con.close()
    mapping = {"table": "commands", "id": "id", "test": {"const": "t"}, "seq": {"const": 1}, "interface": "interface", "queue": "queue",
               "sqe": "sqe", "cs": {"const": "nvm"}}
    src = cmddb.register_source(conn, str(tmp_path / "huge.db"), mapping); cmddb.import_source(conn, src)
    s = _scn(conn, 201, [_step(0x09, 7), _step(0x0A, 7)])
    t = time.time()
    run = coverage.run_coverage(conn, 1, src)
    took = time.time() - t
    got = conn.execute("SELECT state, match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, s)).fetchone()
    assert tuple(got) == ("matched", n)          # every pair found, within the step budget
    assert took < 30                             # the old matcher rescanned consumed rows for every match (quadratic)


def test_catalog_override_validates_its_values(ws, conn, parsed_set):
    from specforge.api.app import create_app

    url = "/api/sets/1/catalog/override"
    with TestClient(create_app()) as client:
        for bad in ({"key": "cmd:admin:06", "observability": "submissionn"}, {"key": "cmd:admin:06", "observability": ["stimulus"]},
                    {"key": "cmd:admin:06", "observability": "stimulus", "na_reason": 5}, {"key": 7, "observability": "stimulus"}):
            assert client.put(url, json=bad).status_code == 422, bad
        assert connect().execute("SELECT COUNT(*) FROM item_override").fetchone()[0] == 0          # the typo was not stored
        assert client.put(url, json={"key": "cmd:admin:06", "observability": "stimulus", "na_reason": "why"}).status_code == 200
        assert client.put(url, json={"key": "cmd:admin:06", "observability": None, "na_reason": None}).status_code == 200      # clears the override
