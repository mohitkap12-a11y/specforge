"""Section-driven cross-command scenario generation: scoping, connected passages, validation and review gating."""
import json

import pytest
from fastapi.testclient import TestClient
from llm_helpers import Fake, settings

from specforge import crossgen
from specforge.core.db import jloads, rows
from specforge.llm.service import LLMService

SETF, GETF = "cmd:admin:09", "cmd:admin:0A"


def _sec(conn, title):
    r = next(r for r in rows(conn, "SELECT key,label,attrs_json FROM catalog_item WHERE kind='command'") if r["label"] == title)
    return jloads(r["attrs_json"], {})["section"]


def _flow(quote, **over):
    a = {"command": SETF, "note": "setup", "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 4}], "capture": {"fid": {"dw": 10, "hi": 7, "lo": 0}}}
    b = {"command": GETF, "note": "check", "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": {"var": "fid"}}]}
    return {"title": "Set then Get the same feature", "observability": "stimulus", "steps": [a, b], "quotes": [quote], **over}


def _svc(conn, scenarios):
    return LLMService(conn, settings(), Fake([json.dumps({"testable": True, "scenarios": scenarios})]))


def test_sections_listing_and_command_selection(conn, parsed_set):
    out = crossgen.list_sections(conn, 1, "features")
    titles = {s["title"] for s in out["sections"]}
    assert {"5.4 Set Features command"} <= titles or any("Set Features" in t for t in titles)
    assert any(c["key"] == SETF for s in out["sections"] for c in s["commands"])
    units = crossgen.build_units(conn, 1, command_keys=[SETF])
    assert len(units) == 1 and SETF in units[0]["own_commands"] and units[0]["section_key"] == _sec(conn, "Set Features")


def test_connected_passages_come_from_references_anywhere_in_the_set(conn, parsed_set):
    (u,) = crossgen.build_units(conn, 1, command_keys=["cmd:admin:06"])             # the section references a figure and another spec
    got = {r["key"]: r["why"] for r in u["related"]}
    assert "referenced from the section" in got["nvme-nvm-cs@1.3/sec:3.1"]               # forward reference into another spec
    assert "refers to the section" in got["nvme-mi@2.0/sec:1"]                           # reverse reference from a third spec
    assert u["section_key"] not in got
    assert len(u["primary"]) <= crossgen.PRIMARY_CHARS and sum(len(r["text"]) for r in u["related"]) <= crossgen.RELATED_CHARS


def test_sections_selected_together_see_each_others_commands(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    assert len(units) == 2
    assert all({SETF, GETF} <= set(u["commands"]) for u in units)
    assert any("selected together" in r["why"] for r in units[0]["related"])


def test_bad_selection_is_rejected(conn, parsed_set):
    for kw in ({}, {"section_keys": ["nope"]}, {"command_keys": ["cmd:admin:EE"]}):
        with pytest.raises(ValueError):
            crossgen.build_units(conn, 1, **kw)


def test_generation_creates_validated_linked_drafts_and_rejects_the_rest(conn, parsed_set):
    sec = _sec(conn, "Set Features")
    assert sec in {u_["section_key"] for u_ in crossgen.build_units(conn, 1, section_keys=[sec])}
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    quote = next(x for x in units[0]["primary"].split("\n") if "Set Features command configures" in x or "configures a feature" in x)[:60]
    single = {**_flow(quote), "title": "one command only", "steps": [_flow(quote)["steps"][0]]}
    ungrounded = {**_flow(quote), "title": "invented", "quotes": ["the device shall levitate above the rack"]}
    outside = _flow(quote)
    outside["title"] = "bad bits"
    outside["steps"][0]["fields"] = [{"dw": 12, "hi": 3, "lo": 0, "op": "eq", "value": 1}]
    stats = crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote), single, ungrounded, outside]), command_keys=[SETF, GETF])
    assert stats["sections"] == 2 and stats["created"] >= 1 and stats["rejected"] >= 3
    sc = rows(conn, "SELECT * FROM scenario WHERE origin='cross'")
    assert sc and all(s["state"] == "draft" and s["kind"] == "cross" for s in sc)
    pred = json.loads(sc[0]["predicate_json"])
    assert [s["opcode"] for s in pred["steps"]] == [9, 10] and pred["steps"][0]["note"] == "setup"      # opcodes from the catalog
    prov = json.loads(sc[0]["provenance_json"])
    assert prov["section"] in {u_["section_key"] for u_ in units} and prov["quotes"]
    linked = {r[0] for r in conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=?", (sc[0]["id"],))}
    assert {SETF, GETF} <= linked


def test_regeneration_replaces_unreviewed_drafts_but_never_approved_ones(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    quote = next(x for x in units[0]["primary"].split("\n") if "configures a feature" in x)[:60]
    crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote)]), command_keys=[SETF, GETF])
    first = rows(conn, "SELECT id,sid FROM scenario WHERE origin='cross'")
    assert len(first) == 1
    conn.execute("UPDATE scenario SET state='approved' WHERE id=?", (first[0]["id"],))
    conn.commit()
    other = _flow(quote, title="different")
    other["steps"][0]["fields"][0]["value"] = 7
    crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [other]), command_keys=[SETF, GETF], focus="another angle")       # a new prompt, so no cached answer
    now = rows(conn, "SELECT id,state FROM scenario WHERE origin='cross' ORDER BY id")
    assert [r["state"] for r in now] == ["approved", "draft"] and now[0]["id"] == first[0]["id"]


