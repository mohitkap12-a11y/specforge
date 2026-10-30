"""The MCP endpoint for coding agents: spec lookups (specquery), the tools over Streamable HTTP, the admin switch, rate limit
and status page, and access without sign-in."""
import pytest
from fastapi.testclient import TestClient
from test_compare import v_new

from specforge.devtools.synth import base_spec, write_docx
from specforge.ingest import store
from specforge.specquery import NotFound, QueryError, SpecLibrary, logical_tables, name_score, paginate

H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
LOCAL = "http://127.0.0.1:8000"      # without sign-in (the conftest default) the endpoint only answers requests addressed to localhost


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


def test_versions_named_in_the_spec_and_previous_relative_to_the_target(lib):
    assert [d["version"] for d in lib._pair("nvme-base@2.4", None, None)] == ["2.3", "2.4"]
    with pytest.raises(NotFound, match="older than 2.3"):          # 2.3 was asked for: not silently the latest pair
        lib.changes("nvme-base@2.3")
    with pytest.raises(NotFound, match="older than 2.3"):
        lib.diff("NVMe Base 2.3", "Identify")
    assert [d["version"] for d in lib._pair("base", "previous", "2.4")] == ["2.3", "2.4"]
    with pytest.raises(NotFound, match="older than 2.3"):          # previous of 2.3, not of the latest
        lib._pair("base", "previous", "2.3")


def test_a_failed_comparison_is_not_started_again_on_every_call(lib, conn, monkeypatch):
    from specforge import compare

    def boom(*a, **k):
        raise compare.CompareError("broken parse run")
    monkeypatch.setattr(compare, "run_comparison", boom)
    with pytest.raises(QueryError, match="broken parse run"):
        lib.changes("base")
    with pytest.raises(QueryError, match="failed recently"):
        lib.diff("base", "Identify")
    assert conn.execute("SELECT COUNT(*) FROM job WHERE type='compare'").fetchone()[0] == 1


def test_read_raw_serves_converted_files_by_name(lib, conn, ws):
    """A file converted on the To Markdown page but not parsed is read by its file name, even when the name looks like a spec."""
    import json

    folder = ws / "conversions" / "c1"
    folder.mkdir(parents=True)
    (folder / "out.md").write_text("# ECN 123\n\nIntro.\n\nThe new field NEWFLD is 4 bytes.\n\nEnd.\n", encoding="utf-8")
    conn.execute("INSERT INTO job(type,state,payload_json,result_json) VALUES('convert-markdown','done',?,?)",
                 (json.dumps({"filename": "nvme-mi-ecn-123.pdf", "sha256": "abc"}), json.dumps({"dir": "c1"})))
    conn.commit()
    assert "nvme-mi-ecn-123.pdf" in lib.list_specs()
    md = lib.raw("nvme-mi-ecn-123.pdf")
    assert "ECN 123" in md and "NEWFLD" in md
    assert "NEWFLD" in lib.raw("ecn-123", around="NEWFLD", context=5)
    with pytest.raises(NotFound, match="does not occur"):
        lib.raw("nvme-mi-ecn-123.pdf", around="nothing like this")
    with pytest.raises(QueryError, match="no parsed specification or converted file"):
        lib.raw("frobnicator manual")


LIB_MD = """---
title: "ECN 123: New Feature"
source: "nvme-ecn-123.pdf"
engine: builtin
---

###### Table of Contents

5.2.14 Identify command ........ 12

### 5.2.14 Identify command

The Identify command returns a data buffer.

**5.2.14.2.1**

**Identify Controller Data Structure (CNS 01h)**

The Identify Controller data structure is returned to the host.

**Figure 338: Identify – Identify Controller Data Structure**

| Bytes |  | Description |
| --- | --- | --- |
|  |  | 1 |
| 77 |  | Maximum Data Transfer Size (MDTS): the largest transfer in units of CAP.MPSMIN. |

**Figure 338: Identify – Identify Controller Data Structure**

| Bytes |  | Description |
| --- | --- | --- |
| 79:78 |  | Controller ID (CNTLID): the NVM subsystem unique controller identifier. |

### 5.2.15 Keep Alive command

The Keep Alive command restarts the Keep Alive Timer when the keep alive timeout expires.
"""


