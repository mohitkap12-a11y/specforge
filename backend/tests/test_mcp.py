"""The MCP endpoint for coding agents: spec lookups (specquery), the tools over Streamable HTTP, the admin switch, rate limit
and status page, and access without sign-in."""
import pytest
from fastapi.testclient import TestClient
from test_compare import v_new

from specforge.devtools.synth import base_spec, write_docx
from specforge.ingest import store
from specforge.specquery import NotFound, QueryError, SpecLibrary, logical_tables, name_score, paginate

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture()
def lib(conn, demo_dir, tmp_path):
    """Base 2.3 and 2.4 (2.4 is test_compare's 'newer revision'), NVM Command Set 1.3 and NVMe-MI 2.0."""
    for ver, blocks in (("2.3", base_spec()), ("2.4", v_new())):
        p = write_docx(tmp_path / f"base-{ver}.docx", blocks, numbered_text=True)
        d = store.register_document(conn, p, "nvme-base", ver)
        store.run_parse(conn, d["id"], workers=1, activate=True)
    for name in ("nvme-nvm-cs-1.3.pdf", "nvme-mi-2.0.pdf"):
        d = store.register_document(conn, demo_dir / name)
        store.run_parse(conn, d["id"], workers=1, activate=True)
    return SpecLibrary(conn_factory=lambda: conn)


# ----------------------------------------------------------------------------- lookups

def test_spec_names_versions_and_the_latest_default(lib):
    for name in ("nvme-base", "base", "NVMe Base Specification", "NVM Express Base Specification", "nvme 2.3"):
        assert lib.resolve_doc(name)[0]["family"] == "nvme-base"
    assert lib.resolve_doc("mi")[0]["family"] == "nvme-mi"
    assert lib.resolve_doc("NVM Command Set")[0]["family"] == "nvme-nvm-cs"
    d, note = lib.resolve_doc("base")
    assert d["version"] == "2.4" and "latest parsed version" in note
    assert lib.resolve_doc("base", "2.4")[1] == ""                      # asked for: no note
    assert lib.resolve_doc("nvme-base@2.3")[0]["version"] == "2.3"
    assert lib.resolve_doc("base", "v2.3")[0]["version"] == "2.3"
    assert lib.resolve_doc("base", "previous")[0]["version"] == "2.3"
    with pytest.raises(NotFound, match="Parsed versions: 2.3, 2.4"):
        lib.resolve_doc("base", "1.4")
    with pytest.raises(NotFound, match="no parsed specification matches"):
        lib.resolve_doc("pcie")
    assert "nvme-mi" in lib.list_specs() and "2.3" in lib.list_specs()


def test_search_expands_abbreviations_and_finds_table_rows(lib):
    md, n = lib.search("identify cdw10 cns")
    assert n and md.index("Figure 101") < md.index("Open a hit")           # Identify - Command Dword 10
    assert "CNS" in md and "specforge://nvme-base@2.4/fig:101" in md
    md, n = lib.search("NUMDL", spec="base", version="2.3")
    assert n and "Figure 120" in md and "nvme-base@2.3" in md
    assert lib.search("Figure 103", spec="base")[0].count("Figure 103") >= 1
    md, n = lib.search("xyzzy quux")
    assert n == 0 and "No matches" in md


def test_index_is_built_once_per_parse_run(lib):
    d, _ = lib.resolve_doc("base")
    lib.ensure_index(d)
    assert lib.ensure_index(d) is False
    assert lib.ensure_index(d, force=True) is True
    assert lib.index_state()[d["run_id"]]["entries"] > 0


def test_section_and_table(lib):
    md = lib.section("base", "2.4", "5.2")
    assert md.startswith("_Version not given") is False and "# 5.2 Identify command" in md
    assert "shall complete the command within 5 seconds" in md and "Figure 101" in md and "Sub-sections" in md
    assert "5.2.1 Identify Namespace data structure" in lib.section("base", "2.4", "5.2", depth=1)
    assert "# 5.2 Identify command" in lib.section("base", None, "Identify command")
    t = lib.table("base", "2.3", "103")
    assert "| 10h | O | Allocated Namespace ID list | 5.2.5 |" in t and "11h" not in t
    with pytest.raises(NotFound):
        lib.section("base", None, "99.9")