def test_not_testable_and_model_failure_are_recorded(conn, parsed_set):
    svc = LLMService(conn, settings(), Fake([json.dumps({"testable": False, "reason": "single command"})]))
    assert crossgen.generate_cross_scenarios(conn, 1, svc, command_keys=[SETF])["not_testable"] == 1
    svc = LLMService(conn, settings(), Fake(["not json"]))
    assert crossgen.generate_cross_scenarios(conn, 1, svc, command_keys=[SETF], focus="retry")["failed"] == 1
    assert {r["state"] for r in rows(conn, "SELECT state FROM enrichment WHERE task='cross'")} == {"not_testable", "failed"}


def test_api_selection_sections_plan_and_validation(ws, demo_dir):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        docs = [client.post("/api/specs/upload", files=[("files", (n, open(demo_dir / n, "rb"), "application/pdf"))]).json()[0]["id"]
                for n in ("nvme-base-2.4.pdf", "nvme-nvm-cs-1.3.pdf")]
        import time
        def wait(j):
            for _ in range(600):
                r = client.get(f"/api/jobs/{j}").json()
                if r["state"] in ("done", "error", "failed"):
                    return r
                time.sleep(0.1)
        for d in docs:
            wait(client.post(f"/api/specs/{d}/parse").json()["job"])
        sid = client.post("/api/sets", json={"name": "s", "document_ids": docs}).json()["id"]
        assert wait(client.post(f"/api/sets/{sid}/catalog/build").json()["job"])["state"] == "done"
        assert client.get(f"/api/sets/{sid}/scenarios", params={"origin": "template"}).json()["total"] == 0       # no single-command templates
        secs = client.get(f"/api/sets/{sid}/sections", params={"q": "Features"}).json()
        assert secs["total"] >= 2
        plan = client.post(f"/api/sets/{sid}/scenarios/cross/plan", json={"command_keys": ["cmd:admin:06"]}).json()["units"]
        assert plan[0]["related"] and plan[0]["own_commands"] == ["cmd:admin:06"]
        for body in ({}, {"section_keys": []}, {"section_keys": "x"}, {"command_keys": [SETF], "max_per_section": 0},
                     {"command_keys": [SETF], "focus": 3}, {"command_keys": ["cmd:admin:EE"]}):
            assert client.post(f"/api/sets/{sid}/scenarios/cross", json=body).status_code == 422, body
        assert not [j for j in client.get("/api/jobs").json() if j["type"] == "cross-scenarios"]      # rejected selections queue nothing
