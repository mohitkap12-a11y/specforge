import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from specforge import eventlog, jobs
from specforge.ai import AIOutput, generate_ai_scenarios
from specforge.core.db import connect
from specforge.llm.service import LLMService

from llm_helpers import Fake, reply_for, settings


@pytest.fixture(autouse=True)
def default_logging():
    eventlog.configure({})
    yield
    eventlog.configure({})


def events(conn, **kw):
    assert eventlog.drain(10)
    return eventlog.query(conn, limit=2000, **kw)


def test_round_trip_filters_and_data(conn):
    eventlog.emit("info", "parser", "hello", pages=3)
    eventlog.emit("warn", "llm", "slow", seconds=9.5)
    eventlog.emit("error", "job", "boom", job_id=7)
    eventlog.emit("debug", "job", "dropped: below the default minimum level")
    ev = events(conn)
    assert [e["message"] for e in ev] == ["hello", "slow", "boom"]
    assert ev[0]["data"] == {"pages": 3} and ev[2]["job_id"] == 7 and ev[0]["ts"].endswith("Z")
    assert [e["message"] for e in events(conn, level="warn")] == ["slow", "boom"]
    assert [e["message"] for e in events(conn, sources=["parser"])] == ["hello"]
    assert [e["message"] for e in events(conn, job_id=7)] == ["boom"]
    assert [e["message"] for e in events(conn, q="9.5")] == ["slow"]
    after = ev[0]["id"]
    assert [e["message"] for e in eventlog.query(conn, after_id=after)] == ["slow", "boom"]
    assert [e["message"] for e in eventlog.query(conn, before_id=ev[2]["id"], limit=1)] == ["slow"]


def test_min_level_is_configurable(conn):
    eventlog.configure({"logging": {"min_level": "debug"}})
    eventlog.emit("debug", "system", "visible now")
    eventlog.configure({"logging": {"min_level": "error"}})
    eventlog.emit("warn", "system", "hidden")
    assert [e["message"] for e in events(conn)] == ["visible now"]


def test_large_detail_is_truncated(conn):
    eventlog.emit("info", "system", "big", blob="x" * 200_000)
    d = events(conn)[0]["data"]
    assert "_truncated" in d and len(json.dumps(d)) < 70_000


def test_events_written_while_another_connection_holds_the_write_lock(conn, ws):
    """A job's open transaction must delay log writes, never deadlock or lose them."""
    holder = connect()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO setting(key,value) VALUES('x','1')")
    eventlog.emit("info", "system", "while locked")
    time.sleep(0.5)
    holder.commit()
    holder.close()
    assert [e["message"] for e in events(conn)] == ["while locked"]


def wait_job(conn, job_id):
    for _ in range(300):
        j = jobs.get(conn, job_id)
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise TimeoutError


def test_job_lifecycle_is_logged_with_job_id(conn):
    def work(c, prog):
        prog("stage one 1/2", 1, 2)
        eventlog.emit("info", "parser", "inside the job")        # picks up the job id from context
        prog("stage two 2/2", 2, 2)
        return {"ok": True}

    jid = jobs.submit(conn, "demo", work)
    assert wait_job(conn, jid)["state"] == "done"
    ev = events(conn, job_id=jid)
    msgs = [e["message"] for e in ev]
    assert msgs[0] == "Queued demo" and "Started demo" in msgs and "inside the job" in msgs
    assert "stage one 1/2" in msgs and "stage two 2/2" in msgs and msgs[-1] == "Finished demo"
    assert ev[-1]["data"]["result"] == {"ok": True} and ev[-1]["data"]["seconds"] >= 0


def test_failed_job_logs_error_with_traceback(conn):
    def work(c, prog):
        raise ValueError("kaput")

    jid = jobs.submit(conn, "demo", work)
    assert wait_job(conn, jid)["state"] == "failed"
    last = events(conn, job_id=jid)[-1]
    assert last["level"] == "error" and "kaput" in last["message"] and "ValueError" in last["data"]["traceback"]


def test_cancelled_job_is_logged_as_warning(conn):
    gate = threading.Event()

    def work(c, prog):
        gate.wait(5)
        prog("after cancel")
        return {}

    jid = jobs.submit(conn, "demo", work)
    jobs.cancel(jid)
    gate.set()
    assert wait_job(conn, jid)["state"] == "cancelled"
    assert events(conn, job_id=jid)[-1]["message"] == "Cancelled demo"


