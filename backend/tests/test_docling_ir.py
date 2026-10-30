"""Docling -> IR (ingest/docling_ir.py) and the Specs tab reusing To Markdown's Docling conversions.

The DoclingDocuments here are built by hand in the shape Docling 2 exports (texts / tables / pictures / groups referenced
by '#/kind/n', table grids with spans, furniture for page headers), so the tests need no models.
"""
import io
import json

import pytest
from fastapi.testclient import TestClient

from specforge.ingest.audit import audit_spec
from specforge.ingest.docling_ir import parse_docling


class Doc:
    """A DoclingDocument dict under construction; items are appended to the body in reading order."""

    def __init__(self, pages=3):
        self.d = {"schema_name": "DoclingDocument", "body": {"self_ref": "#/body", "children": []},
                  "furniture": {"self_ref": "#/furniture", "children": []}, "groups": [], "texts": [], "tables": [],
                  "pictures": [], "pages": {str(i): {"page_no": i, "size": {"width": 612, "height": 792}} for i in range(1, pages + 1)}}

    def _text(self, label, text, page, parent="#/body", layer="body"):
        ref = f"#/texts/{len(self.d['texts'])}"
        self.d["texts"].append({"self_ref": ref, "parent": {"$ref": parent}, "children": [], "content_layer": layer, "label": label,
                                "prov": [{"page_no": page, "bbox": {}, "charspan": [0, len(text)]}], "orig": text, "text": text})
        return ref

    def text(self, text, page, label="text"):
        self.d["body"]["children"].append({"$ref": self._text(label, text, page)})
        return self

    def header(self, text, page):
        return self.text(text, page, "section_header")

    def furniture(self, text, page, label="page_header"):
        self.d["body"]["children"].append({"$ref": self._text(label, text, page, layer="furniture")})
        return self

    def table(self, page, rows, caption=None, spans=(), header_rows=1, label="table"):
        """rows: list of lists of text; spans: (row, col, row_span, col_span) of cells covering more than one position."""
        ref = f"#/tables/{len(self.d['tables'])}"
        caps = [{"$ref": self._text("caption", caption, page, parent=ref)}] if caption else []
        nr, nc = len(rows), max(len(r) for r in rows)
        covered, cells = set(), []
        span_of = {(r, c): (rs, cs) for r, c, rs, cs in spans}
        for r in range(nr):
            for c in range(nc):
                if (r, c) in covered:
                    continue
                rs, cs = span_of.get((r, c), (1, 1))
                cells.append({"text": rows[r][c] if c < len(rows[r]) else "", "row_span": rs, "col_span": cs,
                              "start_row_offset_idx": r, "end_row_offset_idx": r + rs, "start_col_offset_idx": c, "end_col_offset_idx": c + cs,
                              "column_header": r < header_rows, "row_header": False})
                covered |= {(r2, c2) for r2 in range(r, r + rs) for c2 in range(c, c + cs)}
        grid = [[None] * nc for _ in range(nr)]
        for cell in cells:
            for r in range(cell["start_row_offset_idx"], cell["end_row_offset_idx"]):
                for c in range(cell["start_col_offset_idx"], cell["end_col_offset_idx"]):
                    grid[r][c] = dict(cell)          # Docling's export repeats a spanning cell in each position
        self.d["tables"].append({"self_ref": ref, "parent": {"$ref": "#/body"}, "children": caps, "content_layer": "body", "label": label,
                                 "prov": [{"page_no": page, "bbox": {}, "charspan": [0, 0]}], "captions": caps,
                                 "data": {"num_rows": nr, "num_cols": nc, "table_cells": cells, "grid": grid}})
        self.d["body"]["children"].append({"$ref": ref})
        return self

    def picture(self, page, caption, inner=()):
        ref = f"#/pictures/{len(self.d['pictures'])}"
        caps = [{"$ref": self._text("caption", caption, page, parent=ref)}]
        kids = caps + [{"$ref": self._text("text", t, page, parent=ref)} for t in inner]
        self.d["pictures"].append({"self_ref": ref, "parent": {"$ref": "#/body"}, "children": kids, "content_layer": "body",
                                   "label": "picture", "prov": [{"page_no": page, "bbox": {}, "charspan": [0, 0]}], "captions": caps})
        self.d["body"]["children"].append({"$ref": ref})
        return self


