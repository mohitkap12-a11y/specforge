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
    assert crossgen.generate_cross_scenarios(conn, 1, svc, command_keys=[SETF], passes=1)["not_testable"] == 1
    svc = LLMService(conn, settings(), Fake(["not json"]))
    assert crossgen.generate_cross_scenarios(conn, 1, svc, command_keys=[SETF], focus="retry", passes=1)["failed"] == 1
    assert {r["state"] for r in rows(conn, "SELECT state FROM enrichment WHERE task LIKE 'cross%'")} == {"not_testable", "failed"}


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
                     {"command_keys": [SETF], "focus": 3}, {"command_keys": ["cmd:admin:EE"]},
                     {"command_keys": [SETF], "passes": 0}, {"command_keys": [SETF], "passes": 9}, {"command_keys": [SETF], "passes": "3"}):
            assert client.post(f"/api/sets/{sid}/scenarios/cross", json=body).status_code == 422, body
        angles = client.get("/api/cross/angles").json()
        assert angles["default"] == crossgen.DEFAULT_PASSES and [a["id"] for a in angles["angles"]] == [a["id"] for a in crossgen.ANGLES]
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
        msgs, _, _ = crossgen._messages(conn, 1, u, "", 6, sel)
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


def test_an_oversized_selection_is_rejected_not_silently_truncated(conn, parsed_set, monkeypatch):
    sel = ["cmd:admin:09", "cmd:admin:0A", "cmd:admin:06", "cmd:admin:02"]
    monkeypatch.setattr(crossgen, "MAX_COMMANDS", 3)
    with pytest.raises(ValueError, match="at most 3"):
        crossgen.build_units(conn, 1, command_keys=sel)
    units = crossgen.build_units(conn, 1, command_keys=sel[:3])                 # exactly the limit: all of them stay in every scope
    assert all(set(sel[:3]) <= set(u["commands"]) for u in units)
    with pytest.raises(ValueError, match="at most 3"):                          # commands of a selected section count too
        crossgen.build_units(conn, 1, command_keys=sel[:3], section_keys=[_sec(conn, "Get Log Page")])


def test_two_revisions_of_one_family_can_be_told_apart(ws, tmp_path):
    import time

    from specforge.api.app import create_app
    from specforge.devtools.synth import base_spec, write_pdf

    def blocks(rev):
        return [("title", "NVM Express® Base Specification", f"Revision {rev}"), *base_spec()[1:]]

    with TestClient(create_app()) as client:
        ids = []
        for rev in ("2.3", "2.4"):
            p = tmp_path / f"base-{rev}.pdf"
            write_pdf(p, blocks(rev), header=rev)
            ids.append(client.post("/api/specs/upload", files=[("files", (p.name, p.read_bytes(), "application/pdf"))]).json()[0]["id"])

        def wait(j):
            for _ in range(600):
                r = client.get(f"/api/jobs/{j}").json()
                if r["state"] in ("done", "error", "failed"):
                    return r
                time.sleep(0.1)
        for d in ids:
            wait(client.post(f"/api/specs/{d}/parse").json()["job"])
        sid = client.post("/api/sets", json={"name": "revs", "document_ids": ids}).json()["id"]
        wait(client.post(f"/api/sets/{sid}/catalog/build").json()["job"])
        both = client.get(f"/api/sets/{sid}/sections", params={"family": "nvme-base", "limit": 2000}).json()
        assert {(s["family"], s["version"]) for s in both["specs"]} == {("nvme-base", "2.3"), ("nvme-base", "2.4")}
        assert {s["version"] for s in both["sections"]} == {"2.3", "2.4"}               # family alone cannot tell the revisions apart
        only = client.get(f"/api/sets/{sid}/sections", params={"family": "nvme-base", "version": "2.3", "limit": 2000}).json()
        assert only["sections"] and {s["version"] for s in only["sections"]} == {"2.3"} and only["total"] < both["total"]
        by_version = {v: client.get(f"/api/sets/{sid}/catalog", params={"kind": "command", "family": "nvme-base", "version": v, "limit": 2000}).json()
                      for v in ("2.3", "2.4")}
        allc = client.get(f"/api/sets/{sid}/catalog", params={"kind": "command", "family": "nvme-base", "limit": 2000}).json()
        assert allc["total"] > 0 and by_version["2.3"]["total"] + by_version["2.4"]["total"] == allc["total"]      # the version filter partitions the family
        for v, r in by_version.items():
            assert {i["attrs"]["version"] for i in r["items"]} <= {v}                                             # (the catalog keeps one command per opcode across revisions)