def test_commands_and_command_dword_fields(lib):
    md = lib.command("Identify")
    assert "Admin 06h" in md and "§5.2" in md and "Identify – Command Dword 10" in md and "Identify – Command Dword 11" in md
    assert lib.command("admin 06h").splitlines()[0] == md.splitlines()[0]
    assert "NVM Command Set 1.3" in lib.command("Read") and "Read NVMe-MI" not in lib.command("Read")   # one-word name: needs 'command'
    assert "| Identify | Admin 06h | §5.2 | CDW10, CDW11 |" in lib.list_commands("base", "2.4")

    dw10 = lib.command_field("Identify", dword="CDW10")
    assert "Controller or Namespace Structure (CNS)" in dw10
    assert "Referenced tables" in dw10 and "Identify – CNS Values" in dw10 and "Namespace Group list" in dw10   # Figure 103, followed
    assert lib.command_field("identify", dword="dword 10") == dw10
    cns = lib.command_field("Identify", field="CNS", spec="base", version="2.3")
    assert "1 of 3 rows match" in cns and "CNTID" not in cns.split("Referenced tables")[0].split("| 07:00")[1]
    assert "The CNS field is defined in Figure 103." in cns
    with pytest.raises(NotFound, match="Command Dword tables: CDW10, CDW11"):
        lib.command_field("Identify", dword="12")
    with pytest.raises(NotFound, match="no field 'BOGUS'"):
        lib.command_field("Identify", field="BOGUS")
    with pytest.raises(NotFound):
        lib.command("Frobnicate")


def test_structures_by_offset_and_field(lib):
    md = lib.structure("Identify Namespace", offset="8")
    assert "1 of 2 rows match" in md and "NCAP" in md and "NSZE" not in md
    assert "NSZE" in lib.structure("Identify Namespace", field="NSZE")
    assert "NCAP" in lib.structure("Identify Namespace", offset="0Fh")
    with pytest.raises(NotFound):
        lib.structure("Identify Namespace", offset="64")


def test_references_follow_other_specifications(lib):
    md = lib.reference("Refer to section 3.1 of the NVM Express NVM Command Set Specification", "base")
    assert "3.1 Command Set Overview" in md and "NVM Command Set 1.3" in md and "Figure 20" in md
    assert "Identify – CNS Values" in lib.reference("Figure 103", "base", "2.3")
    assert "# 5.2 Identify command" in lib.reference("5.2", "base")


def test_changes_and_diff_are_computed_once_through_the_job_queue(lib, conn):
    md = lib.changes("base")
    assert "NVMe Base 2.3 → 2.4" in md and "Commands modified" in md and "Identify" in md and "Namespace Attachment" in md
    assert conn.execute("SELECT COUNT(*) FROM job WHERE type='compare' AND user='mcp' AND state='done'").fetchone()[0] == 1
    lib.changes("base", "2.3", "2.4")
    assert conn.execute("SELECT COUNT(*) FROM job WHERE type='compare'").fetchone()[0] == 1       # kept, not computed again
    d = lib.diff("base", "Identify")
    assert "row added: 11h | O | Namespace Group list" in d                                     # Figure 103
    assert "row removed: 23:16 | Reserved" in d                                                 # Figure 102 (breaking)
    assert "added (mandatory): The controller shall complete the command within 5 seconds." in d
    assert "“Reserved” → “Namespace Group Identifier (NSGID)" in d
    assert "11h" in lib.diff("base", "Figure 103") and "Namespace Attachment" not in lib.diff("base", "Figure 103")
    gf = lib.diff("base", "Get Features")                                                       # only renumbered
    assert "No change: 'Get Features' is the same in both versions. Only its section number changed: §5.5 → §5.6." in gf
    assert "No change: 'Abort' is the same in both versions." in lib.diff("base", "Abort")
    assert "[breaking]" in lib.changes("base", impact="breaking") and "[new]" not in lib.changes("base", impact="breaking").split("Change list")[1]
    with pytest.raises(NotFound, match="older than 2.3"):
        lib.changes("base", to_version="2.3")
    with pytest.raises(QueryError):
        lib.changes("base", impact="huge")


