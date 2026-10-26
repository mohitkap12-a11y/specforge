"""Spec version comparison: matching across renumbering, sentence/table diffs, impact, storage, API, AI summary, export."""
import pytest
from fastapi.testclient import TestClient

from specforge import compare
from specforge.devtools.synth import base_spec, write_docx
from specforge.ingest import store


def v_new():
    """The base spec as a 'newer revision': one requirement weakened, a value edited, a field defined, a row removed, a section inserted
    (which renumbers every later section) and one section dropped."""
    out = []
    for b in base_spec():
        if b[0] == "p" and b[1].startswith("This synthetic document imitates"):
            out.append(("p", b[1].replace("A controller shall process", "A controller should process")))
        elif b[0] == "p" and b[1].startswith("The Identify command returns"):
            out.append(("p", b[1].replace("a non-zero value", "a value other than zero") + " The controller shall complete the command within 5 seconds."))
        elif b[0] == "table" and b[1].startswith("Figure 101"):
            rows = [list(r) for r in b[3]]
            rows[1] = ["15:08", "Namespace Group Identifier (NSGID): This field specifies the namespace group."]    # was Reserved
            out.append(("table", b[1], b[2], rows, b[4]))
        elif b[0] == "table" and b[1].startswith("Figure 102"):
            out.append(("table", b[1], b[2], [list(r) for r in b[3] if r[0] != "23:16"], b[4]))                       # a row is gone
        elif b[0] == "table" and b[1].startswith("Figure 103"):
            rows = [list(r) for r in b[3]]
            rows[2][2] = "Active Namespace ID list (updated)"
            rows.append(["11h", "O", "Namespace Group list", "5.2.6"])
            out.append(("table", b[1], b[2], rows, b[4]))
        elif b[0] == "h" and b[2].startswith("5.3 Get Log Page"):
            out += [("h", 2, "5.3 Namespace Attachment command"),
                    ("p", "The Namespace Attachment command attaches or detaches a namespace. The controller shall support attach and detach operations."),
                    ("table", "Figure 130: Namespace Attachment – Command Dword 10", ["Bits", "Description"],
                     [["03:00", "Select (SEL): This field selects attach or detach."]], None),
                    ("h", 2, "5.4 Get Log Page command")]
        elif b[0] == "h" and b[2].startswith("5.4 Set Features"):
            out.append(("h", 2, "5.5 Set Features command"))
        elif b[0] == "h" and b[2].startswith("5.5 Get Features"):
            out.append(("h", 2, "5.6 Get Features command"))
        else:
            out.append(b)
    return out


@pytest.fixture()
def two_versions(conn, tmp_path):
    ids = {}
    for ver, blocks in (("2.3", base_spec()), ("2.4", v_new())):
        p = write_docx(tmp_path / f"base-{ver}.docx", blocks, numbered_text=True)
        d = store.register_document(conn, p, "nvme-base", ver)
        rid = store.run_parse(conn, d["id"], workers=1, activate=True)
        ids[ver] = rid
    return ids


def by(changes, kind, change, title_part):
    return [c for c in changes if c["kind"] == kind and c["change"] == change and title_part.lower() in c["title"].lower()]


def run(conn, ids):
    cid = compare.run_comparison(conn, ids["2.3"], ids["2.4"])
    return cid, compare.query_changes(conn, cid, limit=500)["items"], compare.get_comparison(conn, cid)


