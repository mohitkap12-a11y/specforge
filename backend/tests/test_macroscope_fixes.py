"""Regression tests for the Macroscope review findings on PR #1."""
import json
import sqlite3
import time

from fastapi.testclient import TestClient
from llm_helpers import Fake, settings

from specforge import catalog, cmddb, coverage, refs
from specforge.core.db import connect, rows
from specforge.devtools.synth import mi_msg, write_pdf
from specforge.ingest import store


def T(title, rev):  # title block
    return ("title", f"NVM Express® {title}", f"Revision {rev}")


def build(conn, tmp_path, specs):
    """specs: list of (filename, blocks). Parses them and creates spec set 1."""
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
    return ids


def wait_job(client, j):
    while client.get(f"/api/jobs/{j}").json()["state"] not in ("done", "failed"):
        time.sleep(0.05)
    return client.get(f"/api/jobs/{j}").json()


# ------------------------------------------------------------------ app.py

def test_spa_path_containment(tmp_path):
    from specforge.api.app import safe_join

    dist, private = tmp_path / "dist", tmp_path / "dist-private"
    dist.mkdir(); private.mkdir()
    (dist / "a.js").write_text("ok"); (private / "secret.txt").write_text("nope")
    assert safe_join(dist, "a.js") == (dist / "a.js").resolve()
    assert safe_join(dist, "../dist-private/secret.txt") is None      # shares the string prefix of the root, but is outside it
    assert safe_join(dist, "../../etc/passwd") is None and safe_join(dist, "/etc/passwd") is None
    assert safe_join(dist, "") is None and safe_join(dist, "missing.js") is None


def test_set_members_replacement_is_atomic(ws, demo_dir):
    from specforge.api.app import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as client:
        d = client.post("/api/specs/upload", files=[("files", ("nvme-base-2.4.pdf", open(demo_dir / "nvme-base-2.4.pdf", "rb"), "application/pdf"))]).json()[0]["id"]
        sid = client.post("/api/sets", json={"name": "s", "document_ids": [d]}).json()["id"]
        members = lambda: [m["document_id"] for m in client.get("/api/sets").json()[0]["members"]]
        assert members() == [d]
        assert client.put(f"/api/sets/{sid}/members", json={}).status_code == 422
        assert members() == [d]                                              # nothing deleted by the failed request
        assert client.put(f"/api/sets/{sid}/members", json={"document_ids": [d, 9999]}).status_code == 409
        assert members() == [d]                                              # unknown id rolled the whole replacement back
        assert client.put(f"/api/sets/{sid}/members", json={"document_ids": [d]}).json() == {"ok": True}


def test_evidence_is_limited_to_the_runs_spec_set(ws, demo_dir):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        docs = [client.post("/api/specs/upload", files=[("files", (n, open(demo_dir / n, "rb"), "application/pdf"))]).json()[0]["id"]
                for n in ("nvme-base-2.4.pdf", "nvme-nvm-cs-1.3.pdf", "nvme-mi-2.0.pdf")]
        for d in docs:
            wait_job(client, client.post(f"/api/specs/{d}/parse").json()["job"])
        sets = [client.post("/api/sets", json={"name": f"s{i}", "document_ids": docs}).json()["id"] for i in (1, 2)]
        for s in sets:
            assert wait_job(client, client.post(f"/api/sets/{s}/catalog/build").json()["job"])["state"] == "done"
        src = client.post("/api/cmddb", json={"path": str(demo_dir / "cmds-a.db")}).json()["id"]
        wait_job(client, client.post(f"/api/cmddb/{src}/import").json()["job"])
        run = wait_job(client, client.post("/api/coverage/run", json={"set_id": sets[0], "source_id": src}).json()["job"])["result"]["run_id"]
        ev = client.get(f"/api/coverage/{run}/evidence", params={"item": "cmd:admin:06"}).json()
        assert len(ev) == 1 and ev[0]["result"] == "matched"             # the identical scenario of the other set is not returned


# ------------------------------------------------------------------ ai.py

def _gen(conn, keys, scenario):
    from specforge.ai import generate_ai_scenarios
    from specforge.llm.service import LLMService

    svc = LLMService(conn, settings(), Fake([json.dumps({"testable": True, "scenarios": [scenario]})]))
    return generate_ai_scenarios(conn, 1, svc, keys)


