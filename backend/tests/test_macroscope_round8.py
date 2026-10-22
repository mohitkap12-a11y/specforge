"""Regression tests for the eighth round of Macroscope findings (review of 9b63e8d)."""
import io
import json
import sqlite3

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from test_macroscope_round2 import _scn, _step

from specforge import cmddb, coverage, reports
from specforge.core.db import rows
from specforge.devtools.synth import sqe


def _step_for(cmd_key, opcode, queue="admin", interface="pcie", fields=None):
    return {"command": cmd_key, "interface": interface, "queue": queue, "cs": None, "opcode": opcode, "fields": fields or []}


def test_predicate_edits_keep_links_valid_and_reset_approval(ws, conn, parsed_set):
    from specforge.api.app import create_app

    # a manual scenario linked to the Identify CNS=01h item, approved
    conn.execute("INSERT INTO scenario(set_id,sid,origin,title,observability,predicate_json,state) VALUES(1,'M:e','manual','t','submission',?, 'approved')",
                 (json.dumps({"scope": "global", "order": "strict", "steps": [_step_for("cmd:admin:06", 6)]}),))
    sid = conn.execute("SELECT id FROM scenario WHERE sid='M:e'").fetchone()[0]
    conn.execute("INSERT INTO scenario_item(scenario_id,item_key) VALUES(?, 'cmd:admin:06/dw10[07:00]=01h')", (sid,))
    conn.commit()
    with TestClient(create_app()) as client:
        patch = lambda body: client.patch(f"/api/scenarios/{sid}", json=body)           # noqa: E731
        other = {"scope": "global", "order": "strict", "steps": [_step_for("cmd:admin:09", 9)]}
        r = patch({"predicate": other})                                                   # command A -> command B
        assert r.status_code == 422 and "no longer refers to cmd:admin:06" in json.dumps(r.json())
        assert json.loads(conn.execute("SELECT predicate_json FROM scenario WHERE id=?", (sid,)).fetchone()[0])["steps"][0]["opcode"] == 6   # unchanged
        same_cmd = {"scope": "global", "order": "strict",
                    "steps": [_step_for("cmd:admin:06", 6, fields=[{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 1}])]}
        assert patch({"predicate": same_cmd}).status_code == 200
        assert conn.execute("SELECT state FROM scenario WHERE id=?", (sid,)).fetchone()[0] == "draft"      # the edit invalidated the approval
        assert patch({"predicate": same_cmd, "state": "approved"}).status_code == 200                       # re-approval in the same request


def test_report_text_is_never_interpreted_as_a_formula(conn, parsed_set, demo_dir):
    conn.execute("UPDATE scenario SET title='=1+1' WHERE sid='T:cmd:admin:06'")
    conn.execute("UPDATE catalog_item SET label='=HYPERLINK(\"http://x\")' WHERE key='cmd:admin:09'")
    conn.commit()
    src = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, src)
    run = coverage.run_coverage(conn, 1, src)
    ds = reports.build_dataset(conn, run)
    wb = load_workbook(io.BytesIO(reports.to_xlsx(ds)))
    hits = [c for w in wb.worksheets for r in w.iter_rows() for c in r if isinstance(c.value, str) and c.value.startswith("=")]
    assert any(c.value == "=1+1" for c in hits) and all(c.data_type == "s" for c in hits)      # literal text, not data_type 'f'
    csv_text = reports.to_csv(ds, "all").decode()
    assert "'=HYPERLINK" in csv_text and ",=HYPERLINK" not in csv_text and csv_text.count("\n=") == 0


def test_within_counts_commands_in_the_stream_not_per_test_seq(conn, parsed_set, tmp_path):
    con = sqlite3.connect(tmp_path / "w.db")
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)")
    layout = [("a", 1, 0x0A, 1), ("a", 2, 0x0A, 3), ("a", 3, 0x09, 7),          # Set FID7 is the last command of test a (seq 3)
              ("b", 1, 0x0A, 7), ("b", 2, 0x0A, 3), ("b", 3, 0x0A, 3)]          # Get FID7 is the very next command in the stream (test b, seq 1)
    layout += [("c", 1, 0x09, 7)] + [("d", i, 0x0A, 3) for i in range(1, 30)] + [("e", 1, 0x0A, 7)]   # Set7 ... 30 commands ... Get7 (same seq 1)
    for i, (t, seq, op, fid) in enumerate(layout, start=1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)", (i, t, seq, "pcie", "admin", sqe(op, cid=i, cdw10=fid)))
    con.commit(); con.close()
    src = cmddb.register_source(conn, str(tmp_path / "w.db")); cmddb.import_source(conn, src)
    near = _scn(conn, 81, [_step(9, 7), _step(0x0A, 7)], scope="global", within=1)
    run = coverage.run_coverage(conn, 1, src)
    count = conn.execute("SELECT match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, near)).fetchone()[0]
    assert count == 1          # Set7(a,seq3) -> Get7(b,seq1) are adjacent in the stream (id distance 1); Set7(c) -> Get7(e) are 30 apart although both have seq 1
    ev = json.loads(conn.execute("SELECT evidence_json FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, near)).fetchone()[0])
    assert [e["test_id"] for e in ev] == ["a", "b"]
    assert rows(conn, "SELECT 1 AS x") and True
