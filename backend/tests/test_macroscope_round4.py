"""Regression tests for the fourth round of Macroscope findings (review of 2f706bd)."""
import threading
import time

from specforge import catalog, cmddb, coverage, jobs, reports
from specforge.core.db import connect
from specforge.devtools.synth import write_pdf
from specforge.ingest.normalize import extract_references
from specforge.ingest.pdf import _detect_headers_footers, parse_pdf


def test_oxford_comma_reference_lists_keep_the_last_item():
    refs = extract_references("See sections 1.1, 2.4, and 3.5 and Figures 10, 11, or 12 and Table 20 and 21.", "nvme-base")
    got = {(r.kind, r.label) for r in refs}
    assert {("section", "1.1"), ("section", "2.4"), ("section", "3.5")} <= got
    assert {("figure", "10"), ("figure", "11"), ("figure", "12"), ("figure", "20"), ("figure", "21")} <= got
    assert len(got) == 8                                                       # and nothing invented


def test_explicit_na_on_a_field_survives_the_rollup(conn, parsed_set, demo_dir):
    catalog.set_override(conn, 1, "cmd:admin:06/dw10[07:00]", "unobservable", "decoded by the host, never submitted")
    src = cmddb.register_source(conn, str(demo_dir / "cmds-a.db")); cmddb.import_source(conn, src)
    run = coverage.run_coverage(conn, 1, src)
    st = conn.execute("SELECT state, detail_json FROM item_result WHERE run_id=? AND item_key='cmd:admin:06/dw10[07:00]'", (run,)).fetchone()
    assert st[0] == "na" and "decoded by the host, never submitted" in st[1]       # excluded, with the reviewer's own reason
    other = conn.execute("SELECT state FROM item_result WHERE run_id=? AND item_key='cmd:admin:06/dw10[31:16]'", (run,)).fetchone()[0]
    assert other in ("partial", "covered", "not_covered")                      # fields without an override are still aggregated


def test_independent_uncaptioned_table_is_not_merged_into_the_previous_one(tmp_path):
    blocks = [("title", "NVM Express® Base Specification", "Revision 2.4"), ("h", 1, "1 Intro"),
              ("table", "Figure 1: First", ["Bits", "Description"], [["31:16", "A field"], ["15:00", "Reserved"]], None),
              ("pagebreak",),
              ("table", "", ["Value", "Meaning"], [["00h", "Alpha"], ["01h", "Beta"]], None)]     # same column count, different header
    p = tmp_path / "t.pdf"
    write_pdf(p, blocks, header="x")
    s = parse_pdf(str(p), workers=1)
    assert len(s.tables) == 2
    assert [r[0] for r in s.tables[0].rows] == ["31:16", "15:00"]               # not extended with the other table's rows
    assert s.tables[1].header == ["Value", "Meaning"] and [r[0] for r in s.tables[1].rows] == ["00h", "01h"]
    assert any(n["kind"] == "uncaptioned_table" for n in s.notes)


def test_headings_at_the_top_of_a_page_are_not_running_headers():
    line = lambda text: {"text": text, "x0": 70, "y0": 10, "x1": 300, "y1": 22, "block": 1, "size": 12, "bold": True}   # noqa: E731
    pages = [{"page": i + 1, "height": 792, "width": 612, "lines": [line(t)], "tables": [], "raw_chars": 0}
             for i, t in enumerate(["5.1 Command Dword 10", "5.2 Command Dword 11", "5.3 Command Dword 12"])]
    assert _detect_headers_footers(pages) == {(1, 0), (2, 0), (3, 0)}           # digit-normalised repeats look like a running header...
    titles = {1: ["5.1 command dword 10"], 2: ["5.2 command dword 11"], 3: ["5.3 command dword 12"]}
    assert _detect_headers_footers(pages, titles) == set()                       # ...unless they are bookmarked headings


def test_html_report_lists_every_gap():
    items = [{"key": f"k{i}", "kind": "value", "command": "c", "label": f"item {i}", "observability": "submission", "state": "not_covered",
              "na_reason": None} for i in range(6000)]
    ds = {"header": {"tool_version": "x"}, "summary": {"overall": {"denominator": 6000}, "by_command": {}}, "items": items}
    html = reports.to_html(ds).decode()
    assert html.count("<td>not_covered</td>") == 6000 and "item 5999" in html


def test_job_runs_in_the_database_its_row_was_written_to(ws, tmp_path):
    other = connect(tmp_path / "other.db")
    jid = jobs.submit(other, "t", lambda c, p: {"db": [r[2] for r in c.execute("PRAGMA database_list")][0]})      # note: no db_file
    for _ in range(100):
        if jobs.get(other, jid)["state"] in ("done", "failed"):
            break
        time.sleep(0.05)
    j = jobs.get(other, jid)
    assert j["state"] == "done" and j["result_json"].endswith('other.db"}')
    assert connect().execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0     # the default workspace database was never touched


def test_job_state_is_not_shared_between_databases(ws, tmp_path):
    a, b = connect(tmp_path / "a.db"), connect(tmp_path / "b.db")
    release = threading.Event()

    def work(tag):
        def fn(c, prog):
            while not release.is_set():
                prog(tag, 1, 2)                      # raises InterruptedError once THIS job is cancelled
                time.sleep(0.01)
            return tag
        return fn

    ja, jb = jobs.submit(a, "t", work("A")), jobs.submit(b, "t", work("B"))
    assert ja == jb == 1                             # same id in two databases
    for _ in range(100):
        if jobs.get(a, 1)["message"] == "A" and jobs.get(b, 1)["message"] == "B":
            break
        time.sleep(0.02)
    assert (jobs.get(a, 1)["message"], jobs.get(b, 1)["message"]) == ("A", "B")   # progress is per database
    jobs.cancel(1, str(tmp_path / "a.db"))
    for _ in range(100):
        if jobs.get(a, 1)["state"] == "cancelled":
            break
        time.sleep(0.02)
    assert jobs.get(a, 1)["state"] == "cancelled" and jobs.get(b, 1)["state"] == "running"   # B is unaffected
    release.set()
    for _ in range(100):
        if jobs.get(b, 1)["state"] == "done":
            break
        time.sleep(0.02)
    assert jobs.get(b, 1)["state"] == "done"
