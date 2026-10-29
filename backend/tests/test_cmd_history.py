"""Command-history databases: NVMe commands in CommandHistoryTable, NVMe-MI commands in MICommandHistoryTable, hex text values."""
import json
import sqlite3

import pytest

from specforge import cmddb
from specforge.core.db import rows
from specforge.devtools.synth import sqe

NVME_COLS = ["SN INTEGER", "Mode VARCHAR(100)", "CmdType VARCHAR(10)", "Command VARCHAR(100)", "CMDID VARCHAR(20)",
             "ECODE VARCHAR(100)", "OP VARCHAR(100)", "NS VARCHAR(100)", "FUSE VARCHAR(100)", "PSDT VARCHAR(100)",
             *[f"DWORD{i:02d} VARCHAR(100)" for i in (2, 3, 10, 11, 12, 13, 14, 15)],
             "Start VARCHAR(100)", "END VARCHAR(100)", "EXECUTION VARCHAR(100)", "STATUS_CODE VARCHAR(10)", "STATUS_VALUE VARCHAR(10)",
             *[f"VENDOR_{i} VARCHAR(10)" for i in range(6)], "QID VARCHAR(5)", "DeviceID VARCHAR(20)"]


def nvme_row(sn, cmdtype, name, cid, op, ns, cdw10=0, cdw11=0, fuse=0, psdt=0):
    return (sn, "SYNC", cmdtype, name, hex(cid), "0x1", hex(op), hex(ns), hex(fuse), hex(psdt),
            "0x0", "0x0", hex(cdw10), hex(cdw11), "0x0", "0x0", "0x0", "0x0",
            "[2026-11-01 03.24.19.950]", "[2026-11-01 03.24.19.950]", "360uSecs", "127", "6", "0", "0", "0", "578", "0", "578", "0", "0#DEV")


def make_history_db(path, nvme=None, mi_rows=(("MI", "0x0", "0x0", "0x1000000", "0x0"),
                                   ("MI", "0x1", "0x0", "0x0", "0x0"))):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE CommandHistoryTable ({', '.join(NVME_COLS)})")
    con.execute("CREATE TABLE DeviceDetailsTable (DeviceID VARCHAR(20) PRIMARY KEY, Model VARCHAR(100), Serial VARCHAR(100), "
                "Firmware VARCHAR(100), Driver VARCHAR(100), capacity VARCHAR(20), suite VARCHAR(20), busnum VARCHAR(10), "
                "functionN VARCHAR(20), deviceN VARCHAR(20))")
    con.execute("CREATE TABLE MICommandHistoryTable (CmdType VARCHAR(10), OP VARCHAR(100), Header VARCHAR(100), "
                "DWORD0 VARCHAR(100), DWORD1 VARCHAR(100), DeviceID VARCHAR(20))")
    nvme = nvme if nvme is not None else [nvme_row(1245183315555, "ADMIN", "SetFeature", 0x243, 0x09, 0, cdw10=0xE),
            nvme_row(1245183304551, "ADMIN", "Identify", 0x242, 0x06, 0, cdw10=0x1),      # out of insertion order: SN orders
            nvme_row(1245183702071, "ADMIN", "Identify", 0x250, 0x06, 0xFFFFFFFF),
            nvme_row(1245183903389, "ADMIN", "GetLogPage", 0x257, 0x02, 0, cdw10=0x3FF0005),
            nvme_row(1245184000000, "IO", "Read", 0x300, 0x02, 1, cdw10=0x100, fuse=1, psdt=2)]
    con.executemany(f"INSERT INTO CommandHistoryTable VALUES ({','.join('?' * len(NVME_COLS))})", nvme)
    con.executemany("INSERT INTO MICommandHistoryTable VALUES (?,?,?,?,?, '0#DEV')", mi_rows)
    con.commit()
    con.close()
    return path


@pytest.fixture()
def hist(tmp_path):
    d = tmp_path / "2026_10_30"
    d.mkdir()
    return make_history_db(d / "CommandHistory_00_09_52.db")


