"""Regression tests for the issues found in the PR #1 code review."""
import json
import shutil
import sqlite3
import time

from fastapi.testclient import TestClient

from specforge import cmddb, coverage, jobs
from specforge.core.db import connect, rows
from specforge.devtools.synth import sqe, write_pdf
from specforge.ingest.pdf import parse_pdf


def test_paragraph_split_by_page_break_is_rejoined(tmp_path):
    words = " ".join(["alpha"] * 900) + "."
    p = tmp_path / "long.pdf"
    write_pdf(p, [("title", "NVM Express® Base Specification", "Revision 2.4"), ("h", 1, "1 Intro"), ("p", words)], header="x")
    s = parse_pdf(str(p), workers=1)
    assert s.pages >= 3
    sec = next(x for x in s.sections if x.number == "1")
    assert len(sec.paragraphs) == 1 and sec.paragraphs[0].count("alpha") == 900     # nothing lost, nothing split


def _cmd_db(path, rows_):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, interface TEXT, queue TEXT, cs TEXT, sqe BLOB)")
    for i, (test, queue, blob, *cs) in enumerate(rows_, start=1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?,?)", (i, test, i, "pcie", queue, cs[0] if cs else "nvm", blob))
    con.commit(); con.close()


def _manual(conn, steps, n, **kw):
    pred = {"scope": "test", "order": "strict", "steps": steps, **kw}
    conn.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,observability,predicate_json,state) VALUES(1,?, 'manual','seq','t','submission',?, 'approved')", (f"M:r{n}", json.dumps(pred)))
    conn.commit()
    return conn.execute("SELECT id FROM scenario WHERE sid=?", (f"M:r{n}",)).fetchone()[0]


def _step(op, fid):
    return {"interface": "pcie", "queue": "admin", "cs": None, "opcode": op, "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": fid}]}


