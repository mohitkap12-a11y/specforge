"""Regression tests for the fifth round of Macroscope findings (review of 7a01c1c)."""
import json
import sqlite3

from llm_helpers import Fake, reply_for, settings

from specforge import catalog, cmddb, scenarios
from specforge.core.db import rows
from specforge.devtools.synth import mi_admin_msg


def test_scenario_ids_are_never_reused(conn, parsed_set):
    top = conn.execute("SELECT MAX(id) FROM scenario").fetchone()[0]
    conn.execute("DELETE FROM scenario WHERE id=?", (top,))
    conn.commit()
    conn.execute("INSERT INTO scenario(set_id,sid,origin,title,observability,predicate_json,state) VALUES(1,'new','manual','t','submission','{}','draft')")
    conn.commit()
    new_id = conn.execute("SELECT id FROM scenario WHERE sid='new'").fetchone()[0]
    assert new_id > top          # an old coverage_result row for `top` can never be attributed to this unrelated scenario


def test_partial_layout_override_keeps_the_other_offsets():
    layout = cmddb.merged_layout({"admin": {"dwords": {"10": 40}}, "mi": {"nmd1_off": 16}})
    assert layout["admin"]["dwords"]["10"] == 40 and layout["admin"]["dwords"]["11"] == 32 and layout["admin"]["dwords"]["15"] == 48
    assert layout["admin"]["opcode_byte"] == 4 and layout["mi"]["nmd0_off"] == 8 and layout["mi"]["nmd1_off"] == 16
    assert cmddb.MI_DEFAULT_LAYOUT["admin"]["dwords"]["10"] == 28          # the defaults are not mutated
    msg = mi_admin_msg(0x06, nsid=3, cdw10=1, cdw11=2, cdw12=3)
    d = cmddb.decode_mi(msg, cmddb.merged_layout({"admin": {"dwords": {"15": 60}}}))   # unrelated partial override
    assert (d["dwords"]["1"], d["dwords"]["10"], d["dwords"]["11"], d["dwords"]["12"]) == (3, 1, 2, 3)


def _gen(conn, key, scenario):
    from specforge.ai import generate_ai_scenarios
    from specforge.llm.service import LLMService

    svc = LLMService(conn, settings(), Fake([json.dumps({"testable": True, "scenarios": [scenario]})]))
    return generate_ai_scenarios(conn, 1, svc, [key])


def test_a_rejected_scenario_is_not_reused_for_a_new_requirement(conn, parsed_set):
    r1, base = reply_for(conn)
    base["quotes"] = ["The Identify command returns a data structure to the host."]
    r2 = next(r["key"] for r in rows(conn, "SELECT key,label FROM catalog_item WHERE kind='requirement'") if "shall not specify a reserved value" in r["label"])
    catalog.set_override(conn, 1, r2, "stimulus", None)
    assert _gen(conn, r1["key"], base)["created"] == 1
    first = rows(conn, "SELECT id FROM scenario WHERE origin='ai'")[0]["id"]
    conn.execute("UPDATE scenario SET state='rejected' WHERE id=?", (first,)); conn.commit()
    stats = _gen(conn, r2, base)                                              # identical predicate, but the earlier scenario was rejected
    assert stats["created"] == 1
    new = [r for r in rows(conn, "SELECT id,state FROM scenario WHERE origin='ai'") if r["id"] != first]
    assert len(new) == 1 and new[0]["state"] == "draft"
    assert [r[0] for r in conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=?", (new[0]["id"],))] == [r2]
    assert r2 not in {r[0] for r in conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=?", (first,))}


def test_null_dwords_are_missing_not_zero(conn, tmp_path):
    con = sqlite3.connect(tmp_path / "n.db")
    con.execute("CREATE TABLE nvme_cmds(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, queue TEXT, "
                + ", ".join(f"cdw{i} INTEGER" for i in range(16)) + ")")
    ok = [6] + [0] * 15
    null10 = list(ok); null10[10] = None            # cdw10 not recorded
    null0 = list(ok); null0[0] = None               # no opcode at all
    for i, dws in enumerate([ok, null10, null0], start=1):
        con.execute("INSERT INTO nvme_cmds VALUES(?,?,?,?,%s)" % ",".join("?" * 16), (i, "t", i, "admin", *dws))
    con.commit(); con.close()
    sid = cmddb.register_source(conn, str(tmp_path / "n.db"))
    r = cmddb.import_source(conn, sid)
    assert (r["decoded"], r["undecodable"]) == (2, 1) and "cdw0" in r["errors"][0]["error"]
    sigs = [json.loads(x[0]) for x in conn.execute("SELECT dwords_json FROM cmd_signature WHERE source_id=?", (sid,))]
    assert sorted("10" in s for s in sigs) == [False, True]       # the NULL dword is absent, so a 'cdw10 == 0' scenario cannot match it


def test_captures_of_unpreserved_dwords_are_rejected():
    first = lambda cap: {"interface": "pcie", "queue": "admin", "opcode": 9, "capture": {"c": cap}}   # noqa: E731
    for cap in ({"dw": 6, "hi": 31, "lo": 0}, {"dw": 4, "hi": 7, "lo": 0}, {"dw": 0, "hi": 31, "lo": 16}):
        errs = scenarios.validate_predicate({"steps": [first(cap)]})
        assert any("cannot be captured" in e for e in errs), cap
    assert scenarios.validate_predicate({"steps": [first({"dw": 10, "hi": 7, "lo": 0})]}) == []
    assert scenarios.validate_predicate({"steps": [first({"dw": 0, "hi": 7, "lo": 0})]}) == []    # the opcode byte is preserved