def test_converted_documents_are_searched_and_opened_by_section_or_lines(lib, conn, ws):
    """A converted-only document is indexed from its Markdown: section text, tables (continuations stitched) and rows, each hit
    naming the read_raw call that opens it; deleting the conversion drops its entries."""
    import json

    from specforge.specquery import library_entries

    folder = ws / "conversions" / "c9"
    folder.mkdir(parents=True)
    (folder / "out.md").write_text(LIB_MD, encoding="utf-8")
    conn.execute("INSERT INTO job(type,state,payload_json,result_json) VALUES('convert-markdown','done',?,?)",
                 (json.dumps({"filename": "nvme-ecn-123.pdf", "sha256": "e123"}), json.dumps({"dir": "c9"})))
    conn.commit()

    e = library_entries(LIB_MD, "lib:c9", "nvme-ecn-123.pdf")
    tables = [x for x in e if x[5] == "table"]
    assert len(tables) == 1 and tables[0][0] == "338" and tables[0][4].startswith("lib:c9|5.2.14.2.1|T")
    rows_ = [x[2] for x in e if x[5] == "row"]
    assert rows_ == ["77 | Maximum Data Transfer Size (MDTS): the largest transfer in units of CAP.MPSMIN.",
                     "79:78 | Controller ID (CNTLID): the NVM subsystem unique controller identifier."]   # no spacer, no repeated header
    assert not any("........" in x[2] for x in e)                                                        # no table of contents

    md, n = lib.search("maximum data transfer size")
    assert n >= 1 and "nvme-ecn-123.pdf (converted, not parsed), §5.2.14.2.1" in md and "Figure 338" in md
    line = LIB_MD.splitlines().index("| 77 |  | Maximum Data Transfer Size (MDTS): the largest transfer in units of CAP.MPSMIN. |") + 1
    assert f"line {line}" in md and f'lines="{line}"' in md
    md, _ = lib.search("keep alive timeout expires", spec="ecn-123")
    assert md.splitlines()[2].startswith("Searched: nvme-ecn-123.pdf")
    assert '§5.2.15 Keep Alive command' in md and 'read_raw(spec="nvme-ecn-123.pdf", section="5.2.15")' in md

    sec = lib.raw("nvme-ecn-123.pdf", section="5.2.14.2.1")
    assert sec.startswith("# nvme-ecn-123.pdf — §5.2.14.2.1 Identify Controller Data Structure (CNS 01h), lines")
    assert "CNTLID" in sec and "Keep Alive" not in sec
    assert "CNTLID" in lib.raw("ecn-123", section="Identify command") and "Keep Alive" not in lib.raw("ecn-123", section="5.2.14")
    assert "Keep Alive Timer" in lib.raw("ecn-123", section="keep alive command")
    assert lib.raw("ecn-123", lines=f"{line}-{line}").endswith("CAP.MPSMIN. |")
    with pytest.raises(NotFound, match="no section"):
        lib.raw("ecn-123", section="9.9")

    lib.library_search = False                       # the admin switch: not searched, still indexed and readable
    md, _ = lib.search("maximum data transfer size")
    assert "nvme-ecn-123.pdf" not in md and "converted documents are not searched" in md
    with pytest.raises(QueryError, match="turned off"):
        lib.search("keep alive", spec="ecn-123")
    assert "Keep Alive Timer" in lib.raw("ecn-123", section="5.2.15")
    assert lib.lib_index_state()["c9"]["entries"] == len(e)
    lib.library_search = True

    conn.execute("DELETE FROM job WHERE type='convert-markdown'")
    conn.commit()
    assert lib.prune_lib_index() == 1
    assert conn.execute("SELECT COUNT(*) FROM mcp_fts WHERE run_id='lib:c9'").fetchone()[0] == 0


def test_usage_figures_reset_and_forget_idle_clients(monkeypatch):
    from specforge import mcp_server

    u = mcp_server.Usage()
    u.seen("10.0.0.1")
    u.record("search_spec", {"query": "x"}, 5.0, "miss")
    assert u.snapshot()["misses"] == 1
    u.reset()
    assert u.snapshot()["calls"] == 0 and u.snapshot()["client_count"] == 0
    monkeypatch.setattr(mcp_server, "MAX_CLIENTS", 3)
    for i in range(3):
        u.seen(f"10.0.0.{i}")
    u.clients["10.0.0.0"]["last_seen"] -= 2 * 86400            # idle for two days
    u.seen("10.0.0.9")
    assert set(u.clients) == {"10.0.0.1", "10.0.0.2", "10.0.0.9"}


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

    with TestClient(create_app(), base_url=LOCAL) as c:
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
        assert info["url"] == "http://127.0.0.1:8000/mcp" and info["sse_url"].endswith("/mcp/sse") and len(info["tools"]) == len(names)


def test_admin_switch_rate_limit_and_public_url(ws):
    from specforge.api.app import create_app

    with TestClient(create_app(), base_url=LOCAL) as c:
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
        assert c.get("/api/mcp/status").json()["settings"]["library_search"] is True     # on by default
        assert c.put("/api/mcp/settings", json={"library_search": "no"}).status_code == 422
        assert c.put("/api/mcp/settings", json={"library_search": False}).json()["library_search"] is False


def test_without_sign_in_only_localhost_requests_are_answered(ws):
    """Sign-in off means a localhost-only server: a page whose host name was rebound to 127.0.0.1 must not use it."""
    from specforge.api.app import create_app

    with TestClient(create_app(), base_url=LOCAL) as c:
        assert rpc(c, "tools/list", {})[0] == 200
        assert c.post("/mcp", headers={**H, "host": "evil.example:8000"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
        assert c.post("/mcp", headers={**H, "origin": "http://evil.example"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 403
        assert c.get("/mcp/sse", headers={"host": "evil.example"}).status_code == 403


def test_a_diff_over_http_waits_for_its_comparison_without_hanging(lib, monkeypatch):
    import threading

    from specforge import compare
    from specforge.api.app import create_app

    gate = threading.Event()
    real = compare.run_comparison
    monkeypatch.setattr(compare, "run_comparison", lambda *a, **k: (gate.wait(20), real(*a, **k))[1])
    with TestClient(create_app(), base_url=LOCAL) as c:
        c.put("/api/mcp/settings", json={"compare_wait_s": 1})
        __import__("time").sleep(2.1)
        st, r = rpc(c, "tools/call", {"name": "diff", "arguments": {"spec": "base", "item": "Identify"}})
        assert st == 200 and "being computed" in r["result"]["content"][0]["text"]
        assert c.get("/api/health").status_code == 200
        gate.set()
        for _ in range(40):
            st, r = rpc(c, "tools/call", {"name": "diff", "arguments": {"spec": "base", "item": "Identify"}})
            if "being computed" not in r["result"]["content"][0]["text"]:
                break
            __import__("time").sleep(0.25)
        assert "row added: 11h | O | Namespace Group list" in r["result"]["content"][0]["text"]


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
