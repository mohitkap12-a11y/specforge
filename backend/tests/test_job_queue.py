"""Shared server: jobs have owners, Docling work waits in one first-come-first-served queue, asking again for work that
is already queued joins it, queued conversions and parses survive a restart, and an admin resets one area at a time."""
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_convert_docling import Gate, _post, _wait, pdf_file, stub  # noqa: F401 - fixtures
from test_docling_ir import docling_stub, real_pdf  # noqa: F401 - fixtures

from specforge.convert import docling_engine


def _until(cond, timeout=10):
    t = time.time()
    while not cond():
        assert time.time() - t < timeout, "timed out"
        time.sleep(0.02)


def test_a_queued_job_knows_its_place_in_line(conn):
    from specforge import jobs
    from specforge.core.db import db_path

    db, pool, release = str(db_path()), ThreadPoolExecutor(1), threading.Event()
    first = jobs.submit(conn, "convert-markdown", lambda c, p: release.wait(20), db_file=db, pool=pool, queue="q",
                        user="ann", label="Convert A.pdf")
    _until(lambda: jobs.get(conn, first)["state"] == "running")
    second = jobs.submit(conn, "parse", lambda c, p: None, db_file=db, pool=pool, queue="q", user="bob", label="Parse B")
    third = jobs.submit(conn, "parse", lambda c, p: None, db_file=db, pool=pool, queue="q", user="cy", label="Parse C")
    other = jobs.submit(conn, "parse", lambda c, p: None, db_file=db, pool=pool, queue="elsewhere")
    assert jobs.get(conn, first)["ahead"] == 0
    s = jobs.get(conn, second)
    assert (s["ahead"], s["ahead_of"], s["user"], s["label"]) == (1, {"label": "Convert A.pdf", "user": "ann"}, "bob", "Parse B")
    assert jobs.get(conn, third)["ahead"] == 2
    assert jobs.get(conn, other)["ahead"] == 0                    # another line: nothing ahead of it there
    jobs.cancel(second, db)
    assert jobs.get(conn, third)["ahead"] == 1                    # a cancelled job no longer holds a place
    fourth = jobs.submit(conn, "parse", lambda c, p: None, db_file=db, pool=pool, queue="q", label="Parse D")
    conn.execute("UPDATE job SET state='queued' WHERE id=?", (second,))   # a cancel still pending: the row is not flipped yet
    conn.commit()
    with jobs._plock:
        jobs._cancel[(db, second)] = threading.Event()
        jobs._cancel[(db, second)].set()
    assert jobs.get(conn, fourth)["ahead"] == 2 and [j["ahead"] for j in jobs.active(conn) if j["id"] == fourth] == [2]
    release.set()
    _until(lambda: jobs.get(conn, fourth)["state"] == "done")
    assert jobs.get(conn, second)["state"] in ("cancelled", "queued")


def test_restart_keeps_conversions_and_parses_queued_in_their_order(conn):
    from specforge import jobs

    def add(type_, state):
        return conn.execute("INSERT INTO job(type,state,payload_json) VALUES(?,?,'{}')", (type_, state)).lastrowid
    running, coverage, queued, done = add("convert-markdown", "running"), add("coverage", "running"), add("parse", "queued"), add("parse", "done")
    conn.commit()
    failed, again = jobs.recover_interrupted(conn, ("convert-markdown", "parse"))
    assert failed == 1 and [(j["id"], j["state"]) for j in again] == [(running, "queued"), (queued, "queued")]
    states = dict(conn.execute("SELECT id, state FROM job").fetchall())
    assert states == {running: "queued", coverage: "failed", queued: "queued", done: "done"}