class _ByAngle:
    """A model whose answer depends on the ANGLE it is asked for; records every prompt."""
    name = "by-angle"

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def generate(self, messages, **kw):
        text = messages[-1]["content"]
        self.prompts.append(text)
        angle = next((a for a in crossgen.ANGLES if f"ANGLE for this pass - {a['title']}" in text), None)
        return json.dumps(self.answers.get(angle["id"] if angle else None, {"testable": False, "reason": "nothing"}))

    def list_models(self): return []
    def health(self): return {"ok": True}
    def embed(self, t, **kw): return []


def _ev_flow(ev_ids, title, **over):
    f = _flow("unused", title=title, **over)
    f["quotes"] = []
    f["evidence"] = ev_ids
    return f


def test_each_pass_asks_for_a_different_kind_of_scenario_and_the_results_add_up(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    _, _, ev = crossgen._messages(conn, 1, units[0], "", 6)
    first = sorted(ev, key=lambda k: int(k[1:]))[0]
    mk = lambda title, fid: {**_ev_flow([first], title), "steps": [{**_flow("x")["steps"][0], "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": fid}]}, _flow("x")["steps"][1]]}
    model = _ByAngle({"explicit": {"testable": True, "scenarios": [mk("explicit one", 1)]},
                      "fields": {"testable": True, "scenarios": [mk("field one", 2), mk("field two", 3)]},
                      "negative": {"testable": True, "scenarios": [mk("negative one", 4)]}})
    svc = LLMService(conn, settings(), model)
    stats = crossgen.generate_cross_scenarios(conn, 1, svc, command_keys=[SETF, GETF], passes=3)
    assert len(model.prompts) == 2 * 3                                        # two sections x three passes
    assert {a["title"] for a in crossgen.ANGLES[:3]} <= {t.split(" - ")[1].split(":")[0] for p_ in model.prompts for t in p_.split("\n") if t.startswith("ANGLE for this pass - ")}
    assert stats["created"] == 4                                              # the same answers from the second section are duplicates
    angles = {json.loads(r["provenance_json"])["angle"] for r in rows(conn, "SELECT provenance_json FROM scenario WHERE origin='cross'")}
    assert angles == {"explicit", "fields", "negative"}
    assert {r["task"] for r in rows(conn, "SELECT task FROM enrichment")} == {"cross:explicit", "cross:fields", "cross:negative"}


def test_the_prompt_numbers_every_sentence_and_evidence_ids_resolve_to_it(conn, parsed_set):
    (u, _) = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    msgs, corpus, ev = crossgen._messages(conn, 1, u, "", 6, None, crossgen.ANGLES[1])
    text = msgs[1]["content"]
    assert "[S1] " in text and "ANGLE for this pass - " + crossgen.ANGLES[1]["title"] in text and "Write up to 6" in text
    assert all(f"[{k}] " in text for k in list(ev)[:5])
    some = next(iter(ev))
    good = crossgen._evidence(crossgen.AIScenario(title="t", observability="stimulus", steps=[], evidence=[f"[{some}]"]), ev, corpus)
    assert good == ([ev[some]], [])                                           # brackets and case are tolerated
    bad = crossgen._evidence(crossgen.AIScenario(title="t", observability="stimulus", steps=[], evidence=["S99999"]), ev, corpus)
    assert bad[0] == [] and "unknown evidence id" in bad[1][0]
    none = crossgen._evidence(crossgen.AIScenario(title="t", observability="stimulus", steps=[]), ev, corpus)
    assert none == ([], ["no evidence cited"])