def test_a_slow_comparison_tells_the_agent_to_retry(lib, conn, monkeypatch):
    from specforge import compare

    gate = __import__("threading").Event()
    real = compare.run_comparison
    monkeypatch.setattr(compare, "run_comparison", lambda *a, **k: (gate.wait(10), real(*a, **k))[1])
    d_from, d_to = lib._pair("base", None, None)
    assert lib.comparison(d_from, d_to, wait=0.3) is None
    assert lib.comparison(d_from, d_to, wait=0.1) is None                                       # joins the running job
    assert conn.execute("SELECT COUNT(*) FROM job WHERE type='compare'").fetchone()[0] == 1
    gate.set()
    assert lib.comparison(d_from, d_to, wait=10) is not None


def test_tables_cut_over_pages_are_read_as_one():
    """As the PDF parser leaves the real Identify Controller structure: spacer columns, the caption repeated on later pages,
    uncaptioned pieces in between, a row repeated at a page break, and a caption fragment sharing the figure number."""
    def t(label, caption, rows, section="5.2.14.2.1", captioned=True, header=("Bytes", "", "Description", "")):
        return label, {"label": label, "key": f"x/fig:{label}", "caption": caption, "header": list(header), "rows": rows, "section": section,
                       "captioned": captioned, "page_from": 1, "page_to": 1, "quality": 0.5}
    name = "Figure 338: Identify Controller Data Structure"
    raw = dict([t("338", "Unused entries are zero filled.", [], section="4.6.1"),
                t("338#2", name, [["01:00", "", "PCI Vendor ID (VID)", ""], ["03:02", "", "Subsystem Vendor ID (SSVID)", ""]]),
                t("p367t1", "", [["03:02", "", "Subsystem Vendor ID (SSVID)", ""], ["77", "", "Maximum Data Transfer Size (MDTS)", ""]], captioned=False),
                t("338#3", name, [["79:78", "", "Controller ID (CNTLID)", ""]]),
                t("339", "Figure 339: Power State Descriptor", [["31:00", "", "Maximum Power (MP)", ""]])])
    tables, alias = logical_tables(raw)
    assert list(tables) == ["338", "338#2", "339"]
    whole = tables["338#2"]
    assert whole["header"] == ["Bytes", "Description"] and [r[0] for r in whole["rows"]] == ["01:00", "03:02", "77", "79:78"]
    assert whole["parts"] == ["338#2", "p367t1", "338#3"] and alias["p367t1"] == alias["338#3"] == "338#2"
    assert SpecLibrary.fig({"tables": tables, "_alias": alias}, "338") is whole           # not the caption fragment
    assert name_score("Identify Controller", "Identify – Identify Controller Data Structure") > \
        name_score("Identify Controller", "Identify – Primary Controller Capabilities Data Structure")


def test_paging_cuts_long_answers_at_lines():
    text = "\n".join(f"line {i:04d} " + "x" * 50 for i in range(400))
    p1 = paginate(text, 1, limit=5000)
    assert "Page 1 of" in p1 and "page=2" in p1 and p1.split("\n---\n")[0].endswith("\n")
    total = int(p1.rsplit("of ", 1)[1].split(".")[0])
    joined = "".join(paginate(text, i, limit=5000).split("\n\n---\n_Page")[0] for i in range(1, total + 1))
    assert joined == text
    with pytest.raises(QueryError):
        paginate(text, total + 1, limit=5000)


# ----------------------------------------------------------------------------- over HTTP

def rpc(c, method, params=None, i=1):
    r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": i, "method": method, **({"params": params} if params is not None else {})})
    return r.status_code, r.json() if r.content else None


