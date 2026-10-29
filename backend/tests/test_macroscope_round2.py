"""Regression tests for the second round of Macroscope findings (review of c0a5cfa)."""
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pymupdf
from fastapi.testclient import TestClient

from specforge import cmddb, coverage, jobs, scenarios
from specforge.core.config import db_path
from specforge.core.db import connect
from specforge.devtools.synth import sqe, write_docx, base_spec
from specforge.ingest import store
from specforge.ingest.pdf import parse_pdf


# ------------------------------------------------------------------ ai grounding

def test_grounding_is_case_sensitive_but_ignores_layout():
    from specforge.crossgen import _grounded

    corpus = "The Controller SHALL abort the\ncommand – with status Invalid Field."
    assert not _grounded("the controller shall abort", corpus)          # case differs: a different sentence, not a quote
    assert _grounded("The Controller SHALL abort the command - with status", corpus)   # line break / dash style are layout only


# ------------------------------------------------------------------ cmddb identifiers

def test_quoted_identifiers_in_the_command_db(conn, tmp_path):
    con = sqlite3.connect(tmp_path / "q.db")
    con.execute('CREATE TABLE "we""ird" (id INTEGER PRIMARY KEY, "te""st" TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)')
    for i in (1, 2):
        con.execute('INSERT INTO "we""ird" VALUES(?,?,?,?,?,?)', (i, "t", i, "pcie", "admin", sqe(6, cdw10=i)))
    con.commit(); con.close()
    sug = cmddb.suggest_mapping(str(tmp_path / "q.db"))
    assert sug["mapping"]["table"] == 'we"ird'
    mapping = {**sug["mapping"], "test": 'te"st'}
    sid = cmddb.register_source(conn, str(tmp_path / "q.db"), mapping)
    r = cmddb.import_source(conn, sid)
    assert (r["decoded"], r["undecodable"]) == (2, 0)


# ------------------------------------------------------------------ pdf

def test_plumber_fallback_when_pymupdf_finds_no_table(demo_dir, monkeypatch):
    normal = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    monkeypatch.setattr(pymupdf.Page, "find_tables", lambda self, *a, **k: SimpleNamespace(tables=[]))
    s = parse_pdf(str(demo_dir / "nvme-base-2.4.pdf"), workers=1)
    assert [t.label for t in s.tables] == [t.label for t in normal.tables]
    assert {t.extractor for t in s.tables} == {"pdfplumber"} and not any(n["kind"] == "figure_without_table" for n in s.notes)
    assert [t.rows for t in s.tables if t.label == "101"] == [t.rows for t in normal.tables if t.label == "101"]


def test_unmatched_bookmark_does_not_block_later_headings(demo_dir, tmp_path):
    doc = pymupdf.open(str(demo_dir / "nvme-base-2.4.pdf"))
    toc = doc.get_toc()
    at = next(i for i, e in enumerate(toc) if e[1].startswith("5.2 "))
    toc.insert(at, [2, "5.1a A bookmark that is not on the page", toc[at][2]])
    doc.set_toc(toc)
    doc.save(str(tmp_path / "ghost.pdf"))
    s = parse_pdf(str(tmp_path / "ghost.pdf"), workers=1)
    t101 = next(t for t in s.tables if t.label == "101")
    assert t101.section == "5.2"                                             # still assigned to the right section
    sec = {x.number: x for x in s.sections}
    assert any("Identify command returns" in p for p in sec["5.2"].paragraphs)
    assert [n["kind"] for n in s.notes if n["kind"] == "heading_not_found"] == ["heading_not_found"]


# ------------------------------------------------------------------ store

def test_diff_detects_structural_changes_and_load_ir_exposes_nesting(conn, demo_dir, tmp_path):
    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    r1, r2 = store.run_parse(conn, d["id"], workers=1), store.run_parse(conn, d["id"], workers=1)
    assert store.diff_runs(conn, r1, r2)["changed"] == []
    conn.execute("UPDATE ir_node SET parent_key='nvme-base@2.4/sec:5.3' WHERE run_id=? AND key='nvme-base@2.4/fig:101'", (r2,))
    conn.commit()
    assert store.diff_runs(conn, r1, r2)["changed"] == ["nvme-base@2.4/fig:101"]        # same text and rows, different ancestry

    p = tmp_path / "nested.docx"
    write_docx(p, base_spec(), nested_demo=True)
    dn = store.register_document(conn, p, family="nvme-base", version="9.9")
    ir = store.load_ir(conn, store.run_parse(conn, dn["id"]))
    inner = next(t for t in ir["tables"].values() if t["key"].endswith("/fig:t1"))
    assert inner["parent_key"] == "nvme-base@9.9/fig:900" and ir["tables"]["900"]["parent_key"].endswith("/sec:9")


# ------------------------------------------------------------------ jobs