def test_cited_sentence_ids_ground_a_draft_and_unknown_ids_reject_it(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    _, _, ev = crossgen._messages(conn, 1, units[0], "", 6)
    first = sorted(ev, key=lambda k: int(k[1:]))[0]
    answer = {"testable": True, "scenarios": [_ev_flow([first], "cited"), _ev_flow(["S99999"], "invented id")]}
    stats = crossgen.generate_cross_scenarios(conn, 1, _svc(conn, answer["scenarios"]), command_keys=[SETF, GETF], passes=1)
    assert stats["created"] == 1 and stats["rejected"] >= 1
    prov = json.loads(rows(conn, "SELECT provenance_json FROM scenario WHERE origin='cross'")[0]["provenance_json"])
    assert prov["quotes"] == [ev[first]] and prov["angle"] == "explicit"       # the sentence itself is stored as the quote


def test_a_varied_field_is_expanded_from_the_catalog_values(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=["cmd:io:nvm:01", "cmd:io:nvm:02"])
    _, _, ev = crossgen._messages(conn, 1, units[0], "", 6)
    first = sorted(ev, key=lambda k: int(k[1:]))[0]
    wr = {"command": "cmd:io:nvm:01", "note": "write with FUA", "fields": [{"dw": 12, "hi": 30, "lo": 30, "op": "eq", "value": 1, "vary": True}]}
    rd = {"command": "cmd:io:nvm:02", "note": "read back", "fields": []}
    sc = {"title": "FUA write then read", "observability": "stimulus", "steps": [wr, rd], "evidence": [first]}
    stats = crossgen.generate_cross_scenarios(conn, 1, _svc(conn, [sc]), command_keys=["cmd:io:nvm:01", "cmd:io:nvm:02"], passes=1)
    assert stats["created"] == 2 and stats["variants"] == 1                    # FUA=1 as asked, plus FUA=0 from the catalog
    got = rows(conn, "SELECT title,predicate_json,provenance_json FROM scenario WHERE origin='cross' ORDER BY id")
    values = {json.loads(r["predicate_json"])["steps"][0]["fields"][0]["value"] for r in got}
    assert values == {0, 1} and any(r["title"].endswith("[FUA=0]") for r in got)
    assert all("vary" not in json.dumps(json.loads(r["predicate_json"])) for r in got)    # the marker is not part of the predicate


def test_the_per_section_cap_stops_a_runaway_section(conn, parsed_set, monkeypatch):
    monkeypatch.setattr(crossgen, "MAX_PER_SECTION", 1)
    units = crossgen.build_units(conn, 1, command_keys=[SETF, GETF])
    _, _, ev = crossgen._messages(conn, 1, units[0], "", 6)
    first = sorted(ev, key=lambda k: int(k[1:]))[0]
    sc = [{**_ev_flow([first], f"s{i}"), "steps": [{**_flow("x")["steps"][0], "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": i}]}, _flow("x")["steps"][1]]} for i in range(1, 4)]
    crossgen.generate_cross_scenarios(conn, 1, _svc(conn, sc), command_keys=[SETF, GETF], passes=1)
    per_section = [json.loads(r["provenance_json"])["section"] for r in rows(conn, "SELECT provenance_json FROM scenario WHERE origin='cross'")]
    assert per_section and max(per_section.count(k) for k in set(per_section)) == 1                # three were offered; one is kept per section


def test_passes_are_validated(conn, parsed_set):
    for bad in (0, 5, True, "3"):
        with pytest.raises(ValueError, match="passes"):
            crossgen.generate_cross_scenarios(conn, 1, _svc(conn, []), command_keys=[SETF], passes=bad)


def test_long_sentences_are_split_into_pieces_not_truncated():
    long = " ".join(f"word{i}" for i in range(300))                            # about 2,000 characters, as long field descriptions are
    text, ev, last = crossgen._numbered(f"Short rule. {long} The final condition matters.", 0, cap=500)
    assert last == len(ev) and all(len(v) <= 500 for v in ev.values())
    joined = " ".join(ev.values())
    assert "word0" in joined and "word299" in joined and "The final condition matters." in joined       # the remainder is kept
    assert " ".join(v for v in ev.values() if v.startswith("word") or "word" in v).count("word") >= 300
    assert "".join(crossgen._chunks("x" * 1200, 500)) == "x" * 1200                                      # even one unbroken token survives
    assert all(f"[{k}] " in text for k in ev)


def test_variants_are_generated_even_when_the_base_flow_already_exists(conn, parsed_set):
    units = crossgen.build_units(conn, 1, command_keys=["cmd:io:nvm:01", "cmd:io:nvm:02"])
    _, _, ev = crossgen._messages(conn, 1, units[0], "", 6)
    first = sorted(ev, key=lambda k: int(k[1:]))[0]

    def flow(vary):
        wr = {"command": "cmd:io:nvm:01", "note": "write with FUA", "fields": [{"dw": 12, "hi": 30, "lo": 30, "op": "eq", "value": 1, "vary": vary}]}
        rd = {"command": "cmd:io:nvm:02", "note": "read back", "fields": []}
        return {"title": "FUA write then read", "observability": "stimulus", "steps": [wr, rd], "evidence": [first]}

    # pass 1 states the flow plainly; pass 2 restates the same flow only to have FUA swept over the catalog's values
    model = _ByAngle({"explicit": {"testable": True, "scenarios": [flow(False)]}, "fields": {"testable": True, "scenarios": [flow(True)]}})
    stats = crossgen.generate_cross_scenarios(conn, 1, LLMService(conn, settings(), model), command_keys=["cmd:io:nvm:01", "cmd:io:nvm:02"], passes=2)
    assert stats["variants"] >= 1 and stats["duplicates"] >= 1                          # the base was a duplicate, the variant was still made
    values = {json.loads(r["predicate_json"])["steps"][0]["fields"][0]["value"] for r in rows(conn, "SELECT predicate_json FROM scenario WHERE origin='cross'")}
    assert values == {0, 1}