def test_renumbered_sections_are_moved_not_removed_and_added(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    moved = by(ch, "section", "moved", "Get Log Page")
    assert moved and moved[0]["number_from"] == "5.3" and moved[0]["number_to"] == "5.4" and moved[0]["impact"] == "editorial"
    assert not by(ch, "section", "added", "Get Log Page") and not by(ch, "section", "removed", "Get Log Page")
    assert {c["title"] for c in ch if c["change"] == "moved" and c["kind"] == "section"} >= {"Get Log Page command", "Set Features command", "Get Features command"}


def test_new_section_and_table_are_added(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    sec = by(ch, "section", "added", "Namespace Attachment")
    assert sec and sec[0]["impact"] == "new" and sec[0]["normative"] >= 1 and sec[0]["number_to"] == "5.3"
    tbl = by(ch, "table", "added", "Namespace Attachment")
    assert tbl and tbl[0]["impact"] == "new" and tbl[0]["details"]["row_count"] == 1
    assert tbl[0]["nested"]                                           # it belongs to the new section, so it is not counted separately
    assert comp["summary"]["roots"]["sections_added"] == 1 and comp["summary"]["roots"]["tables_added"] == 0


def test_weakened_requirement_is_breaking_with_word_delta(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    intro = by(ch, "section", "modified", "Introduction")[0]
    assert intro["impact"] == "breaking"
    m = intro["details"]["modified"][0]
    assert m["strength"] == ["mandatory", "recommended"] and m["delta"] == {"removed": ["shall"], "added": ["should"]}
    assert comp["summary"]["normative"]["strength_changed"] >= 1


def test_added_requirement_and_edited_sentence_in_existing_section(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    ident = by(ch, "section", "modified", "Identify command")[0]
    d = ident["details"]
    assert any("within 5 seconds" in s["text"] and s["strength"] == "mandatory" for s in d["added"])
    assert any("non-zero value" in s["before"] for s in d["modified"])
    assert ident["number_from"] == ident["number_to"] == "5.2"


def test_table_row_level_changes(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    t101 = by(ch, "table", "modified", "Dword 10")[0]
    row = next(r for r in t101["details"]["rows_changed"] if r["key"] == "15:08")
    assert row["cells"][0]["before"] == "Reserved" and "NSGID" in row["cells"][0]["after"]
    assert t101["impact"] == "new"                                  # a reserved field became defined
    t102 = by(ch, "table", "modified", "Dword 11")[0]
    assert t102["impact"] == "breaking" and t102["details"]["rows_removed"][0][0] == "23:16"
    t103 = by(ch, "table", "modified", "CNS Values")[0]
    assert [r[0] for r in t103["details"]["rows_added"]] == ["11h"]
    assert t103["details"]["rows_changed"][0]["key"] == "02h"


def test_unchanged_tables_and_sections_produce_no_change(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    assert not by(ch, "table", "modified", "Opcodes for Admin")        # Figure 100 identical in both versions
    assert not by(ch, "section", "modified", "Admin Command Overview")
    assert comp["summary"]["total"] == len(ch)


def test_identical_versions_have_no_changes(conn, tmp_path):
    rids = []
    for ver in ("1", "2"):
        d = store.register_document(conn, write_docx(tmp_path / f"s{ver}.docx", base_spec()), "nvme-base", ver)
        rids.append(store.run_parse(conn, d["id"], workers=1, activate=True))
    cid = compare.run_comparison(conn, rids[0], rids[1])
    assert compare.query_changes(conn, cid)["total"] == 0
    assert "no content differences" in compare.get_comparison(conn, cid)["overview"]


def test_filters_paging_and_overview(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    br = compare.query_changes(conn, cid, impact="breaking")
    assert br["total"] >= 2 and all(c["impact"] == "breaking" for c in br["items"])
    assert compare.query_changes(conn, cid, kind="table")["total"] == sum(1 for c in ch if c["kind"] == "table")
    assert compare.query_changes(conn, cid, q="NSGID")["total"] == 1
    assert len(compare.query_changes(conn, cid, limit=2)["items"]) == 2
    top = compare.query_changes(conn, cid, sort="importance", limit=1)["items"][0]
    assert top["score"] == max(c["score"] for c in ch)
    ov = comp["overview"]
    assert "2.4 differs from 2.3" in ov and "new section" in ov and "Normative statements" in ov
    assert comp["summary"]["chapters"][0]["chapter"] in ("1", "5")


def test_recompare_replaces_and_rejects_bad_input(conn, two_versions):
    cid1 = compare.run_comparison(conn, two_versions["2.3"], two_versions["2.4"])
    cid2 = compare.run_comparison(conn, two_versions["2.3"], two_versions["2.4"])
    assert cid1 != cid2 and compare.get_comparison(conn, cid1) is None and len(compare.list_comparisons(conn)) == 1
    with pytest.raises(compare.CompareError, match="different"):
        compare.run_comparison(conn, two_versions["2.3"], two_versions["2.3"])
    with pytest.raises(compare.CompareError, match="not found"):
        compare.run_comparison(conn, two_versions["2.3"], 9999)


def test_markdown_export(conn, two_versions):
    cid, ch, comp = run(conn, two_versions)
    md = compare.export_markdown(conn, cid)
    assert md.startswith("# nvme-base: 2.3 → 2.4") and "## Changes" in md
    assert "Section 5.3: Namespace Attachment command — added · new" in md and "~~Reserved~~" in md
    assert "Get Log Page command — moved" not in md         # editorial renumbering is left out of the change list


@pytest.fixture()
def client(ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_api_options_compare_and_changes(client, tmp_path):
    for ver, blocks in (("2.3", base_spec()), ("2.4", v_new())):
        p = write_docx(tmp_path / f"b{ver}.docx", blocks)
        with open(p, "rb") as f:
            doc = client.post("/api/specs/upload", files=[("files", (p.name, f))]).json()[0]
        client.patch(f"/api/specs/{doc['id']}", json={"family": "nvme-base", "version": ver})
        job = client.post(f"/api/specs/{doc['id']}/parse?activate=true").json()["job"]
        import time
        for _ in range(300):
            j = client.get(f"/api/jobs/{job}").json()
            if j["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert j["state"] == "done", j
    opts = client.get("/api/compare/options").json()
    assert [(o["family"], o["version"], o["parsed"]) for o in opts] == [("nvme-base", "2.3", True), ("nvme-base", "2.4", True)]
    r = client.post("/api/compare", json={"from_run": opts[0]["run_id"], "to_run": opts[1]["run_id"]})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert r.json()["summary"]["total"] > 5 and r.json()["overview"]
    ch = client.get(f"/api/compare/{cid}/changes?impact=breaking&limit=5").json()
    assert ch["total"] >= 2 and ch["items"][0]["impact"] == "breaking"
    assert client.get(f"/api/compare/{cid}/export").headers["content-type"].startswith("text/markdown")
    assert client.get("/api/compare").json()[0]["id"] == cid
    assert client.post("/api/compare", json={"from_run": 1, "to_run": 1}).status_code == 422
    assert client.post("/api/compare", json={"from_run": "x", "to_run": 2}).status_code == 422
    assert client.get("/api/compare/9999").status_code == 404
    assert client.delete(f"/api/compare/{cid}").json()["ok"]
    assert client.get(f"/api/compare/{cid}").status_code == 404


# ----------------------------------------------------------------------------- AI summary

def _fake_model():
    from llm_helpers import Fake

    def reply(messages):
        system, user = messages[0]["content"], messages[1]["content"]
        if system.startswith("You compare two versions"):
            first = user.split("CHANGES:\n", 1)[1].split("\n", 1)[0]
            return json.dumps({"headline": "Chapter changes described.",
                               "points": [{"title": "Requirement weakened", "detail": "A mandatory statement became a recommendation.", "impact": "breaking", "refs": ["C1", "C999"]},
                                          {"title": "Other", "detail": "x", "impact": "changed", "refs": []}],
                               "seen_first_line": first})
        return json.dumps({"overview": "The new revision adds Namespace Attachment and relaxes one requirement.",
                           "highlights": [{"title": "Namespace Attachment added", "detail": "A new command.", "impact": "new"}],
                           "breaking": ["Process-command requirement is now a recommendation."], "upgrade_notes": ["Review tests that rely on the old wording."]})
    return Fake([reply])


import json  # noqa: E402


def test_ai_summary_cites_only_shown_changes_and_is_stored(conn, two_versions):
    from llm_helpers import settings
    from specforge import compare_ai
    from specforge.llm.service import LLMService

    cid, ch, comp = run(conn, two_versions)
    fake = _fake_model()
    stats = compare_ai.summarize(conn, cid, LLMService(conn, settings(), provider=fake))
    assert stats["failed"] == 0 and stats["summarised"] >= 1 and stats["dropped_refs"] >= 1       # C999 was never shown
    ai = compare.get_comparison(conn, cid)["ai"]
    assert "Namespace Attachment" in ai["overview"] and ai["breaking"] and ai["upgrade_notes"]
    ref_ids = {r["id"] for c in ai["chapters"] for p in c["points"] for r in p["refs"]}
    assert ref_ids and ref_ids <= {c["id"] for c in ch}
    calls = fake.calls
    compare_ai.summarize(conn, cid, LLMService(conn, settings(), provider=fake))                  # stored: no second pass
    assert fake.calls == calls
    compare_ai.summarize(conn, cid, LLMService(conn, settings(), provider=fake), redo=True)
    md = compare.export_markdown(conn, cid)
    assert "## AI summary" in md and "authoritative record" in md


def test_ai_summary_digest_shows_the_exact_edit(conn, two_versions):
    from specforge import compare_ai

    cid, ch, comp = run(conn, two_versions)
    intro = next(c for c in ch if c["kind"] == "section" and c["change"] == "modified" and c["title"] == "Introduction")
    text = compare_ai._describe(intro)
    assert "MODIFIED section 1 'Introduction' [breaking]" in text and "shall process" in text and "should process" in text
    tbl = next(c for c in ch if c["kind"] == "table" and "CNS Values" in c["title"])
    assert "+ row: 11h | O | Namespace Group list" in compare_ai._describe(tbl)


def test_ai_summary_failure_is_reported(conn, two_versions):
    from llm_helpers import Fake, settings
    from specforge import compare_ai
    from specforge.llm.base import LLMError
    from specforge.llm.service import LLMService

    cid, ch, comp = run(conn, two_versions)
    with pytest.raises(LLMError):
        compare_ai.summarize(conn, cid, LLMService(conn, settings(), provider=Fake(["not json at all"])))
    assert compare.get_comparison(conn, cid)["ai"] is None


def test_api_summarize_job(client, tmp_path, monkeypatch):
    import time

    from llm_helpers import settings

    from specforge import compare_ai
    from specforge.llm import service as svc_mod

    for ver, blocks in (("2.3", base_spec()), ("2.4", v_new())):
        p = write_docx(tmp_path / f"c{ver}.docx", blocks)
        with open(p, "rb") as f:
            doc = client.post("/api/specs/upload", files=[("files", (p.name, f))]).json()[0]
        client.patch(f"/api/specs/{doc['id']}", json={"family": "nvme-base", "version": ver})
        job = client.post(f"/api/specs/{doc['id']}/parse?activate=true").json()["job"]
        for _ in range(300):
            if client.get(f"/api/jobs/{job}").json()["state"] in ("done", "failed"):
                break
            time.sleep(0.1)
    opts = client.get("/api/compare/options").json()
    cid = client.post("/api/compare", json={"from_run": opts[0]["run_id"], "to_run": opts[1]["run_id"]}).json()["id"]
    fake = _fake_model()
    real = svc_mod.LLMService
    monkeypatch.setattr("specforge.api.app.LLMService", lambda c, st: real(c, settings(), provider=fake))
    job = client.post(f"/api/compare/{cid}/summarize").json()["job"]
    for _ in range(300):
        j = client.get(f"/api/jobs/{job}").json()
        if j["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert j["state"] == "done", j
    assert client.get(f"/api/compare/{cid}").json()["ai"]["overview"].startswith("The new revision")
    assert compare_ai.PROMPT_VERSION == "compare-v1"


def test_cli_compare_writes_report(ws, tmp_path, capsys):
    from specforge.cli import main
    from specforge.core.db import connect

    c = connect()
    for ver, blocks in (("2.3", base_spec()), ("2.4", v_new())):
        d = store.register_document(c, write_docx(tmp_path / f"cli{ver}.docx", blocks), "nvme-base", ver)
        store.run_parse(c, d["id"], workers=1, activate=True)
    main(["compare", "nvme-base", "2.3", "2.4", "--out", str(tmp_path / "r" / "report.md")])
    out = capsys.readouterr().out
    assert "2.4 differs from 2.3" in out
    assert "## Changes" in (tmp_path / "r" / "report.md").read_text()
    with pytest.raises(SystemExit, match="no parsed spec"):
        main(["compare", "nvme-base", "2.3", "9.9"])