def test_a_conversion_and_a_parse_queued_before_a_restart_run_after_it(stub, pdf_file, real_pdf, ws):  # noqa: F811
    from specforge.api.app import create_app
    from specforge.core.db import get_conn
    from specforge.ingest import store

    c = get_conn()
    folder = ws / "conversions" / "tmpwaiting"
    folder.mkdir(parents=True)
    (folder / "tmpabc.pdf").write_bytes(pdf_file.read_bytes())
    payload = {"filename": "Waiting.pdf", "user": "ann", "engine": None, "dir": folder.name, "upload": "tmpabc.pdf",
               "sha256": "x", "front_matter": True}
    conv = c.execute("INSERT INTO job(type,state,payload_json,user,label,queue) VALUES('convert-markdown','running',?,'ann','Convert Waiting.pdf','docling')",
                     (json.dumps(payload),)).lastrowid
    lost = c.execute("INSERT INTO job(type,state,payload_json) VALUES('convert-markdown','queued',?)",       # queued by an older version
                     (json.dumps({"filename": "Old.pdf", "dir": "gone"}),)).lastrowid
    doc = store.register_document(c, real_pdf, filename=real_pdf.name)
    parse = c.execute("INSERT INTO job(type,state,payload_json,user,queue) VALUES('parse','queued',?,'bob','docling')",
                      (json.dumps({"document_id": doc["id"], "engine": "auto"}),)).lastrowid
    c.commit()
    with TestClient(create_app()) as client:
        assert _wait(client, conv)["state"] == "done"
        assert (folder / "out.md").is_file() and (folder / "source.pdf").is_file()
        assert ("Waiting.pdf", "done") in [(e["source"], e["state"]) for e in client.get("/api/convert/markdown/jobs").json()]
        assert _wait(client, parse)["state"] == "done"
        assert client.get("/api/specs").json()[0]["runs"][0]["status"] == "done"
        old = client.get(f"/api/jobs/{lost}").json()
        assert old["state"] == "failed" and "not resumed" in old["error"]
        assert client.get(f"/api/jobs/{parse}").json()["queue"] is None     # needs no conversion: not counted in that queue
        from specforge import eventlog
        eventlog.drain(2.0)
        said = [r[0] for r in c.execute("SELECT message FROM event_log WHERE source='system'")]
        assert "1 job(s) were interrupted by the previous shutdown and marked failed" in said
        assert "2 queued conversion(s) and parse(s) resumed after the restart, in their old order" in said


def test_parses_that_need_docling_wait_in_the_conversion_queue_and_others_do_not(docling_stub, real_pdf, ws):  # noqa: F811
    from specforge import convert
    from specforge.api.app import create_app

    release = threading.Event()
    with TestClient(create_app()) as client:
        doc = client.post("/api/specs/upload", files={"files": (real_pdf.name, io.BytesIO(real_pdf.read_bytes()))}).json()[0]
        busy = convert.serial_pool().submit(release.wait, 20)          # someone else's long conversion holds the queue
        first = client.post(f"/api/specs/{doc['id']}/parse?engine=docling").json()
        again = client.post(f"/api/specs/{doc['id']}/parse?engine=docling").json()
        assert again == {"job": first["job"], "joined": "local"}        # asking again follows the job already queued
        j = client.get(f"/api/jobs/{first['job']}").json()
        assert (j["state"], j["queue"], j["label"], j["mine"], j["can_cancel"]) == ("queued", "docling", "Parse nvme-base 2.4", True, True)
        activating = client.post(f"/api/specs/{doc['id']}/parse?engine=docling&activate=true").json()
        assert "joined" not in activating and activating["job"] != first["job"]    # would not activate if it joined the first
        assert client.post(f"/api/specs/{doc['id']}/parse?engine=docling&activate=true").json() == {"job": activating["job"], "joined": "local"}
        quick = client.post(f"/api/specs/{doc['id']}/parse?engine=builtin").json()["job"]   # no conversion needed: not queued behind it
        assert _wait(client, quick)["state"] == "done"
        assert client.get(f"/api/jobs/{first['job']}").json()["state"] == "queued" and docling_stub == []
        release.set()
        busy.result(5)
        assert _wait(client, first["job"])["state"] == "done" and len(docling_stub) == 1
        assert _wait(client, activating["job"])["state"] == "done" and len(docling_stub) == 1   # converted once