def test_any_order_matches_are_disjoint(conn, parsed_set, tmp_path):
    set7, get7 = sqe(0x09, cdw10=7), sqe(0x0A, cdw10=7)
    _cmd_db(tmp_path / "c.db", [("t", "admin", b) for b in (get7, set7, get7, set7, sqe(0x06))])
    sid = cmddb.register_source(conn, str(tmp_path / "c.db")); cmddb.import_source(conn, sid)
    any_ = _manual(conn, [_step(0x0A, 7), _step(0x09, 7)], 1, order="any")
    strict = _manual(conn, [_step(0x0A, 7), _step(0x09, 7)], 2)
    run = coverage.run_coverage(conn, 1, sid)
    count = lambda i: conn.execute("SELECT match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, i)).fetchone()[0]
    assert count(any_) == 2           # (rows 1,2) and (rows 3,4); the old loop re-found the same rows repeatedly
    assert count(strict) == 2
    ev = json.loads(conn.execute("SELECT evidence_json FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, any_)).fetchone()[0])
    assert len(ev) == 4 and len({e["src_row_id"] for e in ev}) == 4


def test_failed_or_cancelled_job_leaves_no_partial_writes(ws):
    conn = connect()
    from specforge.core.config import db_path

    def boom(c, prog):
        c.execute("INSERT INTO setting(key,value) VALUES('half','written')")
        raise RuntimeError("fail midway")

    def cancelled(c, prog):
        c.execute("INSERT INTO setting(key,value) VALUES('half2','written')")
        raise InterruptedError

    ids = [jobs.submit(conn, "t", boom, db_file=str(db_path())), jobs.submit(conn, "t", cancelled, db_file=str(db_path()))]
    for _ in range(100):
        if all(jobs.get(conn, i)["state"] in ("failed", "cancelled") for i in ids):
            break
        time.sleep(0.05)
    assert [jobs.get(conn, i)["state"] for i in ids] == ["failed", "cancelled"]
    assert conn.execute("SELECT COUNT(*) FROM setting WHERE key IN ('half','half2')").fetchone()[0] == 0


def test_bulk_approve_validates_predicates(ws, demo_dir):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        ids = [client.post("/api/specs/upload", files=[("files", (n, open(demo_dir / n, "rb"), "application/pdf"))]).json()[0]["id"]
               for n in ("nvme-base-2.4.pdf", "nvme-nvm-cs-1.3.pdf", "nvme-mi-2.0.pdf")]
        for d in ids:
            j = client.post(f"/api/specs/{d}/parse").json()["job"]
            while client.get(f"/api/jobs/{j}").json()["state"] not in ("done", "failed"):
                time.sleep(0.1)
        sid = client.post("/api/sets", json={"name": "s", "document_ids": ids}).json()["id"]
        j = client.post(f"/api/sets/{sid}/catalog/build").json()["job"]
        while client.get(f"/api/jobs/{j}").json()["state"] not in ("done", "failed"):
            time.sleep(0.1)
        c = connect()
        good = {"scope": "global", "order": "strict", "steps": [{"command": "cmd:admin:06", "interface": "pcie", "queue": "admin", "cs": None, "opcode": 6, "fields": []}]}
        bad = {**good, "steps": [{**good["steps"][0], "opcode": 7}]}
        for name, pred in (("good", good), ("bad", bad)):
            c.execute("INSERT INTO scenario(set_id,sid,origin,title,observability,predicate_json,state) VALUES(?,?,?,?,?,?, 'draft')", (sid, name, "ai", name, "submission", json.dumps(pred)))
        c.commit()
        ids_ = {r["sid"]: r["id"] for r in rows(c, "SELECT id,sid FROM scenario WHERE origin='ai'")}
        res = client.post("/api/scenarios/bulk", json={"ids": list(ids_.values()), "state": "approved"}).json()
        assert res["updated"] == [ids_["good"]] and res["refused"][0]["id"] == ids_["bad"] and res["ok"] is False
        states = {r["sid"]: r["state"] for r in rows(c, "SELECT sid,state FROM scenario WHERE origin='ai'")}
        assert states == {"good": "approved", "bad": "draft"}
        # activating a run that is not finished is refused
        c.execute("INSERT INTO parse_run(document_id,parser_version,status) VALUES(?, '1', 'failed')", (ids[0],)); c.commit()
        rid = c.execute("SELECT MAX(id) FROM parse_run").fetchone()[0]
        assert client.post(f"/api/runs/{rid}/activate").status_code == 409
        assert client.post("/api/runs/99999/activate").status_code == 404


def test_command_db_path_with_special_characters(conn, demo_dir, tmp_path):
    odd = tmp_path / "run#3 100%" / "what?"
    odd.mkdir(parents=True)
    shutil.copy(demo_dir / "cmds-a.db", odd / "cmds.db")
    path = str(odd / "cmds.db")
    assert cmddb.suggest_mapping(path)["mapping"]["table"] == "commands"
    sid = cmddb.register_source(conn, path)
    assert cmddb.import_source(conn, sid)["decoded"] == 219


def test_numeric_command_set_identifiers_are_translated(conn, parsed_set, tmp_path):
    _cmd_db(tmp_path / "c.db", [("t", "io", sqe(0x02, nsid=1, cdw12=7), "0"), ("t", "io", sqe(0x02, nsid=1, cdw12=7), "2")])
    p = cmddb.suggest_mapping(str(tmp_path / "c.db"))["mapping"]
    assert p["cs"] == "cs"
    sid = cmddb.register_source(conn, str(tmp_path / "c.db")); cmddb.import_source(conn, sid)
    assert {r[0] for r in conn.execute("SELECT cs FROM cmd_signature WHERE source_id=?", (sid,))} == {"nvm", "zns"}
    run = coverage.run_coverage(conn, 1, sid)
    st = conn.execute("SELECT state FROM item_result WHERE run_id=? AND item_key='cmd:io:nvm:02'", (run,)).fetchone()[0]
    assert st == "covered"                       # CSI 0 -> 'nvm' matches the NVM Read scenario (CSI 2 = ZNS does not)


def test_upload_uses_original_filename_for_identity_and_rejects_templates(ws, tmp_path):
    from specforge.api.app import create_app

    p = tmp_path / "x.pdf"
    write_pdf(p, [("h", 1, "1 Intro"), ("p", "No identity text on any page.")], header="x")
    with TestClient(create_app()) as client:
        d = client.post("/api/specs/upload", files=[("files", ("NVM-Express-Base-Specification-Revision-2.1.pdf", p.read_bytes(), "application/pdf"))]).json()[0]
        assert (d["family"], d["version"], d["filename"]) == ("nvme-base", "2.1", "NVM-Express-Base-Specification-Revision-2.1.pdf")
        assert client.post("/api/specs/upload", files=[("files", ("t.dotx", b"x", "application/octet-stream"))]).status_code == 400
