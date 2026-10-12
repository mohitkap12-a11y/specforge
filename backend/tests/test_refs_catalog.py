import json

from specforge import catalog, refs, scenarios
from specforge.core.db import rows
from specforge.ingest import store


def items(conn, kind=None, like=None):
    q, a = "SELECT * FROM catalog_item WHERE set_id=1", []
    if kind:
        q += " AND kind=?"; a.append(kind)
    if like:
        q += " AND key LIKE ?"; a.append(like)
    return rows(conn, q, a)


def test_full_set_resolves_all_references(conn, parsed_set):
    rs = refs.resolve_references(conn, parsed_set)
    s = refs.summarize(rs)
    assert s["broken"] == s["ambiguous"] == s["external-not-loaded"] == 0 and s["cross_spec"] == 4
    mi_to_base = next(r for r in rs if r["from_family"] == "nvme-mi" and r["target_family"] == "nvme-base")
    assert mi_to_base["target_key"] == "nvme-base@2.4/sec:5.2"


def test_missing_family_is_external_not_loaded_then_resolves(conn, demo_dir):
    ids = {}
    for n in ("nvme-nvm-cs-1.3.pdf", "nvme-base-2.4.pdf"):
        d = store.register_document(conn, demo_dir / n)
        store.run_parse(conn, d["id"], workers=1)
        ids[n] = d["id"]
    conn.execute("INSERT INTO spec_set(name) VALUES('s')")
    conn.execute("INSERT INTO spec_set_member VALUES(1,?)", (ids["nvme-nvm-cs-1.3.pdf"],))
    conn.commit()
    rs = refs.resolve_references(conn, 1)
    assert {r["state"] for r in rs if r["target_family"] == "nvme-base"} == {"external-not-loaded"}
    conn.execute("INSERT INTO spec_set_member VALUES(1,?)", (ids["nvme-base-2.4.pdf"],))   # add Base: re-resolve only
    conn.commit()
    rs = refs.resolve_references(conn, 1)
    assert {r["state"] for r in rs if r["target_family"] == "nvme-base"} == {"resolved"}


def test_two_versions_of_a_family_are_ambiguous_until_chosen(conn, demo_dir, tmp_path):
    from specforge.devtools.synth import base_spec, write_pdf

    p2 = tmp_path / "base-2.3.pdf"
    write_pdf(p2, [("title", "NVM Express® Base Specification", "Revision 2.3")] + base_spec()[1:], header="nvme-base 2.3 synthetic")
    members = []
    for path in (demo_dir / "nvme-base-2.4.pdf", p2, demo_dir / "nvme-nvm-cs-1.3.pdf"):
        d = store.register_document(conn, path)
        store.run_parse(conn, d["id"], workers=1)
        members.append(d["id"])
    conn.execute("INSERT INTO spec_set(name) VALUES('s')")
    for m in members:
        conn.execute("INSERT INTO spec_set_member VALUES(1,?)", (m,))
    conn.commit()
    amb = [r for r in refs.resolve_references(conn, 1) if r["state"] == "ambiguous"]
    assert amb and len(amb[0]["candidates"]) == 2
    refs.choose_reference(conn, 1, amb[0]["src_key"], amb[0]["raw"], amb[0]["candidates"][1])
    again = [r for r in refs.resolve_references(conn, 1) if r["src_key"] == amb[0]["src_key"] and r["raw"] == amb[0]["raw"]]
    assert again[0]["state"] == "resolved" and again[0]["target_key"] == amb[0]["candidates"][1]


def test_commands_come_from_opcode_tables(conn, parsed_set):
    keys = {i["key"] for i in items(conn, "command")}
    assert {"cmd:admin:06", "cmd:admin:09", "cmd:admin:0A", "cmd:admin:02", "cmd:io:nvm:01", "cmd:io:nvm:02", "cmd:mi:00"} <= keys
    assert len(keys) == 19
    ident = json.loads(next(i for i in items(conn, "command", "cmd:admin:06"))["attrs_json"])
    assert ident["name"] == "Identify" and ident["section"] == "nvme-base@2.4/sec:5.2" and len(ident["tables"]) == 2