def test_suggest_picks_the_table_for_the_chosen_kind(hist):
    nv = cmddb.suggest_mapping(str(hist))
    assert nv["format"] == "command_history" and nv["kinds"] == ["mi", "nvme"] and nv["warnings"] == []
    m = nv["mapping"]
    assert m["table"] == "CommandHistoryTable" and m["seq"] == "SN" and m["queue"] == "CmdType"
    assert m["fields"] == {"opcode": "OP", "nsid": "NS", "fuse": "FUSE", "psdt": "PSDT", "cid": "CMDID"}
    assert m["cdw"]["10"] == "DWORD10" and "Command" not in m.values()          # the command name is not an SQE blob
    assert m["test"] == {"from": "file"}
    mi = cmddb.suggest_mapping(str(hist), "mi")["mapping"]
    assert mi["table"] == "MICommandHistoryTable" and mi["interface"] == {"const": "mi"}
    assert mi["mi_fields"] == {"opcode": "OP", "nmd0": "DWORD0", "nmd1": "DWORD1"}
    with pytest.raises(ValueError):
        cmddb.suggest_mapping(str(hist), "pcie")


def test_nvme_rows_decode_like_the_equivalent_sqe(conn, hist):
    sid = cmddb.register_source(conn, str(hist))
    res = cmddb.import_source(conn, sid)
    assert (res["rows"], res["decoded"], res["undecodable"]) == (5, 5, 0)
    assert rows(conn, "SELECT name FROM cmd_source")[0]["name"] == "CommandHistory_00_09_52.db (NVMe)"
    got = rows(conn, "SELECT test_id,seq,queue,cs,opcode,nsid,signature FROM decoded_command WHERE source_id=? ORDER BY id", (sid,))
    assert [(r["opcode"], r["queue"]) for r in got] == [(6, "admin"), (9, "admin"), (6, "admin"), (2, "admin"), (2, "io")]
    assert got[0]["seq"] == 1245183304551 and got[2]["nsid"] == 0xFFFFFFFF
    assert {r["test_id"] for r in got} == {"2026_10_30/CommandHistory_00_09_52"}
    read = cmddb.decode_sqe(sqe(0x02, nsid=1, cid=0x300, cdw10=0x100, fuse=1, psdt=2))
    assert got[4]["signature"] == cmddb.signature("pcie", "io", "nvm", 2, read["dwords"])
    glp = rows(conn, "SELECT dwords_json FROM cmd_signature WHERE source_id=? AND opcode=2 AND queue='admin'", (sid,))[0]
    assert '"10": 67043333' in glp["dwords_json"]                                   # 0x3ff0005


def test_mi_rows_decode_as_mi_commands(conn, hist):
    sid = cmddb.register_source(conn, str(hist), kind="mi")
    res = cmddb.import_source(conn, sid)
    assert (res["rows"], res["decoded"]) == (2, 2)
    got = rows(conn, "SELECT interface,queue,cs,opcode FROM decoded_command WHERE source_id=? ORDER BY id", (sid,))
    assert [(r["interface"], r["queue"], r["cs"], r["opcode"]) for r in got] == [("mi", "mi", "mi", 0), ("mi", "mi", "mi", 1)]
    dws = rows(conn, "SELECT dwords_json FROM cmd_signature WHERE source_id=? AND opcode=0", (sid,))[0]["dwords_json"]
    assert '"nmd0": 16777216' in dws
    assert rows(conn, "SELECT name FROM cmd_source WHERE id=?", (sid,))[0]["name"].endswith("(NVMe-MI)")


def test_empty_mi_table_and_bad_values_are_reported(conn, tmp_path):
    p = make_history_db(tmp_path / "empty.db", mi_rows=())
    assert any("no NVMe-MI commands" in w for w in cmddb.suggest_mapping(str(p), "mi")["warnings"])
    con = sqlite3.connect(p)
    con.execute("UPDATE CommandHistoryTable SET OP='junk' WHERE Command='SetFeature'")
    con.commit()
    con.close()
    sid = cmddb.register_source(conn, str(p))
    res = cmddb.import_source(conn, sid)
    assert res["undecodable"] == 1 and "not a number" in res["errors"][0]["error"]


def test_kind_does_not_apply_to_other_databases(demo_dir):
    out = cmddb.suggest_mapping(str(demo_dir / "cmds-a.db"), "mi")
    assert "format" not in out and any("does not apply" in w for w in out["warnings"])