TOC = [[1, "1 Introduction", 1], [2, "1.1 Scope", 1], [1, "2 Commands", 2], [2, "2.1 Identify", 2], [3, "2.1.1 Data Structure", 3]]


def spec_doc():
    return (Doc(3)
            .furniture("NVM Express Base Specification, Revision 2.4", 1).furniture("1", 1, "page_footer")
            .header("1 Introduction", 1).text("The controller shall process commands in order.", 1)
            .header("Scope", 1).text("This section is informative.", 1)                    # deep heading printed without its number
            .header("2 Commands", 2).text("See Figure 2 and Figure 3 for the layouts.", 2)
            .header("2.1 Identify", 2)
            .table(2, [["Bytes", "Description", ""],
                       ["0", "Command Dword 0: the host shall set this field.", ""],
                       ["0", "Bits", "Description"],
                       ["0", "7", "Fused (F): reserved."],
                       ["0", "6:0", "Opcode (OPC)."],
                       ["3:1", "Namespace Identifier", ""]],
                   caption="Figure 2: Command Dword 0", spans=[(0, 1, 1, 2), (1, 0, 4, 1), (1, 1, 1, 2), (5, 1, 1, 2)])
            .furniture("NVM Express Base Specification, Revision 2.4", 3)
            .table(3, [["Bytes", "Description", ""], ["7:4", "Reserved", ""], ["11:8", "Metadata Pointer", ""]],
                   caption="Figure 2: Command Dword 0", spans=[(0, 1, 1, 2), (1, 1, 1, 2), (2, 1, 1, 2)])   # the same figure, next page
            .header("Data Structure", 3)
            .picture(3, "Figure 3: Queue layout", inner=["SQ", "CQ"])
            .table(3, [["Value", "Definition"], ["0h", "Namespace"], ["1h", "Controller"]], caption="Figure 4: CNS Values"))


def test_docling_document_becomes_the_same_ir_as_the_pdf_parser():
    spec = parse_docling(spec_doc().d, "nvme", "2.4", toc=TOC)
    assert [(s.number, s.title, s.parent) for s in spec.sections] == [
        ("front", "Front matter", None), ("1", "Introduction", None), ("1.1", "Scope", "1"), ("2", "Commands", None),
        ("2.1", "Identify", "2"), ("2.1.1", "Data Structure", "2.1")]
    assert spec.sections[1].paragraphs == ["The controller shall process commands in order."]
    assert [(s.page_from, s.page_to) for s in spec.sections[1:]] == [(1, 2), (1, 2), (2, 3), (2, 3), (3, 3)]

    by = {t.label: t for t in spec.tables}
    assert set(by) == {"2", "2.n1", "3", "4"}
    fig2 = by["2"]
    assert fig2.header == ["Bytes", "Description", ""] and fig2.section == "2.1"
    assert fig2.rows == [["0", "Command Dword 0: the host shall set this field.", ""], ["3:1", "Namespace Identifier", ""],
                         ["7:4", "Reserved", ""], ["11:8", "Metadata Pointer", ""]]     # stitched; the repeated header dropped
    assert (fig2.page_from, fig2.page_to, fig2.continued_pages) == (2, 3, 1)
    nested = by["2.n1"]                                    # the Bits table printed inside the Bytes 0 row
    assert nested.parent_table == "2" and nested.parent_cell == (0, 1)
    assert nested.header == ["Bits", "Description"] and nested.rows == [["7", "Fused (F): reserved."], ["6:0", "Opcode (OPC)."]]
    assert by["3"].rows == [] and by["3"].extractor == "none"          # a diagram keeps its label so references resolve
    assert by["4"].header == ["Value", "Definition"] and by["4"].section == "2.1.1"

    assert {(e.category, e.text) for e in spec.excluded} == {("header/footer", "NVM Express Base Specification, Revision 2.4"), ("header/footer", "1")}
    assert {r.text for r in spec.requirements} >= {"The controller shall process commands in order.", "Command Dword 0: the host shall set this field."}
    issues, summary = audit_spec(spec)
    assert not [i for i in issues if i["kind"] in ("figure_reference_missing", "heading_not_found")]
    assert spec.stats["engine"] == "docling" and spec.stats["outline"] == "bookmarks"
    assert spec.accounted_char_count == spec.raw_char_count     # no PDF text layer given: everything Docling saw is placed