@pytest.fixture()
def shared(ws, monkeypatch):
    monkeypatch.setenv("SPECFORGE_AUTH", "1")
    from specforge import auth
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    for name, role in (("root", "admin"), ("ann", "user"), ("bob", "user")):
        auth.create_user(get_conn(), name, f"{name}pass12", role=role, status="active")
    return create_app()


def _signed_in(app, name):
    c = TestClient(app, base_url="http://sf.test")
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": name, "password": f"{name}pass12"}).status_code == 200
    return c


def test_users_follow_their_own_jobs_join_the_same_file_and_cancel_only_their_own(shared, stub, pdf_file, monkeypatch):  # noqa: F811
    gate = Gate()
    monkeypatch.setattr(docling_engine, "_converter", gate)
    root, ann, bob = (_signed_in(shared, n) for n in ("root", "ann", "bob"))
    try:
        spec = _post(ann, pdf_file, "Base.pdf")
        assert gate.started.wait(10)
        data = pdf_file.read_bytes() + b"\n%Base.pdf"                   # the same file again, from another user
        r = bob.post("/api/convert/markdown/jobs", files={"file": ("base-copy.pdf", io.BytesIO(data))}).json()
        assert r == {"job": spec, "reused": True, "joined": "ann"}
        own = _post(bob, pdf_file, "Other.pdf")

        waiting = bob.get(f"/api/jobs/{own}").json()
        assert waiting["ahead"] == 1 and waiting["ahead_of"] == {"label": "Convert Base.pdf to Markdown", "user": "ann"}
        listed = {j["id"]: (j["mine"], j["can_cancel"]) for j in bob.get("/api/jobs?state=active").json()}
        assert listed == {spec: (True, False), own: (True, True)}            # the joined one too, but only ann or an admin cancels it
        assert [j["id"] for j in ann.get("/api/jobs?state=active").json()] == [spec]
        assert {j["id"] for j in root.get("/api/jobs?state=active").json()} == {spec, own}   # an admin sees everyone's
        assert "Base.pdf" in [e["source"] for e in bob.get("/api/convert/markdown/jobs").json()]

        assert bob.post(f"/api/jobs/{spec}/cancel").status_code == 403
        assert ann.post(f"/api/jobs/{own}/cancel").status_code == 404                   # not hers to see
        assert ann.get(f"/api/jobs/{own}").status_code == 404 and ann.get(f"/api/jobs/{own}/events").status_code == 404
        assert root.get(f"/api/jobs/{own}").status_code == 200

        # ann drops the conversion bob joined: it goes on for bob instead of being cancelled
        assert ann.post(f"/api/jobs/{spec}/cancel").json() == {"ok": True, "handed_to": "bob"}
        j = bob.get(f"/api/jobs/{spec}").json()
        assert (j["state"], j["user"], j["can_cancel"]) == ("running", "bob", True) and ann.get(f"/api/jobs/{spec}").status_code == 404

        # a third file both ask for: ann joins bob's queued request, then stops following it; bob's stays
        third = _post(bob, pdf_file, "Third.pdf")
        data3 = pdf_file.read_bytes() + b"\n%Third.pdf"
        assert ann.post("/api/convert/markdown/jobs", files={"file": ("t.pdf", io.BytesIO(data3))}).json()["joined"] == "bob"
        assert next(e for e in ann.get("/api/convert/markdown/jobs").json() if e["job"] == third)["can_remove"] is True
        assert ann.delete(f"/api/convert/markdown/jobs/{third}").json() == {"ok": True, "left": True}
        assert third not in [e["job"] for e in ann.get("/api/convert/markdown/jobs").json()]
        assert bob.get(f"/api/jobs/{third}").json()["state"] == "queued"
        # bob removes it after ann joined again: handed to her
        ann.post("/api/convert/markdown/jobs", files={"file": ("t.pdf", io.BytesIO(data3))})
        assert bob.delete(f"/api/convert/markdown/jobs/{third}").json() == {"ok": True, "handed_to": "ann"}

        assert root.post(f"/api/jobs/{own}/cancel").status_code == 200
        gate.gate.set()
        assert _wait(bob, spec)["state"] == "done" and _wait(bob, own)["state"] == "cancelled"
        assert _wait(ann, third)["state"] == "done"
        assert bob.get("/api/reset").status_code == 403 and bob.post("/api/reset", json={"scope": "comparisons"}).status_code == 403
    finally:
        gate.gate.set()
        for c in (root, ann, bob):
            c.__exit__(None, None, None)


