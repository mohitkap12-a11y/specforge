"""Regression tests for the third round of Macroscope findings (review of cb8962f)."""
import json
import sqlite3

from fastapi.testclient import TestClient

from specforge import catalog, cmddb, scenarios
from specforge.core.db import rows
from specforge.devtools.synth import write_pdf
from specforge.ingest import store


def T(title, rev):
    return ("title", f"NVM Express® {title}", f"Revision {rev}")


def build(conn, tmp_path, specs):
    ids = []
    for name, blocks in specs:
        p = tmp_path / name
        write_pdf(p, blocks, header=name)
        d = store.register_document(conn, p)
        store.run_parse(conn, d["id"], workers=1)
        ids.append(d["id"])
    conn.execute("INSERT INTO spec_set(name) VALUES('s')")
    for i in ids:
        conn.execute("INSERT INTO spec_set_member VALUES(1,?)", (i,))
    conn.commit()


OPC = ("table", "Figure 1: Opcodes for Admin Commands", ["Opcode", "O/M", "Command"],
       [["10h", "M", "Foo"], ["11h to 14h", "O", "Bar"]], None)


def keys(conn, like):
    return {r["key"] for r in rows(conn, "SELECT key FROM catalog_item WHERE key LIKE ?", (like,))}


# ------------------------------------------------------------------ catalog

def test_every_command_of_an_opcode_range_gets_the_field_tables(conn, tmp_path):
    base = [T("Base Specification", "2.4"), ("h", 1, "5 Admin"), OPC,
            ("table", "Figure 4: Bar – Command Dword 10", ["Bits", "Description"],
             [["31:16", "Gadget (GDG): selects a gadget."], ["15:00", "Reserved"]], None)]
    build(conn, tmp_path, [("b.pdf", base)])
    catalog.build_catalog(conn, 1)
    for op in ("11", "12", "13", "14"):
        assert f"cmd:admin:{op}/dw10[31:16]" in keys(conn, f"cmd:admin:{op}/%"), op
        assert f"cmd:admin:{op}/dw10[15:00]:rsvd" in keys(conn, f"cmd:admin:{op}/%")


def test_field_tables_never_cross_revisions_of_a_family(conn, tmp_path):
    a = [T("Base Specification", "2.4"), ("h", 1, "5 Admin"), OPC,
         ("table", "Figure 4: Foo – Command Dword 10", ["Bits", "Description"], [["31:00", "Alpha (ALP): a field."]], None)]
    b = [T("Base Specification", "2.3"), ("h", 1, "5 Admin"),     # revision 2.3 defines Foo's dword 11 but has no opcode table
         ("table", "Figure 4: Foo – Command Dword 11", ["Bits", "Description"], [["31:00", "Beta (BET): another field."]], None)]
    build(conn, tmp_path, [("a.pdf", a), ("b.pdf", b)])
    res = catalog.build_catalog(conn, 1)
    assert "cmd:admin:10/dw10[31:00]" in keys(conn, "cmd:admin:10/%")
    assert not keys(conn, "cmd:admin:10/dw11%")                                 # 2.3's table is not attached to 2.4's command
    assert any(i["kind"] == "unlinked_field_table" for i in res["issues"])


def test_value_table_description_column_is_found_case_insensitively(conn, tmp_path):
    base = [T("Base Specification", "2.4"), ("h", 1, "5 Admin"), OPC, ("h", 2, "5.1 Foo command"),
            ("table", "Figure 4: Foo – Command Dword 10", ["Bits", "Description"],
             [["31:08", "Reserved"], ["07:00", "Mode (MODE): selects a mode. Refer to Figure 5."]], None),
            ("table", "Figure 5: Foo – Mode Values", ["Value", "o/m", "Description"],
             [["00h", "M", "Alpha"], ["01h to 03h", "", "Reserved"]], None)]
    build(conn, tmp_path, [("b.pdf", base)])
    catalog.build_catalog(conn, 1)
    r = rows(conn, "SELECT observability, attrs_json FROM catalog_item WHERE key='cmd:admin:10/dw10[07:00]=01h-03h'")[0]
    assert r["observability"] == "stimulus" and json.loads(r["attrs_json"])["reserved_value"] is True
    a = json.loads(rows(conn, "SELECT attrs_json FROM catalog_item WHERE key='cmd:admin:10/dw10[07:00]=00h'")[0]["attrs_json"])
    assert a["desc"] == "Alpha"


# ------------------------------------------------------------------ cmddb / scenarios

def test_cdw_table_without_cdw0_is_not_a_usable_mapping(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE cmds(id INTEGER PRIMARY KEY, " + ", ".join(f"cdw{i} INTEGER" for i in range(1, 16)) + ")")
    con.commit(); con.close()
    s = cmddb.suggest_mapping(str(tmp_path / "x.db"))
    assert "cdw" not in s["mapping"] and any("cdw0" in w for w in s["warnings"])


def test_fields_lost_by_signature_dedup_cannot_be_used_in_predicates(conn, parsed_set):
    cmds, fields = scenarios.catalog_context(conn, 1)
    step = lambda dw, hi, lo: {"steps": [{"command": "cmd:admin:06", "interface": "pcie", "queue": "admin", "cs": None, "opcode": 6,   # noqa: E731
                                         "fields": [{"dw": dw, "hi": hi, "lo": lo, "op": "eq", "value": 0}]}]}
    for dw, hi, lo in ((6, 31, 0), (4, 7, 0), (9, 0, 0), (0, 31, 16), (0, 16, 16)):
        assert any("de-duplicated" in e for e in scenarios.validate_predicate(step(dw, hi, lo), cmds, fields)), (dw, hi, lo)
    assert not any("de-duplicated" in e for e in scenarios.validate_predicate(step(0, 15, 0), cmds, fields))   # opcode/FUSE/PSDT are kept
    assert not any("de-duplicated" in e for e in scenarios.validate_predicate(step(10, 7, 0), cmds, fields))


def test_browse_does_not_show_values_the_signature_ignores(ws, conn, parsed_set, demo_dir):
    sid = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, sid)
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        cmds = client.get(f"/api/cmddb/{sid}/commands", params={"interface": "pcie", "limit": 1000}).json()
    assert cmds and all(not ({"4", "5", "6", "7", "8", "9"} & set(c["dwords"])) and c["dwords"]["0"] <= 0xFFFF for c in cmds)


# ------------------------------------------------------------------ db / api

def test_deleting_a_set_removes_its_coverage_runs(conn, parsed_set):
    conn.execute("INSERT INTO cmd_source(name,path,mapping_json) VALUES('s','/x','{}')")
    conn.execute("INSERT INTO coverage_run(set_id,source_id) VALUES(1,1)")
    conn.execute("INSERT INTO coverage_result(run_id,scenario_id,state) VALUES(1,1,'matched')")
    conn.execute("INSERT INTO item_result(run_id,item_key,state) VALUES(1,'k','covered')")
    conn.commit()
    conn.execute("DELETE FROM spec_set WHERE id=1"); conn.commit()
    assert [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("coverage_run", "coverage_result", "item_result")] == [0, 0, 0]


def test_create_set_is_all_or_nothing(ws):
    from specforge.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        assert client.post("/api/sets", json={"name": "a", "document_ids": None}).status_code == 422
        assert client.post("/api/sets", json={"document_ids": []}).status_code == 422
        assert client.post("/api/sets", json={"name": "a", "document_ids": [4242]}).status_code == 409
        assert client.get("/api/sets").json() == []                      # nothing half-created, and the connection is not left mid-transaction
        assert client.post("/api/sets", json={"name": "a", "document_ids": []}).status_code == 200
