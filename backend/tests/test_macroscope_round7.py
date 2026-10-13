"""Regression tests for the seventh round of Macroscope findings (review of 7255220 / 9b63e8d)."""
import shutil
import sqlite3

from fastapi.testclient import TestClient

from specforge import catalog, cmddb
from specforge.core.hashing import sha256_file
from specforge.devtools.synth import sqe


def test_deleting_a_set_cleans_legacy_databases_without_cascade_foreign_keys(ws, conn, parsed_set):
    # a database created before the cascading foreign keys existed: result tables without FKs
    conn.executescript("""DROP TABLE coverage_result; DROP TABLE item_result;
        CREATE TABLE coverage_result(run_id INTEGER NOT NULL, scenario_id INTEGER NOT NULL, state TEXT NOT NULL, match_count INTEGER DEFAULT 0,
            evidence_json TEXT, sid TEXT, origin TEXT, kind TEXT, title TEXT, observability TEXT, review_state TEXT, predicate_json TEXT,
            PRIMARY KEY(run_id, scenario_id));
        CREATE TABLE item_result(run_id INTEGER NOT NULL, item_key TEXT NOT NULL, state TEXT NOT NULL, covered INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0, detail_json TEXT, kind TEXT, parent_key TEXT, label TEXT, observability TEXT, na_reason TEXT,
            attrs_json TEXT, PRIMARY KEY(run_id, item_key));""")
    conn.execute("INSERT INTO cmd_source(name,path,mapping_json) VALUES('s','/x','{}')")
    conn.execute("INSERT INTO coverage_run(set_id,source_id) VALUES(1,1)")
    conn.execute("INSERT INTO coverage_result(run_id,scenario_id,state) VALUES(1,1,'matched')")
    conn.execute("INSERT INTO item_result(run_id,item_key,state) VALUES(1,'k','covered')")
    conn.commit()
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        assert client.delete("/api/sets/1").json() == {"ok": True}          # through the endpoint, not direct SQL
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("coverage_run", "coverage_result", "item_result")] == [0, 0, 0]


def test_import_records_the_hash_of_the_bytes_it_imported(conn, parsed_set, demo_dir, tmp_path):
    p = tmp_path / "c.db"
    shutil.copy(demo_dir / "cmds-a.db", p)
    sid = cmddb.register_source(conn, str(p))
    registered = conn.execute("SELECT sha256 FROM cmd_source WHERE id=?", (sid,)).fetchone()[0]
    con = sqlite3.connect(p)                                                       # the file changes after registration
    con.execute("INSERT INTO commands(test_name,script,seq,ts,interface,queue,qid,sqe) VALUES('late','s',1,'t','pcie','admin',0,?)", (sqe(6, cdw10=9),))
    con.commit(); con.close()
    r = cmddb.import_source(conn, sid)
    now = conn.execute("SELECT sha256 FROM cmd_source WHERE id=?", (sid,)).fetchone()[0]
    assert r["rows"] == 220 and now != registered and now == sha256_file(p)         # provenance names the bytes that were decoded
    assert catalog is not None
