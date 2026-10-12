"""Cross-command workflow scenarios: generated from the catalog, validated, matched as sequences with captured variables."""

import json

from specforge import cmddb, coverage, scenarios
from specforge.core.db import jloads, rows
from specforge.workflows import RECIPES, build_workflow_items


def _wf(conn):
    return {r["key"]: jloads(r["attrs_json"], {}) for r in rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=1 AND kind='workflow'")}


def test_workflows_are_catalog_items_with_valid_multi_step_scenarios(conn, parsed_set):
    wf = _wf(conn)
    assert len(wf) >= 20 and all(len(a["predicate"]["steps"]) >= 2 for a in wf.values())
    cmds, fields = scenarios.catalog_context(conn, 1)
    for k, a in wf.items():
        assert scenarios.validate_predicate(a["predicate"], cmds, fields) == [], k
    sc = rows(conn, "SELECT * FROM scenario WHERE set_id=1 AND kind='workflow'")
    assert len(sc) == len(wf) and all(s["origin"] == "template" and s["state"] == "approved" for s in sc)
    # a workflow is linked to its own catalog item only: it must not drag the single-command items into "partial"
    links = rows(conn, "SELECT si.item_key FROM scenario_item si JOIN scenario s ON s.id=si.scenario_id WHERE s.kind='workflow'")
    assert {r["item_key"] for r in links} == set(wf)


def test_feature_roundtrip_links_set_and_get_through_a_captured_fid(conn, parsed_set):
    a = _wf(conn)["wf:feature-roundtrip:07h"]
    set_, get = a["predicate"]["steps"]
    assert (set_["opcode"], get["opcode"]) == (9, 10) and set_["capture"]["fid"] == {"dw": 10, "hi": 7, "lo": 0}
    assert {"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 7} in set_["fields"]
    assert {"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": {"var": "fid"}} in get["fields"]
    assert any(f["lo"] == 8 and f["hi"] == 10 and f["value"] == 0 for f in get["fields"])      # SEL = Current, resolved from the value table
    assert "Set Features" in a["description"] and "1." in a["description"]


def test_data_path_workflow_requires_the_same_range(conn, parsed_set):
    write, read = _wf(conn)["wf:write-read"]["predicate"]["steps"]
    var = next(iter(write["capture"]))
    assert {"var": var} in [f["value"] for f in read["fields"]]


def test_recipes_whose_commands_are_missing_are_skipped_not_guessed(conn, parsed_set):
    items = {r["key"]: {"key": r["key"], "kind": r["kind"], "attrs": jloads(r["attrs_json"], {})}
             for r in rows(conn, "SELECT key,kind,attrs_json FROM catalog_item WHERE set_id=1")}
    out, issues = build_workflow_items(items)
    skipped = {i["key"] for i in issues if i["kind"] == "workflow_skipped"}
    # the synthetic specs have no Delete I/O Queue or Namespace Management fields, so those flows must not appear
    assert "wf:queue-teardown" in skipped and "wf:ns-lifecycle" in skipped
    assert not any(k in {"wf:queue-teardown", "wf:ns-lifecycle"} for k in {o["key"] for o in out})
    only_mi = {k: v for k, v in items.items() if not k.startswith("cmd:mi")}
    assert not [o for o in build_workflow_items(only_mi)[0] if o["attrs"]["recipe"].startswith("mi-")]
    assert len({r["id"] for r in RECIPES}) == len(RECIPES)


def test_workflow_is_covered_only_when_the_whole_sequence_with_the_same_fid_is_in_one_test(conn, parsed_set, demo_dir):
    src = cmddb.register_source(conn, str(demo_dir / "cmds-a.db"))
    cmddb.import_source(conn, src)
    run = coverage.run_coverage(conn, 1, src)
    res = {r["sid"]: r for r in rows(conn, "SELECT sid,state,match_count FROM coverage_result WHERE run_id=? AND kind='workflow'", (run,))}
    assert res["T:wf:feature-roundtrip:07h"]["state"] == "matched"
    assert res["T:wf:feature-roundtrip:01h"]["state"] == "not_matched"          # Arbitration is never read back in the demo log
    item = conn.execute("SELECT state FROM item_result WHERE run_id=? AND item_key='wf:feature-roundtrip:07h'", (run,)).fetchone()
    assert item["state"] == "stimulus_covered"


def test_requirements_that_relate_commands_record_the_mentions(conn, parsed_set):
    reqs = [jloads(r["attrs_json"], {}) for r in rows(conn, "SELECT attrs_json FROM catalog_item WHERE set_id=1 AND kind='requirement'")]
    assert all("mentions" in a for a in reqs)
    own = rows(conn, "SELECT key,attrs_json FROM catalog_item WHERE set_id=1 AND kind='requirement'")
    assert all(jloads(r["attrs_json"], {}).get("command") not in jloads(r["attrs_json"], {})["mentions"] for r in own)


def _ai_flow(req_text):
    step_a = {"command": "cmd:admin:09", "note": "setup", "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 4}], "capture": {"fid": {"dw": 10, "hi": 7, "lo": 0}}}
    step_b = {"command": "cmd:admin:0A", "note": "verify", "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": {"var": "fid"}}]}
    return {"title": "Set then Get the same feature", "observability": "stimulus", "order": "strict", "steps": [step_a, step_b], "quotes": [req_text[:60]]}


def test_ai_may_draft_cross_command_scenarios_only_for_commands_the_requirement_mentions(conn, parsed_set):
    from llm_helpers import Fake, reply_for, settings
    from specforge.ai import build_context, generate_ai_scenarios
    from specforge.llm.service import LLMService

    req, _ = reply_for(conn)                                   # an Identify requirement
    attrs = jloads(req["attrs_json"], {})
    reply = json.dumps({"testable": True, "scenarios": [_ai_flow(attrs["text"])]})

    # not mentioned: the draft references commands outside the model's catalog slice and is rejected
    svc = LLMService(conn, settings(), Fake([reply]))
    assert generate_ai_scenarios(conn, 1, svc, [req["key"]])["rejected"] == 1

    # the catalog recorded that the statement relates Set Features and Get Features
    attrs["mentions"] = ["cmd:admin:09", "cmd:admin:0A"]
    conn.execute("UPDATE catalog_item SET attrs_json=? WHERE id=?", (json.dumps(attrs), req["id"]))
    conn.execute("DELETE FROM enrichment")
    conn.commit()
    ctx = build_context(conn, 1, {"key": req["key"], "attrs": attrs})
    assert {c["command"] for c in ctx["catalog"] if "name" in c} >= {"cmd:admin:06", "cmd:admin:09", "cmd:admin:0A"}
    svc = LLMService(conn, settings(), Fake([reply]))
    assert generate_ai_scenarios(conn, 1, svc, [req["key"]])["created"] == 1
    sc = rows(conn, "SELECT * FROM scenario WHERE origin='ai'")[-1]
    steps = json.loads(sc["predicate_json"])["steps"]
    assert [s["opcode"] for s in steps] == [9, 10] and steps[0]["note"] == "setup" and sc["state"] == "draft"