def test_reset_one_area_at_a_time(ws, demo_dir):
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    with TestClient(create_app()) as client:
        pdf = demo_dir / "nvme-base-2.4.pdf"
        doc = client.post("/api/specs/upload", files=[("files", (pdf.name, pdf.read_bytes(), "application/pdf"))]).json()[0]
        for _ in range(2):
            assert _wait(client, client.post(f"/api/specs/{doc['id']}/parse").json()["job"])["state"] == "done"
        runs = [r["id"] for r in client.get("/api/specs").json()[0]["runs"]]
        assert client.post("/api/compare", json={"from_run": runs[1], "to_run": runs[0]}).status_code == 200
        client.post("/api/sets", json={"name": "demo", "document_ids": [doc["id"]]})
        src = client.post("/api/cmddb", json={"path": str(demo_dir / "cmds-a.db")}).json()["id"]
        assert _wait(client, client.post(f"/api/cmddb/{src}/import").json()["job"])["state"] == "done"
        library = ws / "conversions" / "kept"
        library.mkdir(parents=True)
        (library / "out.md").write_text("# kept")

        assert client.get("/api/reset?scope=nope").status_code == 400
        assert client.post("/api/reset", json={"scope": []}).status_code == 400
        assert client.post("/api/reset", json=["all"]).status_code == 422
        assert client.get("/api/reset?scope=coverage").json()["counts"] == {"command_dbs": 1, "executed_scenarios": 0, "coverage_runs": 0}
        assert client.get("/api/reset?scope=specs").json()["counts"]["comparisons"] == 1

        c = get_conn()
        busy = c.execute("INSERT INTO job(type,state,label) VALUES('coverage','running','Coverage of cmds-a')").lastrowid
        c.commit()
        assert client.get("/api/reset?scope=coverage").json()["blocked_by"] == [{"id": busy, "label": "Coverage of cmds-a", "user": None}]
        assert client.post("/api/reset", json={"scope": "coverage"}).status_code == 409   # its own area is busy
        r = client.post("/api/reset", json={"scope": "comparisons"})                        # another area is not
        assert r.status_code == 200 and r.json()["deleted"] == {"comparisons": 1}
        assert client.get("/api/compare").json() == [] and len(client.get("/api/specs").json()) == 1
        c.execute("UPDATE job SET state='failed' WHERE id=?", (busy,))
        c.commit()

        assert client.post("/api/reset", json={"scope": "coverage"}).json()["deleted"]["command_dbs"] == 1
        assert client.get("/api/cmddb").json() == [] and len(client.get("/api/specs").json()) == 1 and len(client.get("/api/sets").json()) == 1

        assert client.post("/api/reset", json={"scope": "specs"}).json()["deleted"]["specs"] == 1
        assert client.get("/api/specs").json() == [] and client.get("/api/sets").json() == []
        assert (library / "out.md").is_file()                                            # the To Markdown library is kept

        r = client.post("/api/reset", json={"scope": None})                  # null means everything, said as such
        assert r.status_code == 200 and r.json()["scope"] == "all"
        assert not library.exists()
