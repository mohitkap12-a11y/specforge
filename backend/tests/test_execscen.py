"""Scenarios derived from execution: one per test case of a command DB, linked to the catalog items it exercised."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from llm_helpers import Fake, settings

from specforge import cmddb, coverage, execscen, reports
from specforge.core.db import jloads, rows
from specforge.devtools.synth import mi_admin_msg, sqe
from specforge.llm.service import LLMService


def make_db(path, tests):
    """tests: {name: [(queue, sqe bytes | None, mi bytes | None)]}; layout A of the synthetic generator."""
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, script TEXT, seq INTEGER, interface TEXT, queue TEXT, sqe BLOB, mi_msg BLOB)")
    for name, cmds in tests.items():
        for i, (queue, s, m) in enumerate(cmds):
            con.execute("INSERT INTO commands(test_name,script,seq,interface,queue,sqe,mi_msg) VALUES(?,?,?,?,?,?,?)",
                        (name, "s.py", i, "mi" if m else "pcie", queue, s, m))
    con.commit()
    con.close()
    return str(path)


def load(conn, path):
    sid = cmddb.register_source(conn, path)
    cmddb.import_source(conn, sid)
    return sid


def exec_scen(conn, src):
    out = rows(conn, "SELECT * FROM scenario WHERE origin='exec' AND source_id=? ORDER BY id", (src,))
    for s in out:
        s["pred"], s["prov"] = jloads(s["predicate_json"]), jloads(s["provenance_json"])
        s["links"] = {r["item_key"]: r["state"] for r in rows(conn, "SELECT item_key,state FROM scenario_item WHERE scenario_id=?", (s["id"],))}
    return out


def item_state(conn, run, key):
    return conn.execute("SELECT state FROM item_result WHERE run_id=? AND item_key=?", (run, key)).fetchone()[0]


IDENT = lambda cns, cid=1, rsvd=0: ("admin", sqe(0x06, cid=cid, cdw10=(rsvd << 8) | cns), None)  # noqa: E731
READ = lambda slba, nlb, cid=1: ("io", sqe(0x02, nsid=1, cid=cid, cdw10=slba, cdw12=nlb), None)  # noqa: E731


def test_one_scenario_per_test_with_identical_flows_merged(conn, parsed_set, tmp_path):
    path = make_db(tmp_path / "x.db", {
        "t_a": [IDENT(1), IDENT(1, cid=2), *[READ(i * 8, 7, cid=i) for i in range(50)]],
        "t_b": [IDENT(1, cid=9), READ(4096, 0), READ(0, 0xFFFF)],            # same flow, other LBAs and lengths: merged with t_a
        "t_c": [IDENT(0), IDENT(1, rsvd=1), ("admin", sqe(0xC1), None)],      # reserved bits, an opcode the catalog does not know
        "t_mi": [("", None, mi_admin_msg(0x06, cdw10=1))],
    })
    src = load(conn, path)
    res = execscen.analyze_execution(conn, 1, src)
    assert (res["tests"], res["scenarios"], res["commands"], res["unknown_commands"]) == (4, 3, 59, 1)
    assert res["unknown"] == [{"interface": "pcie", "queue": "admin", "opcode": 0xC1, "commands": 1}]
    by_test = {t: s for s in exec_scen(conn, src) for t in s["prov"]["tests"]}

    ab = by_test["t_a"]
    assert by_test["t_b"] is ab and ab["pred"]["tests"] == ["t_a", "t_b"] and ab["pred"]["test_count"] == 2
    steps = ab["pred"]["steps"]
    assert [(st["name"], st["count"]) for st in steps] == [("Identify", 3), ("Read", 52)]          # runs collapse, counts add up
    assert steps[0]["fields"][0]["name"] == "CNS" and steps[0]["fields"][0]["value"] == 1
    nlb = next(r for r in steps[1]["ranges"] if r["name"] == "NLB")
    assert (nlb["min"], nlb["max"], sorted(nlb["values"])) == (0, 0xFFFF, [0, 7, 0xFFFF])
    assert ab["title"].startswith("t_a (+1 with the same flow): Identify (CNS 01h)")
    links = ab["links"]
    assert set(links.values()) == {"observed"}
    assert {"cmd:admin:06", "cmd:admin:06/dw10[07:00]=01h", "cmd:io:nvm:02"} <= set(links)
    nlb_key = next(k for k in links if k.startswith("cmd:io:nvm:02/dw12[15:00]") and k.endswith("=max"))
    assert nlb_key                                                    # the maximum NLB of t_b reached the boundary item
    assert "cmd:admin:06/dw10[07:00]=00h" not in links

    c = by_test["t_c"]
    assert "cmd:admin:06/dw10[15:08]:rsvd" in c["links"]
    assert [st["command"] for st in c["pred"]["steps"]] == ["cmd:admin:06", "cmd:admin:06", None]
    assert c["pred"]["steps"][2]["name"] == "Unknown Admin opcode C1h"
    assert "not in the catalog" in c["description"]

    mi = by_test["t_mi"]
    assert mi["pred"]["steps"][0]["tunnel"] and "cmd:admin:06/mi-tunnel" in mi["links"]


def test_long_logs_keep_their_identity_beyond_the_steps_kept(conn, parsed_set, tmp_path, monkeypatch):
    monkeypatch.setattr(execscen, "MAX_STEPS", 3)
    flow = [IDENT(0), IDENT(1), IDENT(2), IDENT(0)]
    path = make_db(tmp_path / "x.db", {"t1": flow, "t2": flow[:3] + [IDENT(1)]})   # same first three steps, different fourth
    src = load(conn, path)
    execscen.analyze_execution(conn, 1, src)
    sc = exec_scen(conn, src)
    assert len(sc) == 2 and all(s["pred"]["truncated"] and len(s["pred"]["steps"]) == 3 and s["prov"]["steps"] == 4 for s in sc)


def test_reanalysis_keeps_ids_descriptions_and_reviews(conn, parsed_set, tmp_path):
    path = make_db(tmp_path / "x.db", {"t1": [IDENT(1)], "t2": [IDENT(0), READ(0, 0)]})
    src = load(conn, path)
    execscen.analyze_execution(conn, 1, src)
    first = {s["prov"]["tests"][0]: s for s in exec_scen(conn, src)}
    s1 = first["t1"]["id"]
    conn.execute("UPDATE scenario SET title='My title' , provenance_json=json_set(provenance_json,'$.edited',1) WHERE id=?", (s1,))
    conn.execute("INSERT INTO scenario_item(scenario_id,item_key,state) VALUES(?, 'req:x', 'approved')", (s1,))
    conn.commit()
    # the DB grows: t1 is unchanged, t2 runs a different flow now
    make_db(tmp_path / "x.db", {"t1": [IDENT(1)], "t3": [IDENT(2)]})
    cmddb.import_source(conn, src)
    res = execscen.analyze_execution(conn, 1, src)
    assert (res["new"], res["updated"], res["removed"]) == (1, 1, 1)
    after = {s["prov"]["tests"][0]: s for s in exec_scen(conn, src)}
    assert set(after) == {"t1", "t3"} and after["t1"]["id"] == s1
    assert after["t1"]["title"] == "My title" and after["t1"]["links"]["req:x"] == "approved"
    assert after["t1"]["links"]["cmd:admin:06"] == "observed"


def test_coverage_reads_observations_and_reviewed_requirement_links(conn, parsed_set, tmp_path):
    conn.execute("DELETE FROM scenario")       # no spec-driven scenarios: coverage comes from the log alone
    conn.commit()
    src = load(conn, make_db(tmp_path / "x.db", {"t1": [IDENT(1, rsvd=1)], "t2": [READ(0, 7)]}))
    run = coverage.run_coverage(conn, 1, src)
    assert item_state(conn, run, "cmd:admin:06") == "covered"
    assert item_state(conn, run, "cmd:admin:06/dw10[07:00]=01h") == "covered"
    assert item_state(conn, run, "cmd:admin:06/dw10[07:00]=00h") == "not_covered"       # analysed, never executed
    assert item_state(conn, run, "cmd:admin:06/dw10[07:00]") == "partial"               # some of its values
    assert item_state(conn, run, "cmd:admin:06/dw10[15:08]:rsvd") == "stimulus_covered"
    assert item_state(conn, run, "cmd:admin:09") == "not_covered"
    summary = json.loads(conn.execute("SELECT summary_json FROM coverage_run WHERE id=?", (run,)).fetchone()[0])
    assert summary["execution"]["tests"] == 2 and summary["by_kind_all"]["value"]["covered"] >= 2

    req = next(r for r in rows(conn, "SELECT key FROM catalog_item WHERE kind='requirement' AND observability='stimulus' AND parent_key='cmd:admin:06'"))["key"]
    sid = next(s["id"] for s in exec_scen(conn, src) if s["prov"]["tests"] == ["t1"])
    assert item_state(conn, run, req) == "no_scenario"
    conn.execute("INSERT INTO scenario_item(scenario_id,item_key,state) VALUES(?,?, 'proposed')", (sid, req))
    conn.commit()
    assert item_state(conn, coverage.run_coverage(conn, 1, src), req) == "needs_review"
    execscen.set_link(conn, sid, req, "approved")
    run = coverage.run_coverage(conn, 1, src)
    assert item_state(conn, run, req) == "stimulus_covered"
    execscen.set_link(conn, sid, req, "rejected")
    assert item_state(conn, coverage.run_coverage(conn, 1, src), req) == "no_scenario"
    with pytest.raises(ValueError):
        execscen.set_link(conn, sid, "cmd:admin:06", "rejected")           # observations are facts

    ds = reports.build_dataset(conn, run)
    assert ds["header"]["scenarios_executed"] == 2 and {x["tests"][0] for x in ds["executed"]} == {"t1", "t2"}
    for fmt in ("xlsx", "html", "json"):
        assert reports.export(conn, run, fmt)[0]
    assert b"t1" in reports.to_csv(ds, "executed")


def test_another_command_dbs_executions_do_not_count(conn, parsed_set, tmp_path):
    conn.execute("DELETE FROM scenario")
    conn.commit()
    a = load(conn, make_db(tmp_path / "a.db", {"t": [IDENT(1)]}))
    b = load(conn, make_db(tmp_path / "b.db", {"t": [READ(0, 0)]}))
    execscen.analyze_execution(conn, 1, a)
    run = coverage.run_coverage(conn, 1, b)
    assert item_state(conn, run, "cmd:admin:06") == "not_covered" and item_state(conn, run, "cmd:io:nvm:02") == "covered"


def test_describe_proposes_only_shown_requirements(conn, parsed_set, tmp_path):
    src = load(conn, make_db(tmp_path / "x.db", {"t1": [IDENT(1), IDENT(1, rsvd=3)]}))
    execscen.analyze_execution(conn, 1, src)
    seen = {}

    def reply(messages):
        user = messages[-1]["content"]
        seen["user"] = user
        assert "Identify (Admin 06h)" in user and "CNS=01h" in user and "reserved 10[15:8]=03h" in user
        return json.dumps({"title": "Identify controller, then with reserved bits set", "description": "Reads the controller data structure.",
                           "requirements": [{"id": "R1", "why": "step 2"}, {"id": "R999", "why": "made up"}]})
    stats = execscen.describe_scenarios(conn, 1, src, LLMService(conn, settings(), Fake([reply])))
    assert stats["described"] == 1 and stats["proposed"] == 1 and stats["dropped_ids"] == 1
    s = exec_scen(conn, src)[0]
    assert s["title"] == "Identify controller, then with reserved bits set" and s["prov"]["ai"]["model"] == "fake-model"
    proposed = [k for k, v in s["links"].items() if v == "proposed"]
    kind = conn.execute("SELECT kind FROM catalog_item WHERE key=?", (proposed[0],)).fetchone()[0] if proposed else None
    assert len(proposed) == 1 and kind == "requirement" and "[R1]" in seen["user"]
    # without redo, described scenarios are skipped; re-analysis keeps the AI text
    assert execscen.describe_scenarios(conn, 1, src, LLMService(conn, settings(), Fake([reply])))["scenarios"] == 0
    execscen.analyze_execution(conn, 1, src)
    assert exec_scen(conn, src)[0]["title"] == "Identify controller, then with reserved bits set"


def test_api_flow(ws, conn, parsed_set, tmp_path):
    from specforge.api.app import create_app

    path = make_db(tmp_path / "x.db", {"t1": [IDENT(1)], "t2": [READ(0, 0)]})
    with TestClient(create_app()) as client:
        src = client.post("/api/cmddb", json={"path": path}).json()["id"]
        assert client.post("/api/sets/1/exec/analyze", json={"source_id": src}).status_code == 409        # not imported yet
        _wait(client, client.post(f"/api/cmddb/{src}/import").json()["job"])
        assert client.post("/api/sets/1/exec/describe", json={"source_id": src}).status_code == 409       # not analysed yet
        job = _wait(client, client.post("/api/sets/1/exec/analyze", json={"source_id": src}).json()["job"])
        assert job["state"] == "done" and job["result"]["scenarios"] == 2
        assert client.get("/api/sets/1/exec").json()[0]["tests"] == 2
        lst = client.get("/api/sets/1/scenarios", params={"origin": "exec", "source_id": src}).json()
        assert lst["total"] == 2 and all(s["state"] == "observed" and s["link_counts"]["observed"] for s in lst["scenarios"])
        sid = lst["scenarios"][0]["id"]
        assert client.patch(f"/api/scenarios/{sid}", json={"state": "approved"}).status_code == 422
        assert client.patch(f"/api/scenarios/{sid}", json={"title": "Renamed"}).json()["ok"]
        assert client.post("/api/scenarios/bulk", json={"ids": [sid], "state": "approved"}).json()["refused"]
        links = client.get(f"/api/scenarios/{sid}/links").json()
        assert links and all(l["state"] == "observed" and l["kind"] for l in links)
        assert client.put(f"/api/scenarios/{sid}/links", json={"item_key": links[0]["key"], "state": "rejected"}).status_code == 422
        conn.execute("INSERT INTO scenario_item(scenario_id,item_key,state) VALUES(?, 'req:y', 'proposed')", (sid,))
        conn.commit()
        assert client.get("/api/sets/1/scenarios", params={"links": "proposed"}).json()["total"] == 1
        assert client.post("/api/scenarios/links/bulk", json={"ids": [sid], "state": "approved"}).json()["updated"] == 1
        run = _wait(client, client.post("/api/coverage/run", json={"set_id": 1, "source_id": src}).json()["job"])["result"]["run_id"]
        ev = client.get(f"/api/coverage/{run}/evidence", params={"item": "cmd:io:nvm:02"}).json()
        assert any(e["sid"].startswith("exec:") and e["evidence"][0]["test_id"] == "t2" for e in ev)
        assert client.get(f"/api/coverage/{run}/export", params={"format": "csv", "which": "executed"}).status_code == 200
        assert next(s for s in client.get("/api/sets/1/scenarios", params={"origin": "exec"}).json()["scenarios"] if s["id"] == sid)["title"] == "Renamed"


def _wait(client, j):
    import time

    for _ in range(600):
        r = client.get(f"/api/jobs/{j}").json()
        if r["state"] in ("done", "failed", "cancelled"):
            return r
        time.sleep(0.05)
    raise TimeoutError


def test_an_older_workspace_is_migrated(tmp_path):
    from specforge.core.db import connect

    p = tmp_path / "old.db"
    old = sqlite3.connect(p)
    old.execute("CREATE TABLE scenario(id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL, sid TEXT NOT NULL, origin TEXT NOT NULL, kind TEXT, "
                "title TEXT, description TEXT, observability TEXT NOT NULL, predicate_json TEXT NOT NULL, state TEXT NOT NULL, provenance_json TEXT, "
                "created TEXT, UNIQUE(set_id, sid))")
    old.execute("CREATE TABLE scenario_item(scenario_id INTEGER NOT NULL, item_key TEXT NOT NULL, PRIMARY KEY(scenario_id, item_key))")
    old.execute("INSERT INTO scenario(set_id,sid,origin,observability,predicate_json,state) VALUES(1,'a','cross','submission','{}','draft')")
    old.execute("INSERT INTO scenario_item VALUES(1,'cmd:admin:06')")
    old.commit()
    old.close()
    c = connect(p)
    assert {"source_id"} <= {r[1] for r in c.execute("PRAGMA table_info(scenario)")}
    assert {"state", "detail_json"} <= {r[1] for r in c.execute("PRAGMA table_info(scenario_item)")}
    assert [tuple(r) for r in c.execute("SELECT item_key, state FROM scenario_item")] == [("cmd:admin:06", None)]
    c.close()
