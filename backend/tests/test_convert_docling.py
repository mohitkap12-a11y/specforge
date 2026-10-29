"""Docling engine of the spec -> Markdown converter.

Most tests swap Docling's DocumentConverter for a stub so they are fast and need no model weights; the last test
runs the real Docling on a generated PDF when SPECFORGE_TEST_DOCLING=1 (the models are downloaded on first use).
"""
import io
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from specforge.convert import ConversionError, convert_file, docling_engine

MD = "# Widget Spec\n\n## 1 Scope\n\n| Bits | Name |\n|------|------|\n| 7:0  | OPC  |\n"


def _text(label, text):
    return SimpleNamespace(label=SimpleNamespace(value=label), text=text)


class StubConverter:
    calls: list = []

    def __init__(self, status="success", markdown=MD, fail=None):
        self.status, self.markdown, self.fail = status, markdown, fail

    def convert(self, path, raises_on_error=True):
        StubConverter.calls.append(path)
        if self.fail:
            raise self.fail
        doc = SimpleNamespace(
            texts=[_text("title", "Widget Spec"), _text("section_header", "1 Scope"), _text("list_item", "a"), _text("text", "b")],
            tables=[object()], pictures=[], num_pages=lambda: 3, export_to_markdown=lambda: self.markdown)
        return SimpleNamespace(status=SimpleNamespace(value=self.status), document=doc,
                               errors=[SimpleNamespace(error_message="page 2 broken")])


@pytest.fixture
def stub(monkeypatch):
    def use(**kw):
        monkeypatch.setattr(docling_engine, "_converter", StubConverter(**kw))
        monkeypatch.setattr(docling_engine, "available", lambda: True)
        StubConverter.calls = []
    use()
    monkeypatch.delenv("SPECFORGE_CONVERT_ENGINE", raising=False)
    return use


@pytest.fixture
def pdf_file(tmp_path):
    p = tmp_path / "upload.tmp"   # uploads arrive under a temp name: the engine must still hand Docling a .pdf
    p.write_bytes(b"%PDF-1.4\n%stub\n")
    return p


def test_docling_is_the_default_engine(stub, pdf_file):
    r = convert_file(pdf_file, filename="Widget.pdf")
    assert r.engine == "docling"
    assert StubConverter.calls[0].endswith(".pdf")
    assert "| 7:0  | OPC  |" in r.markdown
    assert r.markdown.startswith('---\ntitle: "Widget Spec"\nsource: "Widget.pdf"\nformat: pdf\npages: 3\nengine: docling\n---\n')
    assert r.stats["pages"] == 3 and r.stats["tables"] == 1 and r.stats["headings"] == 2 and r.stats["list_items"] == 1
    assert r.title == "Widget Spec" and r.warnings == []


def test_env_and_argument_select_the_engine(stub, pdf_file, monkeypatch):
    monkeypatch.setenv("SPECFORGE_CONVERT_ENGINE", "builtin")
    with pytest.raises(ConversionError):   # the stub file is no real PDF: the builtin engine tries and fails
        convert_file(pdf_file)
    assert StubConverter.calls == []
    assert convert_file(pdf_file, engine="docling").engine == "docling"
    with pytest.raises(ConversionError, match="Unknown conversion engine"):
        convert_file(pdf_file, engine="magic")


def test_partial_and_failed_conversions(stub, pdf_file):
    stub(status="partial_success")
    assert any("partially" in w for w in convert_file(pdf_file).warnings)
    stub(status="failure")
    with pytest.raises(ConversionError, match="page 2 broken"):
        convert_file(pdf_file)
    stub(fail=RuntimeError("boom"))
    with pytest.raises(ConversionError, match="RuntimeError: boom"):
        convert_file(pdf_file)
    stub(markdown="  \n")
    with pytest.raises(ConversionError, match="No text"):
        convert_file(pdf_file)


def test_falls_back_to_builtin_when_docling_is_missing(monkeypatch, tmp_path):
    import docx

    monkeypatch.setattr(docling_engine, "available", lambda: False)
    d = docx.Document()
    d.add_heading("Overview", 1)
    d.add_paragraph("Body text.")
    path = tmp_path / "a.docx"
    d.save(path)
    r = convert_file(path, engine="docling", front_matter=False)
    assert r.engine == "builtin" and "# Overview" in r.markdown
    assert "Docling is not installed" in r.warnings[0]


def test_api_and_cli_pass_the_engine(stub, pdf_file, tmp_path, ws):
    from specforge.api.app import create_app
    from specforge.cli import main

    client = TestClient(create_app())
    r = client.post("/api/convert/markdown", files={"file": ("w.pdf", io.BytesIO(pdf_file.read_bytes()))})
    assert r.status_code == 200 and r.json()["engine"] == "docling" and "| 7:0" in r.json()["markdown"]
    assert client.post("/api/convert/markdown?engine=x", files={"file": ("w.pdf", io.BytesIO(b"%PDF"))}).status_code == 400

    src = tmp_path / "Widget.pdf"
    src.write_bytes(pdf_file.read_bytes())
    main(["to-md", str(src), "--out", str(tmp_path / "out"), "--engine", "docling"])
    assert "| 7:0  | OPC  |" in (tmp_path / "out" / "Widget.md").read_text()


@pytest.mark.skipif(os.environ.get("SPECFORGE_TEST_DOCLING") != "1", reason="set SPECFORGE_TEST_DOCLING=1 to run real Docling")
def test_real_docling_keeps_table_alignment(tmp_path, monkeypatch):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    monkeypatch.setattr(docling_engine, "_converter", None)
    ss = getSampleStyleSheet()
    rows = [["Bits", "Name", "Description"], ["31:16", "SQID", "Submission Queue Identifier"],
            ["15:08", "Reserved", ""], ["07:00", "OPC", "Opcode of the command"]]
    path = tmp_path / "fig.pdf"
    SimpleDocTemplate(str(path), pagesize=A4).build([
        Paragraph("1 Command Dword 0", ss["Heading1"]), Paragraph("The host shall set these fields as described below. " * 4, ss["Normal"]),
        Spacer(1, 20), Paragraph("Figure 1: Command Dword 0", ss["Italic"]),
        Table(rows, style=TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")]))])
    r = convert_file(path, engine="docling", front_matter=False)
    lines = [ln for ln in r.markdown.splitlines() if ln.startswith("|")]
    assert r.stats["tables"] == 1 and len(lines) == 5   # header, separator, three rows
    assert all(ln.count("|") == 4 for ln in lines)       # every row keeps its three columns
    assert "| 31:16  | SQID     | Submission Queue Identifier |" in r.markdown
    assert "## 1 Command Dword 0" in r.markdown