def test_llm_calls_are_logged_with_prompt_and_reply(conn):
    svc = LLMService(conn, settings(), Fake(["not json", '{"testable": false, "reason": "internal"}']))
    svc.generate_model("generate", [{"role": "user", "content": "the question"}], AIOutput, "v1")
    ev = [e for e in events(conn, sources=["llm"], level="debug")]
    msgs = [e["message"] for e in ev]
    assert any("request to fake/fake-model (attempt 1" in m for m in msgs) and any("did not validate" in m for m in msgs)
    assert any("reply accepted" in m for m in msgs)
    req = next(e for e in ev if "request to" in e["message"])
    assert req["data"]["prompt"][0]["content"] == "the question" and req["data"]["model"] == "fake-model"
    done = next(e for e in ev if "reply accepted" in e["message"])
    assert "internal" in done["data"]["response"] and done["data"]["response_chars"] > 0


def test_prompt_capture_can_be_turned_off(conn):
    eventlog.configure({"logging": {"capture_prompts": False}})
    svc = LLMService(conn, settings(), Fake(['{"testable": false, "reason": "secret reason"}']))
    svc.generate_model("generate", [{"role": "user", "content": "secret prompt"}], AIOutput, "v1")
    blob = json.dumps(events(conn, sources=["llm"]))
    assert "secret prompt" not in blob and "secret reason" not in blob


def test_llm_gives_up_is_an_error_event(conn):
    svc = LLMService(conn, {**settings(), "llm": {"max_retries": 1}}, Fake(["nope"]))
    with pytest.raises(Exception):
        svc.generate_model("generate", [{"role": "user", "content": "q"}], AIOutput, "v1")
    ev = events(conn, sources=["llm"])
    assert ev[-1]["level"] == "error" and "gave up after 2 attempts" in ev[-1]["message"]


def test_ai_agent_narrates_each_decision(conn, parsed_set):
    req, base = reply_for(conn)
    bad_quote = {**base, "title": "bad quote", "quotes": ["the controller shall levitate above the namespace"]}
    svc = LLMService(conn, settings(), Fake([json.dumps({"testable": True, "scenarios": [base, bad_quote]})]))
    generate_ai_scenarios(conn, 1, svc, [req["key"]])
    msgs = [e["message"] for e in events(conn, sources=["agent"])]
    assert msgs[0].startswith("Scenario agent starting") and msgs[1].startswith("[1/1] Reading requirement")
    assert any("Rejected draft 'bad quote'" in m and "ungrounded" in m for m in msgs)
    assert any("Drafted scenario" in m for m in msgs) and msgs[-1].startswith("Scenario agent finished")


def test_parser_catalog_import_and_coverage_log_their_steps(conn, parsed_set, demo_dir):
    from specforge import cmddb, coverage

    sid = cmddb.register_source(conn, str(demo_dir / "cmds-a.db"))
    cmddb.import_source(conn, sid)
    coverage.run_coverage(conn, 1, sid)
    by_source = {}
    for e in events(conn):
        by_source.setdefault(e["source"], []).append(e["message"])
    assert any(m.startswith("Audit: 0 error(s)") for m in by_source["parser"]) and any("finished in" in m for m in by_source["parser"])
    assert any(m.startswith("Catalog built: 195 items") for m in by_source["catalog"])
    assert any(m.startswith("Template scenarios: 152 generated") for m in by_source["scenarios"])
    assert any(m.startswith("Import done:") for m in by_source["import"])
    assert any(m.startswith("Coverage run #1 done: 23.31% covered") for m in by_source["coverage"])


