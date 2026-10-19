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


def test_grounding_accepts_connected_text_and_rejects_text_from_unrelated_sections(conn, parsed_set):
    (u,) = crossgen.build_units(conn, 1, command_keys=["cmd:admin:06"])
    related = next(r for r in u["related"] if r["key"] == "nvme-nvm-cs@1.3/sec:3.1")
    quote = next(x for x in related["text"].split("\n") if "NVM command set" in x or "opcodes" in x)[:60]
    flow = _flow(quote)
    flow["steps"][0]["command"], flow["steps"][1]["command"] = "cmd:admin:06", "cmd:admin:06"
    flow["steps"][0]["fields"] = [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 1}]
    flow["steps"][0]["capture"] = {}
    flow["steps"][1]["fields"] = [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 2}]
    # two different commands are needed, so only the grounding is under test here: an unrelated section's sentence is not allowed
    assert crossgen._grounded(quote, u["primary"] + "\n" + "\n".join(r["text"] for r in u["related"]))
    assert not crossgen._grounded("The Write command writes data and metadata to the namespace.", u["primary"] + "\n" + related["text"])


def test_invalid_within_is_rejected_not_treated_as_unlimited(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    quote = next(x for x in units[0]["primary"].split("\n") if "configures a feature" in x)[:60]
    stats = crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote, within=0)]), command_keys=[SETF, GETF])
    assert stats["created"] == 0 and stats["rejected"] >= 1


def test_a_rejected_scenario_is_not_resurrected_by_regeneration(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    quote = next(x for x in units[0]["primary"].split("\n") if "configures a feature" in x)[:60]
    crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote)]), command_keys=[SETF, GETF])
    conn.execute("UPDATE scenario SET state='rejected' WHERE origin='cross'")
    conn.commit()
    stats = crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote)]), command_keys=[SETF, GETF], focus="again")
    assert stats["created"] == 0 and stats["duplicates"] >= 1
    assert {r["state"] for r in rows(conn, "SELECT state FROM scenario WHERE origin='cross'")} == {"rejected"}


def test_cancellation_is_checked_even_when_the_model_fails(conn, parsed_set):
    from specforge.llm.base import LLMError

    calls, seen = [], []

    def boom(messages):
        calls.append(1)
        raise LLMError("model unavailable")

    st = {**settings(), "llm": {"max_retries": 0}}        # a fresh dict: the defaults are shared between tests

    def progress(done, total):
        seen.append(done)
        if done >= 1:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError):
        crossgen.generate_cross_scenarios(conn, 1, LLMService(conn, st, Fake([boom])), command_keys=[SETF, GETF], progress=progress)
    assert len(calls) == 1 and seen == [0, 1]


def test_the_agent_narrates_each_decision(conn, parsed_set):
    from specforge import eventlog

    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    quote = next(x for x in units[0]["primary"].split("\n") if "configures a feature" in x)[:60]
    bad = {**_flow(quote), "title": "bad quote", "quotes": ["the device shall levitate above the rack"]}
    crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [_flow(quote), bad]), command_keys=[SETF, GETF])
    assert eventlog.drain(10)
    msgs = [e["message"] for e in eventlog.query(conn, limit=2000, sources=["agent"])]
    assert msgs[0].startswith("Cross-command agent starting") and "[1/2]" in msgs[1] and "reading" in msgs[1]
    assert any("rejected draft 'bad quote'" in m and "ungrounded" in m for m in msgs)
    assert any("drafted" in m for m in msgs) and msgs[-1].startswith("Cross-command agent finished")


def test_a_large_selection_keeps_every_selected_command_visible_to_the_model(conn, parsed_set):
    sel = ["cmd:admin:09", "cmd:admin:0A", "cmd:admin:06", "cmd:admin:02", "cmd:io:nvm:02", "cmd:io:nvm:01"]       # six sections, two specs
    units = crossgen.build_units(conn, 1, command_keys=sel)
    assert len(units) > 5                                                 # more sections than the old 'small group' limit
    for u in units:
        assert set(sel) <= set(u["commands"]), (u["title"], set(sel) - set(u["commands"]))
        msgs, _ = crossgen._messages(conn, 1, u, "", 6, sel)
        text = msgs[1]["content"]
        assert all(f'"command":"{k}"' in text for k in sel)              # every command has its own share of the catalog text
        assert "SELECTED TOGETHER" in text


def test_catalog_text_gives_each_command_a_share_and_never_cuts_a_line(conn, parsed_set, monkeypatch):
    monkeypatch.setattr(crossgen, "CATALOG_CHARS", 1500)
    monkeypatch.setattr(crossgen, "MIN_PER_COMMAND", 200)
    cmds = ["cmd:admin:09", "cmd:admin:06", "cmd:admin:0A"]
    text = crossgen._catalog_text(conn, 1, cmds)
    lines = text.split("\n")
    assert all(json.loads(ln) for ln in lines)                           # every line is whole JSON
    assert all(any(f'"command":"{k}"' in ln for ln in lines) for k in cmds)
    assert any("omitted for space" in ln for ln in lines)                # truncation is announced, not silent


def test_sections_and_catalog_can_be_paged_and_filtered_by_spec(ws, demo_dir):
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
        sid = client.post("/api/sets", json={"name": "two", "document_ids": docs}).json()["id"]
        assert wait(client.post(f"/api/sets/{sid}/catalog/build").json()["job"])["state"] == "done"
        allsec = client.get(f"/api/sets/{sid}/sections", params={"limit": 5}).json()
        assert len(allsec["sections"]) == 5 and allsec["total"] > 5
        assert {s["family"] for s in allsec["specs"]} == {"nvme-base", "nvme-nvm-cs"} and all(s["sections"] > 0 for s in allsec["specs"])
        cs = client.get(f"/api/sets/{sid}/sections", params={"family": "nvme-nvm-cs", "limit": 500}).json()
        assert cs["sections"] and {s["family"] for s in cs["sections"]} == {"nvme-nvm-cs"}
        page2 = client.get(f"/api/sets/{sid}/sections", params={"limit": 5, "offset": 5}).json()["sections"]
        assert page2 and page2[0]["key"] != allsec["sections"][0]["key"]
        cmds = client.get(f"/api/sets/{sid}/catalog", params={"kind": "command", "family": "nvme-nvm-cs"}).json()
        assert cmds["total"] > 0 and {i["attrs"]["family"] for i in cmds["items"]} == {"nvme-nvm-cs"}
