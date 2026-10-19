import json

from specforge.core import config
from specforge.core.db import rows
from specforge.ingest import store


def test_register_is_content_addressed(conn, demo_dir):
    a = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    b = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    assert a["id"] == b["id"] and (a["family"], a["version"], a["format"], a["pages"]) == ("nvme-base", "2.4", "pdf", 6)
    c = store.register_document(conn, demo_dir / "nvme-base-2.4.docx")
    assert c["id"] != a["id"] and c["format"] == "docx"


def test_parse_persists_ir_and_activates_first_run(conn, demo_dir):
    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    run = store.run_parse(conn, d["id"], workers=1)
    r = rows(conn, "SELECT * FROM parse_run WHERE id=?", (run,))[0]
    assert r["status"] == "done" and r["active"] == 1
    ir = store.load_ir(conn, run)
    assert ir["prefix"] == "nvme-base@2.4"
    assert ir["tables"]["103"]["header"] == ["CNS Value", "O/M", "Description", "Reference Section"]
    assert ir["sections"]["5.2"]["key"] == "nvme-base@2.4/sec:5.2"
    assert any(q["key"].startswith("nvme-base@2.4/sec:5.2/req:") for q in ir["requirements"])
    audit = json.loads(r["audit_json"])
    assert audit["coverage_ready"] and audit["checks"]["text_accounting"]["pass"]


def test_reparse_keeps_old_run_and_diff_is_empty_for_same_input(conn, demo_dir):
    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    r1 = store.run_parse(conn, d["id"], workers=1)
    r2 = store.run_parse(conn, d["id"], workers=1)          # reparse
    active = {r["id"]: r["active"] for r in rows(conn, "SELECT id,active FROM parse_run")}
    assert active == {r1: 1, r2: 0}                           # reparse never silently replaces the active run
    diff = store.diff_runs(conn, r1, r2)
    assert diff["summary"]["added"] == diff["summary"]["removed"] == diff["summary"]["changed"] == 0 and diff["summary"]["unchanged"] > 25
    store.activate_run(conn, r2)
    active = {r["id"]: r["active"] for r in rows(conn, "SELECT id,active FROM parse_run")}
    assert active == {r1: 0, r2: 1}
    # both runs remain loadable (stable keys are identical)
    assert set(store.load_ir(conn, r1)["tables"]) == set(store.load_ir(conn, r2)["tables"])


def test_corrections_survive_reparse_and_orphans_are_reported(conn, demo_dir):
    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    r1 = store.run_parse(conn, d["id"], workers=1)
    new_rows = [["31:16", "Fixed row A"], ["15:08", "Reserved"], ["07:00", "Fixed row B"]]
    store.set_correction(conn, "nvme-base", "2.4", "nvme-base@2.4/fig:101", "table_replace", {"header": ["Bits", "Description"], "rows": new_rows})
    store.set_correction(conn, "nvme-base", "2.4", "nvme-base@2.4/fig:9999", "table_replace", {"header": ["a"], "rows": []})
    r2 = store.run_parse(conn, d["id"], workers=1)
    assert store.load_ir(conn, r1)["tables"]["101"]["rows"] != new_rows
    assert store.load_ir(conn, r2)["tables"]["101"]["rows"] == new_rows
    kinds = {i["kind"] for i in rows(conn, "SELECT kind FROM parse_issue WHERE run_id=?", (r2,))}
    assert "correction_orphaned" in kinds and "correction_applied" in kinds
    assert store.diff_runs(conn, r1, r2)["changed"] == ["nvme-base@2.4/fig:101"]


def test_parser_version_bump_flags_reparse_available(conn, demo_dir, monkeypatch):
    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    store.run_parse(conn, d["id"], workers=1)
    assert store.reparse_available(conn) == []
    monkeypatch.setattr(config, "PARSER_VERSION", "999")
    monkeypatch.setattr(store, "PARSER_VERSION", "999")
    assert store.reparse_available(conn) == [d["id"]]


def test_failed_parse_is_recorded(conn, demo_dir, monkeypatch):
    import pytest

    d = store.register_document(conn, demo_dir / "nvme-base-2.4.pdf")
    import specforge.ingest.pdf as pdfmod

    monkeypatch.setattr(pdfmod, "parse_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        store.run_parse(conn, d["id"], workers=1)
    r = rows(conn, "SELECT status,error,active FROM parse_run")[0]
    assert r["status"] == "failed" and "boom" in r["error"] and r["active"] == 0