def test_api_suggest_takes_the_kind(hist, ws):
    from fastapi.testclient import TestClient

    from specforge.api.app import create_app

    c = TestClient(create_app())
    assert c.get("/api/cmddb/suggest", params={"path": str(hist), "kind": "mi"}).json()["mapping"]["table"] == "MICommandHistoryTable"
    assert c.get("/api/cmddb/suggest", params={"path": str(hist), "kind": "x"}).status_code == 400
    sid = c.post("/api/cmddb", json={"path": str(hist), "kind": "mi"}).json()["id"]
    assert c.get("/api/cmddb").json()[0]["mapping"]["table"] == "MICommandHistoryTable" and sid


def make_tree(root):
    """A folder of test scripts: one command-history database per script, in sub-folders; plus things that must be skipped."""
    (root / "suiteA" / "2026_10_30").mkdir(parents=True)
    (root / "suiteB").mkdir()
    (root / ".hidden").mkdir()
    make_history_db(root / "suiteA" / "2026_10_30" / "TC_Identify.db", nvme=[
        nvme_row(1, "ADMIN", "Identify", 1, 0x06, 0, cdw10=0x1), nvme_row(2, "ADMIN", "Identify", 2, 0x06, 0, cdw10=0x2)])
    make_history_db(root / "suiteB" / "TC_GetLogPage.db", nvme=[nvme_row(1, "ADMIN", "GetLogPage", 1, 0x02, 0, cdw10=0x3FF0002)], mi_rows=())
    make_history_db(root / ".hidden" / "skip.db", nvme=[nvme_row(1, "ADMIN", "Identify", 1, 0x06, 0)])
    (root / "notes.txt").write_text("not a database")
    (root / "fake.db").write_text("not a database either")
    con = sqlite3.connect(root / "suiteB" / "other.sqlite")      # a SQLite file without the command table
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    return root


def test_folder_lists_every_database_with_its_test_name(tmp_path):
    root = make_tree(tmp_path / "logs")
    found = [f.relative_to(root).as_posix() for f in cmddb.discover(root)]
    assert found == ["suiteA/2026_10_30/TC_Identify.db", "suiteB/other.sqlite", "suiteB/TC_GetLogPage.db"]
    sc = cmddb.scan(str(root))
    assert sc["folder"] and sc["count"] == 3 and sc["mapping"]["table"] == "CommandHistoryTable"
    by = {d["rel"]: d for d in sc["databases"]}
    assert by["suiteA/2026_10_30/TC_Identify.db"]["test"] == "suiteA/2026_10_30/TC_Identify"
    assert by["suiteA/2026_10_30/TC_Identify.db"]["commands"] == {"nvme": 2, "mi": 2}
    assert by["suiteB/other.sqlite"]["tables"] == ["t"]
    assert cmddb.scan(str(root), "mi")["mapping"]["table"] == "MICommandHistoryTable"
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="No SQLite databases"):
        cmddb.suggest_mapping(str(tmp_path / "empty"))


def test_folder_import_keeps_one_test_per_database(conn, tmp_path):
    root = make_tree(tmp_path / "logs")
    sid = cmddb.register_source(conn, str(root))
    res = cmddb.import_source(conn, sid)
    assert (res["databases"], res["failed_databases"], res["rows"], res["decoded"]) == (3, 1, 3, 3)
    src = rows(conn, "SELECT name,unknown_json FROM cmd_source WHERE id=?", (sid,))[0]
    assert src["name"] == "logs/ (NVMe)"
    files = {f["rel"]: f for f in json.loads(src["unknown_json"])["files"]}
    assert files["suiteB/other.sqlite"]["error"].startswith("no table") and files["suiteB/TC_GetLogPage.db"]["decoded"] == 1
    got = rows(conn, "SELECT test_id,script,seq,opcode FROM decoded_command WHERE source_id=? ORDER BY id", (sid,))
    assert [(r["test_id"], r["script"], r["seq"]) for r in got] == [
        ("suiteA/2026_10_30/TC_Identify", "suiteA/2026_10_30/TC_Identify.db", 1),
        ("suiteA/2026_10_30/TC_Identify", "suiteA/2026_10_30/TC_Identify.db", 2),
        ("suiteB/TC_GetLogPage", "suiteB/TC_GetLogPage.db", 1)]
    make_history_db(root / "suiteB" / "TC_New.db", nvme=[nvme_row(1, "ADMIN", "Identify", 1, 0x06, 0)])
    assert cmddb.import_source(conn, sid)["databases"] == 4                      # a re-import picks up new databases