def test_without_bookmarks_numbered_headings_make_the_tree_and_unmatched_bookmarks_are_errors():
    spec = parse_docling(spec_doc().d, "nvme", "2.4")
    assert [s.number for s in spec.sections] == ["front", "1", "2", "2.1"]
    assert spec.stats["outline"] == "headings"
    assert "Scope" in spec.sections[1].paragraphs              # an unnumbered heading is kept as text
    spec = parse_docling(spec_doc().d, "nvme", "2.4", toc=TOC + [[2, "2.2 Missing", 3]])
    assert any(n["kind"] == "heading_not_found" and n["node_key"] == "sec:2.2" for n in spec.notes)


def test_uncaptioned_table_at_the_top_of_the_next_page_continues_the_previous_one():
    d = (Doc(2).header("1 Introduction", 1)
         .table(1, [["Bits", "Description"], ["31:16", "SQID"]], caption="Figure 1: Dword")
         .table(2, [["15:0", "CID"]], header_rows=0)
         .text("Afterwards.", 2)
         .table(2, [["A", "B"], ["x", "y"]], header_rows=1))
    spec = parse_docling(d.d, "nvme", "1.0", toc=[[1, "1 Introduction", 1]])
    assert [t.label for t in spec.tables] == ["1", "p2t1"]
    assert spec.tables[0].rows == [["31:16", "SQID"], ["15:0", "CID"]]
    assert any(n["kind"] == "uncaptioned_table" for n in spec.notes)


def test_text_accounting_against_the_pdf_text_layer():
    spec = parse_docling(spec_doc().d, "nvme", "2.4", toc=TOC, raw_chars=10_000)
    issues, _ = audit_spec(spec)
    assert any(i["kind"] == "text_accounting" for i in issues)       # Docling dropped text the PDF has: reported


# ----------------------------------------------------------------------------- the Specs tab reuses conversions