def test_fields_values_reserved_and_boundaries(conn, parsed_set):
    cns = [i["key"] for i in items(conn, "value", "cmd:admin:06/dw10[07:00]=%")]
    assert cns == ["cmd:admin:06/dw10[07:00]=00h", "cmd:admin:06/dw10[07:00]=01h", "cmd:admin:06/dw10[07:00]=02h",
                   "cmd:admin:06/dw10[07:00]=03h", "cmd:admin:06/dw10[07:00]=04h-0Fh", "cmd:admin:06/dw10[07:00]=10h"]
    rsvd_val = next(i for i in items(conn, "value", "cmd:admin:06/dw10[07:00]=04h-0Fh"))
    assert rsvd_val["observability"] == "stimulus"
    assert [i["key"] for i in items(conn, "reserved", "cmd:admin:06/%")] == ["cmd:admin:06/dw10[15:08]:rsvd", "cmd:admin:06/dw11[23:16]:rsvd"]
    # 1-bit flag without value table -> implicit 0/1
    assert {i["key"] for i in items(conn, "value", "cmd:io:nvm:02/dw12[31:31]=%")} == {"cmd:io:nvm:02/dw12[31:31]=00h", "cmd:io:nvm:02/dw12[31:31]=01h"}
    # multi-bit un-enumerated field -> min/max boundaries
    assert {i["key"] for i in items(conn, "boundary", "cmd:io:nvm:02/dw12[15:00]=%")} == {"cmd:io:nvm:02/dw12[15:00]=min", "cmd:io:nvm:02/dw12[15:00]=max"}
    # SEL values written in binary notation
    assert len(items(conn, "value", "cmd:admin:0A/dw10[10:08]=%")) == 5
    # value table referenced from another field description (Figure 131 for FID)
    assert len(items(conn, "value", "cmd:admin:09/dw10[07:00]=%")) == 5
    # NMD fields for MI
    assert items(conn, "field", "cmd:mi:00/nmd0[31:24]") and len(items(conn, "value", "cmd:mi:00/nmd0[31:24]=%")) == 5


def test_requirements_link_and_classify(conn, parsed_set):
    reqs = [(i, json.loads(i["attrs_json"])) for i in items(conn, "requirement")]
    zeros = next(a for i, a in reqs if "return zeros" in a["text"])
    assert zeros["command"] is None or zeros["command"].startswith("cmd:")
    abort = next((i, a) for i, a in reqs if "Invalid Field in Command if a reserved" in a["text"])
    assert abort[1]["command"] == "cmd:admin:06" and abort[0]["observability"] == "stimulus"
    generic = next((i for i, a in reqs if a["text"].startswith("A controller shall process each command")))
    assert generic["observability"] == "unobservable"


def test_overrides_survive_catalog_rebuild(conn, parsed_set):
    key = next(i["key"] for i in items(conn, "requirement") if i["observability"] == "stimulus")
    catalog.set_override(conn, 1, key, "unobservable", "internal behaviour")
    catalog.build_catalog(conn, 1)
    row = next(i for i in items(conn, "requirement") if i["key"] == key)
    assert row["observability"] == "unobservable" and row["na_reason"] == "internal behaviour"


def test_template_scenarios_validate_and_link(conn, parsed_set):
    cmds, fields = scenarios.catalog_context(conn, 1)
    sc = rows(conn, "SELECT * FROM scenario WHERE set_id=1 AND origin='template'")
    assert len(sc) == 152
    for s in sc:
        assert scenarios.validate_predicate(json.loads(s["predicate_json"]), cmds, fields) == [], s["sid"]
        assert s["state"] == "approved"
    n_links = conn.execute("SELECT COUNT(*) FROM scenario_item").fetchone()[0]
    assert n_links == len(sc)
    # regenerating is idempotent
    scenarios.generate_templates(conn, 1)
    assert conn.execute("SELECT COUNT(*) FROM scenario WHERE origin='template'").fetchone()[0] == 152


def test_predicate_validator_rejects_bad_predicates(conn, parsed_set):
    cmds, fields = scenarios.catalog_context(conn, 1)
    good = {"steps": [{"command": "cmd:admin:06", "interface": "pcie", "queue": "admin", "cs": None, "opcode": 6,
                       "fields": [{"dw": 10, "hi": 7, "lo": 0, "op": "eq", "value": 1}]}]}
    assert scenarios.validate_predicate(good, cmds, fields) == []
    import copy

    def bad(mut):
        g = copy.deepcopy(good)
        mut(g)
        return scenarios.validate_predicate(g, cmds, fields)

    assert any("opcode" in e for e in bad(lambda g: g["steps"][0].__setitem__("opcode", 7)))
    assert any("not in the catalog" in e for e in bad(lambda g: g["steps"][0].__setitem__("command", "cmd:admin:EE")))
    assert any("does not fit" in e for e in bad(lambda g: g["steps"][0]["fields"][0].__setitem__("value", 300)))
    assert any("not inside any field" in e for e in bad(lambda g: g["steps"][0]["fields"][0].update(dw=12)))
    assert any("variable" in e for e in bad(lambda g: g["steps"][0]["fields"][0].__setitem__("value", {"var": "nope"})))
    assert scenarios.validate_predicate({"steps": []}) != []