def test_coverage_per_test_script(conn, parsed_set, tmp_path):
    from specforge import coverage, reports

    root = make_tree(tmp_path / "logs")
    sid = cmddb.register_source(conn, str(root))
    cmddb.import_source(conn, sid)
    run = coverage.run_coverage(conn, parsed_set, sid)
    tests = {t["test"]: t for t in coverage.test_results(conn, run)}
    assert set(tests) == {"suiteA/2026_10_30/TC_Identify", "suiteB/TC_GetLogPage"}
    ident, glp = tests["suiteA/2026_10_30/TC_Identify"], tests["suiteB/TC_GetLogPage"]
    assert ident["commands"] == 2 and ident["by_kind"]["command"]["exercised"] == 1 and ident["script"].endswith("TC_Identify.db")
    assert 0 < glp["pct_covered"] and glp["denominator"] == ident["denominator"]
    items = rows(conn, "SELECT value AS k FROM json_each((SELECT items_json FROM coverage_test WHERE run_id=? AND test_id=?))",
                 (run, "suiteB/TC_GetLogPage"))
    assert "cmd:admin:02" in {r["k"] for r in items} and "cmd:admin:06" not in {r["k"] for r in items}
    ds = reports.build_dataset(conn, run)
    assert len(ds["tests"]) == 2 and b"suiteB/TC_GetLogPage" in reports.to_csv(ds, "tests")
    assert b"By test" in reports.to_html(ds)
    reports.to_xlsx(ds)


def test_api_scan_browse_upload_and_tests(ws, tmp_path, parsed_set):
    from fastapi.testclient import TestClient

    from specforge.api.app import create_app

    root = make_tree(tmp_path / "logs")
    c = TestClient(create_app())
    sc = c.get("/api/cmddb/scan", params={"path": str(root)}).json()
    assert sc["count"] == 3
    assert c.get("/api/cmddb/scan", params={"path": str(tmp_path / "nope")}).status_code == 400
    b = c.get("/api/fs/browse", params={"path": str(root / "suiteB")}).json()
    assert [d["name"] for d in b["databases"]] == ["other.sqlite", "TC_GetLogPage.db"] and b["parent"] == str(root)
    assert [d["name"] for d in c.get("/api/fs/browse", params={"path": str(root)}).json()["dirs"]] == ["suiteA", "suiteB"]
    assert c.get("/api/fs/browse").json()["dirs"]
    # a dropped folder: relative paths are kept below the folder's name
    up = [("files", ("a.db", (root / "suiteA" / "2026_10_30" / "TC_Identify.db").read_bytes())),
          ("files", ("b.db", (root / "suiteB" / "TC_GetLogPage.db").read_bytes())),
          ("files", ("n.txt", b"x")), ("files", ("x.db", b"not sqlite"))]
    paths = {"paths": ["drop/suiteA/TC_Identify.db", "drop/suiteB/../TC_GetLogPage.db", "drop/n.txt", "drop/x.db"]}
    r = c.post("/api/cmddb/upload", files=up, data=paths)
    assert r.status_code == 200, r.text
    dest = r.json()["path"]
    assert r.json()["databases"] == 2 and dest.endswith("-drop")
    assert [f.relative_to(dest).as_posix() for f in cmddb.discover(dest)] == ["suiteA/TC_Identify.db", "suiteB/TC_GetLogPage.db"]
    assert c.post("/api/cmddb/upload", files=[("files", ("n.txt", b"x"))]).status_code == 400
    sid = c.post("/api/cmddb", json={"path": dest}).json()["id"]
    sid2 = c.post("/api/cmddb", json={"path": dest, "kind": "mi"}).json()["id"]
    assert sid != sid2
    from specforge import coverage
    from specforge.core.db import get_conn

    cmddb.import_source(get_conn(), sid)
    run = coverage.run_coverage(get_conn(), parsed_set, sid)
    tests = c.get(f"/api/coverage/{run}/tests").json()
    assert [t["test"] for t in tests] == ["suiteA/TC_Identify", "suiteB/TC_GetLogPage"]
    items = c.get(f"/api/coverage/{run}/items", params={"test": "suiteB/TC_GetLogPage", "kind": "command"}).json()
    assert [i["key"] for i in items["items"]] == ["cmd:admin:02"]