def test_tools_over_streamable_http(lib):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        st, init = rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        assert st == 200 and init["result"]["serverInfo"]["name"] == "specforge" and "get_command_field" in init["result"]["instructions"]
        st, tl = rpc(c, "tools/list", {}, 2)
        names = {t["name"] for t in tl["result"]["tools"]}
        assert {"list_specs", "search_spec", "get_section", "get_table", "get_command", "get_command_field", "get_structure",
                "resolve_reference", "list_changes", "diff", "read_raw", "list_commands"} <= names
        assert all(t["annotations"]["readOnlyHint"] for t in tl["result"]["tools"])
        st, r = rpc(c, "tools/call", {"name": "get_command_field", "arguments": {"command": "Identify", "dword": "10"}}, 3)
        assert not r["result"]["isError"] and "Identify – CNS Values" in r["result"]["content"][0]["text"]
        st, r = rpc(c, "tools/call", {"name": "get_section", "arguments": {"section": "99.9", "spec": "base"}}, 4)
        assert r["result"]["isError"] and "no section 99.9" in r["result"]["content"][0]["text"]
        st, r = rpc(c, "resources/read", {"uri": "specforge://nvme-base@2.3/fig:103"}, 5)
        assert "Allocated Namespace ID list" in r["result"]["contents"][0]["text"]

        s = c.get("/api/mcp/status").json()
        tools = {t["tool"]: t for t in s["usage"]["tools"]}
        assert tools["get_command_field"]["calls"] == 1 and tools["get_section"]["misses"] == 1
        assert s["usage"]["clients"][0]["calls"] == 2 and s["usage"]["recent_misses"][0]["tool"] == "get_section"
        assert {d["family"] for d in s["documents"]} == {"nvme-base", "nvme-mi", "nvme-nvm-cs"}
        info = c.get("/api/mcp/info").json()
        assert info["url"] == "http://testserver/mcp" and info["sse_url"].endswith("/mcp/sse") and len(info["tools"]) == len(names)


def test_admin_switch_rate_limit_and_public_url(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        assert c.put("/api/mcp/settings", json={"enabled": False}).status_code == 200
        st, r = rpc(c, "tools/list", {})
        assert st == 503 and "turned off" in r["error"]["message"]
        assert c.get("/api/mcp/info").json()["enabled"] is False
        c.put("/api/mcp/settings", json={"enabled": True, "rate_limit_per_min": 2})
        __import__("time").sleep(2.1)                     # the endpoint re-reads its settings every 2 s
        assert [rpc(c, "tools/list", {}, i)[0] for i in range(4)] == [200, 200, 429, 429]
        assert c.get("/api/mcp/status").json()["usage"]["rate_limited"] == 2
        assert c.put("/api/mcp/settings", json={"rate_limit_per_min": -1}).status_code == 422
        assert c.put("/api/mcp/settings", json={"public_url": "not a url"}).status_code == 422
        assert c.put("/api/mcp/settings", json={"nope": 1}).status_code == 422
        c.put("/api/mcp/settings", json={"public_url": "http://specforge.lab:8000/"})
        assert c.get("/api/mcp/info").json()["url"] == "http://specforge.lab:8000/mcp"


def test_endpoint_needs_no_sign_in_but_status_is_admin_only(ws, monkeypatch):
    monkeypatch.setenv("SPECFORGE_AUTH", "1")
    from specforge import auth
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    auth.create_user(get_conn(), "root", "rootpass1", role="admin", status="active")
    auth.create_user(get_conn(), "bob", "bobpass12", role="user", status="active")
    with TestClient(create_app(), base_url="http://sf.test") as c:
        assert rpc(c, "tools/list", {})[0] == 200                          # agents: no session, no key
        assert c.get("/api/mcp/info").status_code == 401                   # the web API still needs a session
        c.post("/api/auth/login", json={"username": "bob", "password": "bobpass12"})
        assert c.get("/api/mcp/info").status_code == 200
        assert c.get("/api/mcp/status").status_code == 403
        assert c.put("/api/mcp/settings", json={"enabled": False}).status_code == 403
        assert c.post("/api/mcp/reindex").status_code == 403
        c.post("/api/auth/login", json={"username": "root", "password": "rootpass1"})
        assert c.get("/api/mcp/status").status_code == 200
        r = c.post("/api/mcp/reindex")
        assert r.status_code == 200 and r.json()["job"]