@pytest.fixture
def real_pdf(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for i, (title, body) in enumerate([("1 Introduction", "The controller shall process commands in order."),
                                       ("2 Commands", "See Figure 2 and Figure 3 for the layouts.")]):
        page = doc.new_page()
        page.insert_text((72, 72), "NVM Express Base Specification, Revision 2.4")
        page.insert_text((72, 120), title)
        page.insert_text((72, 150), body)
    doc.set_toc([[1, "1 Introduction", 1], [1, "2 Commands", 2]])
    p = tmp_path / "NVMe Base 2.4.pdf"
    doc.save(p)
    return p


@pytest.fixture
def docling_stub(monkeypatch):
    """Docling replaced by a converter that returns a fixed DoclingDocument (and records each conversion)."""
    from types import SimpleNamespace

    from specforge.convert import docling_engine

    d = (Doc(2).furniture("NVM Express Base Specification, Revision 2.4", 1)
         .header("1 Introduction", 1).text("The controller shall process commands in order.", 1)
         .furniture("NVM Express Base Specification, Revision 2.4", 2)
         .header("2 Commands", 2).text("See Figure 2 and Figure 3 for the layouts.", 2)
         .table(2, [["Bits", "Description"], ["31:16", "SQID"]], caption="Figure 2: Dword").picture(2, "Figure 3: Layout")).d
    calls = []

    class Conv:
        def initialize_pipeline(self, fmt):
            pass

        def convert(self, path, raises_on_error=True):
            calls.append(path)
            doc = SimpleNamespace(texts=[], tables=[], pictures=[], num_pages=lambda: 2,
                                  export_to_markdown=lambda: "# 1 Introduction\n\ntext\n", export_to_dict=lambda: d)
            return SimpleNamespace(status=SimpleNamespace(value="success"), document=doc, errors=[])

    monkeypatch.setattr(docling_engine, "_converter", Conv())
    monkeypatch.setattr(docling_engine, "available", lambda: True)
    monkeypatch.setattr(docling_engine, "_ready", {})
    monkeypatch.setattr(docling_engine, "_broken", {})
    monkeypatch.delenv("SPECFORGE_CONVERT_ENGINE", raising=False)
    return calls


def _wait(client, job):
    import time

    for _ in range(300):
        j = client.get(f"/api/jobs/{job}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _runs(client):
    return {d["filename"]: d for d in client.get("/api/specs").json()}


def test_a_converted_spec_is_added_to_specs_and_parsed_from_its_docling_conversion(docling_stub, real_pdf, ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        conv = client.post("/api/convert/markdown/jobs", files={"file": (real_pdf.name, io.BytesIO(real_pdf.read_bytes()))}).json()["job"]
        assert _wait(client, conv)["state"] == "done"
        folder = next((ws / "conversions").iterdir())
        assert json.loads((folder / "docling.json").read_text())["schema_name"] == "DoclingDocument"

        added = client.post(f"/api/specs/from-conversion/{conv}").json()
        assert added["document"]["filename"] == real_pdf.name
        run = _wait(client, added["job"])
        assert run["state"] == "done", run
        assert len(docling_stub) == 1                         # parsed from the saved conversion: Docling did not run again
        doc = _runs(client)[real_pdf.name]
        assert doc["docling"] is True
        r = doc["runs"][0]
        assert r["stats"]["engine"] == "docling" and r["status"] == "done" and r["active"]
        opt = next(o for o in client.get("/api/compare/options").json() if o["doc_id"] == doc["id"])
        assert opt["engine"] == "docling"

        # the conversion is deleted from the library later: the spec keeps its own copy and still parses with Docling
        assert client.delete(f"/api/convert/markdown/jobs/{conv}").json() == {"ok": True}
        again = client.post(f"/api/specs/{doc['id']}/parse?engine=docling").json()["job"]
        assert _wait(client, again)["state"] == "done" and len(docling_stub) == 1
        assert client.post(f"/api/specs/{doc['id']}/parse?engine=builtin").status_code == 200
        assert client.post(f"/api/specs/{doc['id']}/parse?engine=nope").status_code == 400


def test_uploading_on_specs_reuses_a_library_conversion_and_docling_converts_into_the_library(docling_stub, real_pdf, ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        doc = client.post("/api/specs/upload", files={"files": (real_pdf.name, io.BytesIO(real_pdf.read_bytes()))}).json()[0]
        assert _runs(client)[real_pdf.name]["docling"] is False
        auto = client.post(f"/api/specs/{doc['id']}/parse").json()["job"]     # nothing converted yet: the built-in parser
        assert _wait(client, auto)["state"] == "done"
        assert _runs(client)[real_pdf.name]["runs"][0]["stats"]["engine"] == "builtin" and docling_stub == []

        job = client.post(f"/api/specs/{doc['id']}/parse?engine=docling").json()["job"]   # converts first, into the library
        assert _wait(client, job)["state"] == "done"
        assert len(docling_stub) == 1 and _runs(client)[real_pdf.name]["runs"][0]["stats"]["engine"] == "docling"
        library = client.get("/api/convert/markdown/jobs").json()
        assert [(c["source"], c["state"], c["result"]["engine"], c["has_source"]) for c in library] == [(real_pdf.name, "done", "docling", True)]
        # the same file dropped on To Markdown now is not converted again
        assert client.post("/api/convert/markdown/jobs", files={"file": (real_pdf.name, io.BytesIO(real_pdf.read_bytes()))}).json()["reused"] is True
        assert len(docling_stub) == 1