def test_progress_does_not_commit_the_workers_transaction(ws):
    conn = connect()
    gate, seen = threading.Event(), threading.Event()

    def work(c, prog):
        c.execute("INSERT INTO setting(key,value) VALUES('partial','1')")
        prog("halfway", 5, 10)
        seen.set()
        gate.wait(10)
        raise RuntimeError("fail after progress")

    jid = jobs.submit(conn, "t", work, db_file=str(db_path()))
    assert seen.wait(10)
    live = jobs.get(conn, jid)
    assert (live["state"], live["message"], live["progress"], live["total"]) == ("running", "halfway", 5, 10)   # live progress is visible
    gate.set()
    for _ in range(100):
        if jobs.get(conn, jid)["state"] == "failed":
            break
        time.sleep(0.05)
    assert jobs.get(conn, jid)["state"] == "failed"
    assert conn.execute("SELECT COUNT(*) FROM setting WHERE key='partial'").fetchone()[0] == 0      # rolled back despite the progress call


def test_cancelling_a_queued_job_prevents_it_from_running(ws):
    conn = connect()
    release, ran = threading.Event(), []
    blockers = [jobs.submit(conn, "b", lambda c, p: release.wait(20), db_file=str(db_path())) for _ in range(4)]   # saturate the pool

    def wait_for(cond):
        for _ in range(200):
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError("timed out")
    wait_for(lambda: all(jobs.get(conn, b)["state"] == "running" for b in blockers))
    queued = jobs.submit(conn, "q", lambda c, p: ran.append(1), db_file=str(db_path()))
    jobs.cancel(queued)
    assert jobs.get(conn, queued)["state"] == "cancelled"      # shown cancelled at once, not when a worker frees up
    release.set()
    wait_for(lambda: all(jobs.get(conn, b)["state"] == "done" for b in blockers))
    time.sleep(0.2)                                            # the freed worker reaches the cancelled job: it must not run it
    assert jobs.get(conn, queued)["state"] == "cancelled" and ran == []


# ------------------------------------------------------------------ coverage engine

def _cmds(path, rows_):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB)")
    for i, (op, fid) in enumerate(rows_, start=1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)", (i, "t", i, "pcie", "admin", sqe(op, cid=i, cdw10=fid)))
    con.commit(); con.close()


def _step(op, fid=None, capture=None, var=None):
    f = []
    if fid is not None:
        f.append({"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": fid})
    if var:
        f.append({"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": {"var": var}})
    s = {"interface": "pcie", "queue": "admin", "cs": None, "opcode": op, "fields": f}
    if capture:
        s["capture"] = {capture: {"dw": 10, "hi": 7, "lo": 0}}
    return s


def _scn(conn, n, steps, **kw):
    conn.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,observability,predicate_json,state) VALUES(1,?, 'manual','seq','t','submission',?, 'approved')",
                 (f"R2:{n}", json.dumps({"scope": "test", "order": "strict", "steps": steps, **kw})))
    conn.commit()
    return conn.execute("SELECT id FROM scenario WHERE sid=?", (f"R2:{n}",)).fetchone()[0]


def _res(conn, run, sid):
    return tuple(conn.execute("SELECT state,match_count FROM coverage_result WHERE run_id=? AND scenario_id=?", (run, sid)).fetchone())


def test_strict_matches_are_disjoint_not_skipped(conn, parsed_set, tmp_path):
    _cmds(tmp_path / "c.db", [(9, 7), (9, 7), (10, 7), (10, 7)])                  # A A B B
    src = cmddb.register_source(conn, str(tmp_path / "c.db")); cmddb.import_source(conn, src)
    s = _scn(conn, 1, [_step(9, 7), _step(10, 7)])
    assert _res(conn, coverage.run_coverage(conn, 1, src), s) == ("matched", 2)  # (A1,B3) and (A2,B4); the old scan reported 1


def test_pathological_sequence_finishes_quickly(conn, parsed_set, tmp_path):
    _cmds(tmp_path / "c.db", [(9, 1)] * 200 + [(10, 2)] * 200)                    # a late variable mismatch used to force ~200^3 paths
    src = cmddb.register_source(conn, str(tmp_path / "c.db")); cmddb.import_source(conn, src)
    s = _scn(conn, 2, [_step(9, capture="fid"), _step(9), _step(10, var="fid")])
    t = time.time()
    run = coverage.run_coverage(conn, 1, src)
    assert _res(conn, run, s) == ("not_matched", 0) and time.time() - t < 15


def test_search_budget_is_reported_as_an_error_not_a_miss(conn, parsed_set, tmp_path, monkeypatch):
    _cmds(tmp_path / "c.db", [(9, 1)] * 60 + [(10, 2)] * 60)
    src = cmddb.register_source(conn, str(tmp_path / "c.db")); cmddb.import_source(conn, src)
    s = _scn(conn, 3, [_step(9, capture="fid"), _step(9), _step(10, var="fid")], order="any")
    monkeypatch.setattr(coverage, "MAX_SEARCH_STEPS", 500)
    run = coverage.run_coverage(conn, 1, src)
    assert _res(conn, run, s)[0] == "error"
    summary = json.loads(conn.execute("SELECT summary_json FROM coverage_run WHERE id=?", (run,)).fetchone()[0])
    assert summary["scenario_errors"] == 1