def _scenario(**kw):
    s = {"title": "Identify with reserved bits", "observability": "stimulus", "quotes": ["The Identify command returns a data structure to the host."],
         "steps": [{"command": "cmd:admin:06", "fields": [{"dw": 10, "hi": 15, "lo": 8, "op": "ne", "value": 0}]}]}
    s.update(kw)
    return s


def test_duplicate_predicate_links_the_second_requirement(conn, parsed_set):
    r1 = next(r["key"] for r in rows(conn, "SELECT key,label FROM catalog_item WHERE kind='requirement'") if "Invalid Field in Command if a reserved" in r["label"])
    r2 = next(r["key"] for r in rows(conn, "SELECT key,label FROM catalog_item WHERE kind='requirement'") if "shall not specify a reserved value" in r["label"])
    catalog.set_override(conn, 1, r2, "stimulus", None)
    stats = _gen(conn, [r1, r2], _scenario())
    assert (stats["created"], stats["duplicates"]) == (1, 1)
    sc = rows(conn, "SELECT id FROM scenario WHERE origin='ai'")
    assert len(sc) == 1
    assert {r[0] for r in conn.execute("SELECT item_key FROM scenario_item WHERE scenario_id=?", (sc[0]["id"],))} == {r1, r2}


def test_within_zero_is_rejected_not_unlimited(conn, parsed_set):
    r1 = next(r["key"] for r in rows(conn, "SELECT key,label FROM catalog_item WHERE kind='requirement'") if "Invalid Field in Command if a reserved" in r["label"])
    stats = _gen(conn, [r1], _scenario(within=0))
    assert stats["created"] == 0 and stats["rejected"] == 1


# ------------------------------------------------------------------ catalog.py

def test_referenced_non_value_table_is_not_used_as_enumeration(conn, tmp_path):
    base = [T("Base Specification", "2.4"), ("h", 1, "5 Admin Command Set"),
            ("table", "Figure 1: Opcodes for Admin Commands", ["Opcode", "O/M", "Command"],
             [["10h", "M", "Foo"], ["11h to 14h", "O", "Bar"], ["80h to FFh", "O", "Vendor specific"], ["20h to 2Fh", "", "Reserved"]], None),
            ("h", 2, "5.1 Foo command"), ("p", "The Foo command does a thing."),
            ("table", "Figure 2: Foo – Command Dword 10", ["Bits", "Description"],
             [["31:16", "Widget (WGT): This field selects a widget. Refer to Figure 3."], ["15:00", "Reserved"]], None),
            ("table", "Figure 3: Foo – Command Dword 11", ["Bits", "Description"],
             [["31:16", "Other (OTH): something else."], ["15:00", "Reserved"]], None)]
    build(conn, tmp_path, [("base-2.4.pdf", base)])
    catalog.build_catalog(conn, 1)
    keys = {r["key"] for r in rows(conn, "SELECT key FROM catalog_item WHERE kind='command'")}
    assert keys == {"cmd:admin:10", "cmd:admin:11", "cmd:admin:12", "cmd:admin:13", "cmd:admin:14"}      # whole 11h-14h range; vendor / reserved rows skipped
    kinds = {r["kind"] for r in rows(conn, "SELECT kind FROM catalog_item WHERE key LIKE 'cmd:admin:10/dw10[31:16]%' AND key<>'cmd:admin:10/dw10[31:16]'")}
    assert kinds == {"boundary"}          # Figure 3 is a dword layout table (31:16 parses as a range!), never a value enumeration