# ---------------------------------------------------------------- API
@pytest.fixture()
def client(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_logs_api_history_filters_and_clear(client):
    first = client.get("/api/logs").json()
    assert any(e["message"].startswith("SpecForge ") and e["source"] == "system" for e in first["items"])
    client.put("/api/settings", json={"parser": {"workers": 1}})
    client.get("/api/providers/nope/models")
    out = client.get("/api/logs", params={"source": "api"}).json()
    assert [e["message"] for e in out["items"]][:1] == ["Settings saved"] and out["newest_id"] >= out["items"][-1]["id"]
    assert all(e["level"] in ("warn", "error") for e in client.get("/api/logs", params={"level": "warn"}).json()["items"])
    page = client.get("/api/logs", params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["has_older"] is True
    older = client.get("/api/logs", params={"limit": 1, "before_id": page["items"][0]["id"]}).json()["items"]
    assert older and older[0]["id"] < page["items"][0]["id"]
    assert client.delete("/api/logs").json()["deleted"] >= 2
    rest = client.get("/api/logs").json()["items"]
    assert [e["message"] for e in rest][0].startswith("Activity log cleared")


def test_settings_logging_options_apply_immediately(client):
    client.put("/api/settings", json={"logging": {"min_level": "warn"}})
    client.post("/api/sets", json={"name": "x", "document_ids": []})          # an info-level audit event: now filtered out
    msgs = [e["message"] for e in client.get("/api/logs").json()["items"]]
    assert not any("Spec set 'x'" in m for m in msgs)
    client.put("/api/settings", json={"logging": {"min_level": "info"}})


def test_stream_delivers_new_events(ws):
    """Needs a real server: TestClient cannot close an endless event stream."""
    import socket

    import httpx
    import uvicorn

    from specforge.api.app import create_app

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                newest = httpx.get(base + "/api/logs").json()["newest_id"]
                break
            except httpx.TransportError:
                time.sleep(0.1)
        else:
            raise TimeoutError("server did not start")
        with httpx.stream("GET", base + "/api/logs/stream", params={"after_id": newest, "source": "system"}, timeout=10) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            threading.Timer(0.3, lambda: (eventlog.emit("info", "parser", "other source"), eventlog.emit("info", "system", "streamed event"))).start()
            for line in r.iter_lines():
                if line.startswith("data:"):
                    batch = json.loads(line[5:])
                    assert [e["message"] for e in batch] == ["streamed event"]      # the filter applies to the stream too
                    break
    finally:
        server.should_exit = True
        t.join(5)


# ---------------------------------------------------------------- review fixes
@pytest.mark.parametrize("bad", [{"min_level": []}, {"min_level": "loud"}, {"min_level": 3}, {"capture_prompts": "yes"},
                                 {"preview_chars": 10}, {"preview_chars": "big"}, {"preview_chars": True}, None, 5])
def test_invalid_logging_settings_are_rejected_and_not_stored(client, bad):
    r = client.put("/api/settings", json={"logging": bad})
    assert r.status_code == 422
    assert client.get("/api/settings").json()["logging"] == {"min_level": "info", "capture_prompts": True, "preview_chars": 4000}


def test_settings_must_be_an_object(client):
    assert client.put("/api/settings", json=[1, 2]).status_code == 422


def test_configure_never_raises_on_garbage():
    eventlog.configure({"logging": {"min_level": [], "capture_prompts": None, "preview_chars": {}}})
    eventlog.configure({"logging": 7})
    eventlog.configure(None)
    assert eventlog._cfg["min_level"] == "info" and eventlog._cfg["preview_chars"] == 4000


@pytest.mark.parametrize("body", [{"key": "k", "header": ["a"], "rows": None}, {"key": "k", "header": ["a"], "rows": 5},
                                  {"key": "k", "header": ["a"], "rows": ["x"]}, {"key": "k", "header": "a", "rows": []}, {"key": 1, "header": [], "rows": []}])
def test_table_correction_validates_before_storing(client, parsed_set, body):
    from specforge.core.db import connect

    assert client.put("/api/runs/1/tables/correction", json=body).status_code == 422
    assert connect().execute("SELECT COUNT(*) FROM correction").fetchone()[0] == 0


def test_retention_prunes_every_database_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(eventlog, "KEEP_ROWS", 5)
    monkeypatch.setattr(eventlog, "PRUNE_EVERY", 20)
    dbs = [str(tmp_path / f"{n}.db") for n in "ab"]
    for d in dbs:
        connect(d).close()
    for i in range(30):                      # alternate databases so neither batch alone crosses the threshold in one pass
        for d in dbs:
            eventlog.emit("info", "system", f"e{i}", db=d)
            eventlog.drain(5)
    for d in dbs:
        c = connect(d)
        assert c.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] <= 5 + 20, d
        c.close()


def test_active_jobs_are_not_limited_to_the_recent_page(client):
    from specforge.core.db import connect

    c = connect()
    c.executemany("INSERT INTO job(type,state) VALUES('demo','running')", [()] * 70)
    c.commit()
    c.close()
    assert len(client.get("/api/jobs").json()) == 50                 # the capped history
    assert len(client.get("/api/jobs", params={"state": "active"}).json()) == 70


def test_has_older_only_counts_events_matching_the_filters(client):
    eventlog.emit("error", "job", "an error")
    eventlog.emit("info", "job", "later info")
    eventlog.drain(5)
    # the startup event and "later info" do not match level=error, so there is nothing older to load
    r = client.get("/api/logs", params={"level": "error", "limit": 1}).json()
    assert [e["message"] for e in r["items"]] == ["an error"] and r["has_older"] is False
    # but an unfiltered page starting at "an error" does have older history (the startup event)
    r = client.get("/api/logs", params={"limit": 2}).json()
    assert r["has_older"] is True


def test_newest_id_is_a_watermark_taken_before_the_history(client, monkeypatch):
    """An event committed while the page is being built must be delivered by the stream, never skipped."""
    seen = {}
    real = eventlog.query

    def query(conn, **kw):
        out = real(conn, **kw)
        eventlog.emit("info", "system", "arrived mid-request")      # lands after the history was read
        eventlog.drain(5)
        seen["hit"] = True
        return out

    monkeypatch.setattr(eventlog, "query", query)
    r = client.get("/api/logs").json()
    monkeypatch.setattr(eventlog, "query", real)
    assert seen["hit"] and "arrived mid-request" not in [e["message"] for e in r["items"]]
    after = client.get("/api/logs", params={"after_id": r["newest_id"]}).json()["items"]
    assert "arrived mid-request" in [e["message"] for e in after]


def test_prune_failure_does_not_requeue_or_recount_committed_events(tmp_path):
    class Flaky:
        """Real connection whose retention DELETE hits a lock timeout."""
        def __init__(self, c): self.c = c
        def executemany(self, *a): return self.c.executemany(*a)
        def commit(self): return self.c.commit()
        def rollback(self): return self.c.rollback()
        def execute(self, sql, *a):
            if sql.lstrip().startswith("DELETE"):
                raise sqlite3.OperationalError("database is locked")
            return self.c.execute(sql, *a)

    db = str(tmp_path / "x.db")
    real = connect(db)
    batch = [(db, "2026-01-01T00:00:00.000Z", "info", "system", None, f"e{i}", None, 0) for i in range(12)]
    inserted: dict[str, int] = {}
    old = eventlog.PRUNE_EVERY
    eventlog.PRUNE_EVERY = 5
    try:
        retry, finished = eventlog._write(batch, {db: Flaky(real)}, inserted)
    finally:
        eventlog.PRUNE_EVERY = old
    assert retry == [] and finished == 12                      # committed once: not re-queued, counted exactly once
    assert real.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 12
    assert inserted[db] == 12                                  # the prune is attempted again after the next batch


def test_emit_never_raises_on_unserialisable_details(conn):
    cyc: dict = {}
    cyc["self"] = cyc
    eventlog.emit("info", "system", "tuple keys", bad={(1, 2): "x"})
    eventlog.emit("info", "system", "cycle", bad=cyc)
    eventlog.emit("info", "system", "object", bad=object())
    ev = {e["message"]: e for e in events(conn)}
    assert set(ev) == {"tuple keys", "cycle", "object"}               # all three were recorded
    assert "_unserializable" in ev["tuple keys"]["data"] and "_unserializable" in ev["cycle"]["data"]
    assert "object object" in json.dumps(ev["object"]["data"])        # default=str handles plain objects


def test_sets_report_approved_and_draft_scenario_counts(client):
    from specforge.core.db import connect

    sid = client.post("/api/sets", json={"name": "s", "document_ids": []}).json()["id"]
    c = connect()
    for i, st in enumerate(["approved", "approved", "draft", "rejected"]):
        c.execute("INSERT INTO scenario(set_id,sid,origin,kind,title,observability,predicate_json,state) VALUES(?,?,?,?,?,?,?,?)",
                  (sid, f"S{i}", "ai", "requirement", "t", "submission", "{}", st))
    c.commit()
    c.close()
    s = next(x for x in client.get("/api/sets").json() if x["id"] == sid)
    assert (s["scenarios"], s["scenarios_approved"], s["scenarios_draft"]) == (4, 2, 1)


@pytest.mark.parametrize("bad", [1, "cmd:admin:06", {"a": 1}, [1, 2], ["ok", None]])
def test_ai_generation_validates_item_keys_before_submitting(client, bad):
    r = client.post("/api/sets/1/scenarios/ai", json={"item_keys": bad})
    assert r.status_code == 422 and "item_keys" in r.text
    assert client.get("/api/jobs").json() == []                     # nothing was queued