def test_historical_run_keeps_its_own_item_snapshot(conn, parsed_set, demo_dir, ws):
    src = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, src)
    run = coverage.run_coverage(conn, 1, src)
    with TestClient(__import__("specforge.api.app", fromlist=["x"]).create_app()) as client:
        before = client.get(f"/api/coverage/{run}/items", params={"limit": 2000}).json()
        conn.execute("DELETE FROM catalog_item WHERE set_id=1")                 # catalog rebuilt / items removed later
        conn.execute("UPDATE scenario SET state='rejected'"); conn.commit()
        after = client.get(f"/api/coverage/{run}/items", params={"limit": 2000}).json()
        assert after == before and after["total"] > 150
        from specforge import reports

        ds = reports.build_dataset(conn, run)
        assert len(ds["items"]) == after["total"] and ds["items"][0]["label"]


# ------------------------------------------------------------------ predicate validation

def test_validator_never_raises_on_malformed_input(conn, parsed_set):
    cmds, fields = scenarios.catalog_context(conn, 1)
    bad = [{"steps": [None]}, {"steps": ["x"]}, {"steps": [{}], "scope": []}, {"steps": [{}], "order": {}},
           {"steps": [{"interface": "pcie", "queue": "admin", "opcode": 6, "fields": "x"}]},
           {"steps": [{"interface": "pcie", "queue": "admin", "opcode": 6, "fields": [None, 3]}]},
           {"steps": [{"interface": [], "queue": {}, "opcode": True, "command": ["cmd:admin:06"], "capture": [1]}]},
           {"steps": [{"interface": "pcie", "queue": "admin", "opcode": 6, "fields": [{"dw": [], "hi": 3, "lo": 0, "op": []}]}]},
           {"steps": [{"interface": "pcie", "queue": "admin", "opcode": 6, "capture": {"a": None, "b": {"dw": 10}}}]}]
    for b in bad:
        errs = scenarios.validate_predicate(b, cmds, fields)
        assert errs and all(isinstance(e, str) for e in errs), b


def _p(**step):
    base = {"command": "cmd:io:nvm:02", "interface": "pcie", "queue": "io", "cs": "nvm", "opcode": 2, "fields": []}
    return {"steps": [{**base, **step}]}


def test_queue_and_command_set_must_match_the_catalog(conn, parsed_set):
    cmds, fields = scenarios.catalog_context(conn, 1)
    ok = lambda p: scenarios.validate_predicate(p, cmds, fields)      # noqa: E731
    assert ok(_p()) == []
    assert any("route" in e for e in ok(_p(queue="admin")))               # NVM Read relabelled as an Admin command
    assert any("cs must be" in e for e in ok(_p(cs=None))) and any("cs must be" in e for e in ok(_p(cs="zns")))
    admin = {"command": "cmd:admin:06", "opcode": 6, "fields": [], "cs": None}
    assert ok({"steps": [{**admin, "interface": "pcie", "queue": "admin"}]}) == []
    assert ok({"steps": [{**admin, "interface": "mi", "queue": "admin"}]}) == []        # NVMe-MI tunnel stays valid
    assert any("route" in e for e in ok({"steps": [{**admin, "interface": "pcie", "queue": "io"}]}))
    assert any("route" in e for e in ok({"steps": [{**admin, "interface": "mi", "queue": "mi"}]}))


def test_captured_variables_only_with_eq_ne_and_captures_are_range_checked():
    cap = {"dw": 10, "hi": 7, "lo": 0}
    step = lambda **f: {"interface": "pcie", "queue": "admin", "opcode": 6, "fields": [{"dw": 10, "hi": 7, "lo": 0, **f}]}   # noqa: E731
    first = {"interface": "pcie", "queue": "admin", "opcode": 9, "capture": {"v": cap}}
    for op, val in (("in", {"var": "v"}), ("range", {"var": "v"}), ("mask", {"var": "v"})):
        errs = scenarios.validate_predicate({"steps": [first, step(op=op, value=val)]})
        assert any("only be used with 'eq' or 'ne'" in e for e in errs), op
    assert scenarios.validate_predicate({"steps": [first, step(op="eq", value={"var": "v"})]}) == []
    for bad in ({"dw": 10, "hi": 3, "lo": 7}, {"dw": 10, "hi": 40, "lo": 0}, {"dw": 10, "hi": 7, "lo": -1}):
        errs = scenarios.validate_predicate({"steps": [{**first, "capture": {"v": bad}}]})
        assert any("0 <= lo <= hi <= 31" in e for e in errs), bad
