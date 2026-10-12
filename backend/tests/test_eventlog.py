import json
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