def test_multi_revision_value_table_follows_the_users_choice(conn, tmp_path):
    cs = [T("NVM Command Set Specification", "1.3"), ("h", 1, "3 I/O Commands"),
          ("table", "Figure 20: Opcodes for NVM Command Set", ["Opcode", "O/M", "Command"], [["02h", "M", "Read"]], None),
          ("h", 2, "3.2 Read command"),
          ("table", "Figure 30: Read – Command Dword 10", ["Bits", "Description"],
           [["31:08", "Reserved"], ["07:00", "Mode (MODE): selects a mode. Refer to Figure 103 of the NVM Express Base Specification."]], None)]
    mk = lambda rev, vals: [T("Base Specification", rev), ("h", 1, "1 Intro"), ("table", "Figure 103: Modes", ["Value", "Description"], vals, None)]
    build(conn, tmp_path, [("cs.pdf", cs), ("b24.pdf", mk("2.4", [["00h", "Alpha"], ["01h", "Beta"]])),
                           ("b23.pdf", mk("2.3", [["00h", "Gamma"], ["01h", "Delta"], ["02h", "Epsilon"]]))])
    res = catalog.build_catalog(conn, 1)
    count = lambda: conn.execute("SELECT COUNT(*) FROM catalog_item WHERE kind='value' AND key LIKE 'cmd:io:nvm:02/dw10[07:00]=%'").fetchone()[0]
    assert count() == 0 and any(i["kind"] == "ambiguous_value_table" for i in res["issues"])        # no guessing between revisions
    amb = next(r for r in refs.resolve_references(conn, 1) if r["state"] == "ambiguous")
    refs.choose_reference(conn, 1, amb["src_key"], amb["raw"], next(c for c in amb["candidates"] if c.startswith("nvme-base@2.3")))
    catalog.build_catalog(conn, 1)
    assert count() == 3                                                                              # Base 2.3's three values


def test_clearing_an_override_restores_the_generated_value(conn, parsed_set):
    key = next(r["key"] for r in rows(conn, "SELECT key,observability FROM catalog_item WHERE kind='requirement'") if r["observability"] == "stimulus")
    catalog.set_override(conn, 1, key, "unobservable", "internal behaviour")
    assert conn.execute("SELECT observability FROM catalog_item WHERE key=?", (key,)).fetchone()[0] == "unobservable"
    catalog.set_override(conn, 1, key, None, None)
    assert conn.execute("SELECT observability FROM catalog_item WHERE key=?", (key,)).fetchone()[0] == "stimulus"
    assert conn.execute("SELECT COUNT(*) FROM item_override").fetchone()[0] == 0


# ------------------------------------------------------------------ cmddb.py

def test_non_numeric_sequence_is_recorded_and_import_continues(conn, parsed_set, tmp_path):
    from specforge.devtools.synth import sqe

    con = sqlite3.connect(tmp_path / "s.db")
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq TEXT, interface TEXT, queue TEXT, sqe BLOB)")
    for i, seq in enumerate(["1", "abc", "3"], start=1):
        con.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)", (i, "t", seq, "pcie", "admin", sqe(6, cdw10=i)))
    con.commit(); con.close()
    sid = cmddb.register_source(conn, str(tmp_path / "s.db"))
    r = cmddb.import_source(conn, sid)
    assert (r["rows"], r["decoded"], r["undecodable"]) == (3, 2, 1) and r["errors"][0]["row"] == 2


def test_mi_only_database_defaults_to_the_mi_interface(conn, tmp_path):
    con = sqlite3.connect(tmp_path / "mi.db")
    con.execute("CREATE TABLE commands(id INTEGER PRIMARY KEY, test_name TEXT, seq INTEGER, mi_msg BLOB)")
    for i in (1, 2):
        con.execute("INSERT INTO commands VALUES(?,?,?,?)", (i, "t", i, mi_msg(nmimt=1, opcode=0, nmd0=i << 24)))
    con.commit(); con.close()
    m = cmddb.suggest_mapping(str(tmp_path / "mi.db"))["mapping"]
    assert m["interface"] == {"const": "mi"}
    sid = cmddb.register_source(conn, str(tmp_path / "mi.db"), m)
    r = cmddb.import_source(conn, sid)
    assert (r["decoded"], r["undecodable"]) == (2, 0)


# ------------------------------------------------------------------ db.py

def test_deleting_a_spec_set_removes_its_decisions(conn):
    conn.execute("INSERT INTO spec_set(name) VALUES('a')")
    conn.execute("INSERT INTO ref_choice VALUES(1,'src','raw','chosen')")
    conn.execute("INSERT INTO item_override VALUES(1,'k','unobservable','why')")
    conn.commit()
    conn.execute("DELETE FROM spec_set WHERE id=1"); conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM ref_choice").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM item_override").fetchone()[0] == 0
    conn.execute("INSERT INTO spec_set(name) VALUES('b')"); conn.commit()          # the id may be reused: it must start clean
    assert conn.execute("SELECT id FROM spec_set WHERE name='b'").fetchone()[0] == 1
    assert connect is not None and coverage is not None
