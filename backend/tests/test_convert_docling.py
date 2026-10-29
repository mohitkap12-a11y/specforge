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

    def __init__(self, status="success", markdown=MD, fail=None, fail_init=None, pictures=0):
        self.status, self.markdown, self.fail, self.fail_init, self.pictures = status, markdown, fail, fail_init, pictures
        self.inits = 0

    def initialize_pipeline(self, fmt):
        self.inits += 1
        if self.fail_init:
            raise self.fail_init

    def convert(self, path, raises_on_error=True):
        StubConverter.calls.append(path)
        if self.fail:
            raise self.fail
        doc = SimpleNamespace(
            texts=[_text("title", "Widget Spec"), _text("section_header", "1 Scope"), _text("list_item", "a"), _text("text", "b")],
            tables=[object()], pictures=[object()] * self.pictures, num_pages=lambda: 3, export_to_markdown=lambda: self.markdown)
        return SimpleNamespace(status=SimpleNamespace(value=self.status), document=doc,
                               errors=[SimpleNamespace(error_message="page 2 broken")])


@pytest.fixture
def stub(monkeypatch):
    def use(**kw):
        conv = StubConverter(**kw)
        monkeypatch.setattr(docling_engine, "_converter", conv)
        monkeypatch.setattr(docling_engine, "available", lambda: True)
        monkeypatch.setattr(docling_engine, "_ready", {})
        monkeypatch.setattr(docling_engine, "_broken", {})
        StubConverter.calls = []
        return conv
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


def test_models_that_cannot_load_fall_back_to_builtin(stub, tmp_path, monkeypatch):
    """Offline machine without prefetched models: convert with the builtin engine instead of failing every file."""
    import docx

    d = docx.Document()
    d.add_heading("Overview", 1)
    path = tmp_path / "a.docx"
    d.save(path)
    conv = stub(fail_init=OSError("cannot reach huggingface.co"))
    r = convert_file(path, front_matter=False)
    assert r.engine == "builtin" and "# Overview" in r.markdown
    assert "huggingface.co" in r.warnings[0] and "prefetch-models" in r.warnings[0]
    convert_file(path, front_matter=False)
    assert conv.inits == 1 and StubConverter.calls == []       # the failure is remembered, not retried for every file
    monkeypatch.setattr(docling_engine, "RETRY_AFTER", 0)
    conv.fail_init = None
    assert convert_file(path).engine == "docling" and conv.inits == 2
    convert_file(path)
    assert conv.inits == 2                                     # a loaded pipeline is not loaded again


def test_broken_native_libraries_count_as_unavailable(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith("docling"):
            raise OSError("libtorch_cpu.so: cannot open shared object file")
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake)
    assert docling_engine.available() is False


def test_metadata_title_images_and_word_count(stub, tmp_path):
    import pymupdf

    path = tmp_path / "spec.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "1 Introduction")
    d.set_metadata({"title": "NVM Express Base Specification 2.1"})
    d.save(path)
    stub(pictures=2, markdown="## 1 Introduction\n\n<!-- image -->\n\nOne two three.\n\n<!-- image -->\n")
    r = convert_file(path)
    assert r.title == "NVM Express Base Specification 2.1"
    assert r.stats["words"] == 5                              # "1 Introduction One two three": the image markers are not words
    assert any("2 image(s)" in w and "<!-- image -->" in w for w in r.warnings)


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


def _wait(client, job):
    import time

    for _ in range(200):
        j = client.get(f"/api/jobs/{job}").json()
        if j["state"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_background_conversion_job(stub, pdf_file, ws):
    from specforge.api.app import create_app

    with TestClient(create_app()) as client:
        r = client.post("/api/convert/markdown/jobs?engine=docling", files={"file": ("Widget.pdf", io.BytesIO(pdf_file.read_bytes()))})
        job = r.json()["job"]
        assert _wait(client, job)["state"] == "done"
        res = client.get(f"/api/convert/markdown/jobs/{job}").json()
        assert res["filename"] == "Widget.md" and res["engine"] == "docling" and "| 7:0  | OPC  |" in res["markdown"] and "dir" not in res
        dl = client.get(f"/api/convert/markdown/jobs/{job}?download=true")
        assert dl.headers["content-disposition"].endswith("Widget.md") and "| 7:0" in dl.text
        folder = ws / "conversions"
        assert [p.name for d in folder.iterdir() for p in d.iterdir()] == ["out.md"]   # the upload is deleted after converting

        stub(status="failure")
        bad = client.post("/api/convert/markdown/jobs", files={"file": ("w.pdf", io.BytesIO(pdf_file.read_bytes()))}).json()["job"]
        j = _wait(client, bad)
        assert j["state"] == "failed" and "page 2 broken" in j["error"]
        assert client.get(f"/api/convert/markdown/jobs/{bad}").status_code == 409
        assert client.get("/api/convert/markdown/jobs/99999").status_code == 404

        for d in folder.iterdir():                                     # expired results are pruned on the next upload
            os.utime(d, (0, 0))
        client.post("/api/convert/markdown/jobs", files={"file": ("w.pdf", io.BytesIO(pdf_file.read_bytes()))})
        assert client.get(f"/api/convert/markdown/jobs/{job}").status_code == 410


def test_conversion_results_are_private(stub, pdf_file, ws, monkeypatch):
    from specforge import jobs
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    with TestClient(create_app()) as client:
        job = client.post("/api/convert/markdown/jobs", files={"file": ("w.pdf", io.BytesIO(pdf_file.read_bytes()))}).json()["job"]
        _wait(client, job)
        c = get_conn()
        c.execute("UPDATE job SET payload_json=? WHERE id=?", ('{"user": "someone-else"}', job))
        c.commit()
        assert jobs.get(c, job)["state"] == "done"
        assert client.get(f"/api/convert/markdown/jobs/{job}").status_code == 200    # the local user is an admin
        import specforge.api.app as appmod

        monkeypatch.setattr(appmod, "LOCAL_USER", {**appmod.LOCAL_USER, "role": "user"})
    with TestClient(create_app()) as client:
        assert client.get(f"/api/convert/markdown/jobs/{job}").status_code == 404


@pytest.mark.skipif(os.environ.get("SPECFORGE_TEST_DOCLING") != "1", reason="set SPECFORGE_TEST_DOCLING=1 to run real Docling")
def test_real_docling_keeps_table_alignment(tmp_path, monkeypatch):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    monkeypatch.setattr(docling_engine, "_converter", None)
    monkeypatch.setattr(docling_engine, "_ready", {})
    monkeypatch.setattr(docling_engine, "_broken", {})
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


def _post(client, pdf_file, name):
    return client.post("/api/convert/markdown/jobs", files={"file": (name, io.BytesIO(pdf_file.read_bytes()))}).json()["job"]


def _listed(client):
    return [(c["source"], c["state"]) for c in client.get("/api/convert/markdown/jobs").json()]


def test_conversion_list_survives_and_removes(stub, pdf_file, ws, monkeypatch):
    """The To Markdown page lists the user's conversions from the server, so leaving the page does not lose them."""
    from specforge import jobs
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    with TestClient(create_app()) as client:
        assert client.get("/api/convert/markdown/jobs").json() == []
        first = _post(client, pdf_file, "A.pdf")
        _wait(client, first)
        stub(status="failure")
        bad = _post(client, pdf_file, "B.pdf")
        _wait(client, bad)
        listed = client.get("/api/convert/markdown/jobs").json()
        assert [(c["job"], c["source"], c["state"]) for c in listed] == [(bad, "B.pdf", "failed"), (first, "A.pdf", "done")]
        assert listed[1]["result"]["filename"] == "A.md" and "markdown" not in listed[1]["result"] and "dir" not in listed[1]["result"]
        assert listed[0]["result"] is None and "page 2 broken" in listed[0]["error"] and listed[0]["cancelling"] is False

        import specforge.api.app as appmod
        with monkeypatch.context() as m:                          # another user sees none of them
            m.setattr(appmod, "LOCAL_USER", {**appmod.LOCAL_USER, "username": "other"})
            assert client.get("/api/convert/markdown/jobs").json() == []

        assert client.delete(f"/api/convert/markdown/jobs/{first}").json() == {"ok": True}
        assert _listed(client) == [("B.pdf", "failed")]
        assert client.get(f"/api/convert/markdown/jobs/{first}").status_code == 404
        assert not any((ws / "conversions").iterdir())            # its file is gone
        assert client.delete(f"/api/convert/markdown/jobs/{first}").status_code == 404
        j = jobs.get(get_conn(), first)                           # removed means the file name is gone everywhere (jobs panel too)
        assert "A.pdf" not in (j["payload_json"] or "") and j["result_json"] is None


class Gate(StubConverter):
    """Holds the conversion thread until the test opens the gate (a long Docling run)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        import threading

        self.gate, self.started = threading.Event(), threading.Event()

    def convert(self, path, raises_on_error=True):
        self.started.set()
        assert self.gate.wait(10)
        return super().convert(path, raises_on_error)


def test_cancel_and_remove_while_another_file_converts(stub, pdf_file, ws, monkeypatch):
    from specforge.api.app import create_app

    conv = Gate()
    monkeypatch.setattr(docling_engine, "_converter", conv)
    with TestClient(create_app()) as client:
        long_job = _post(client, pdf_file, "Long.pdf")
        assert conv.started.wait(10)
        queued = _post(client, pdf_file, "Queued.pdf")
        removed = _post(client, pdf_file, "Removed.pdf")
        assert _listed(client) == [("Removed.pdf", "queued"), ("Queued.pdf", "queued"), ("Long.pdf", "running")]

        client.post(f"/api/jobs/{queued}/cancel")                # a queued job shows cancelled at once, not after the long one
        assert ("Queued.pdf", "cancelled") in _listed(client)
        assert client.delete(f"/api/convert/markdown/jobs/{removed}").json() == {"ok": True}   # remove works on a queued job too
        assert ("Removed.pdf", "queued") not in _listed(client) and len(list((ws / "conversions").iterdir())) == 2

        client.post(f"/api/jobs/{long_job}/cancel")              # Docling cannot be interrupted: it is "cancelling" until it returns
        entry = next(c for c in client.get("/api/convert/markdown/jobs").json() if c["job"] == long_job)
        assert entry["state"] == "running" and entry["cancelling"] is True
        assert client.delete(f"/api/convert/markdown/jobs/{long_job}").json() == {"ok": True}
        conv.gate.set()
        assert _wait(client, long_job)["state"] == "cancelled"
        assert client.delete(f"/api/convert/markdown/jobs/{queued}").json() == {"ok": True}
        assert not any((ws / "conversions").iterdir())             # nothing left behind by any of them
        assert _listed(client) == []


def test_old_active_jobs_stay_listed_and_their_uploads_are_not_pruned(stub, pdf_file, ws, monkeypatch):
    """A queue of long specs can take more than a day: age counts from when a conversion ended, not when it was queued."""
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    conv = Gate()
    monkeypatch.setattr(docling_engine, "_converter", conv)
    with TestClient(create_app()) as client:
        running = _post(client, pdf_file, "Running.pdf")
        assert conv.started.wait(10)
        waiting = _post(client, pdf_file, "Waiting.pdf")
        c = get_conn()
        c.execute("UPDATE job SET created=datetime('now','-3 days') WHERE id IN (?,?)", (running, waiting))
        c.commit()
        for d in (ws / "conversions").iterdir():
            os.utime(d, (0, 0))
        _post(client, pdf_file, "Trigger.pdf")                    # a new upload runs the prune
        assert len(list((ws / "conversions").iterdir())) == 3       # the old queued and running uploads were kept
        assert {s for s, _ in _listed(client)} == {"Running.pdf", "Waiting.pdf", "Trigger.pdf"}
        conv.gate.set()
        assert _wait(client, waiting)["state"] == "done"
        c.execute("UPDATE job SET created=datetime('now','-3 days')")   # finished just now: still listed
        c.commit()
        assert ("Waiting.pdf", "done") in _listed(client)
        assert "| 7:0" in client.get(f"/api/convert/markdown/jobs/{waiting}").json()["markdown"]


def test_remove_never_deletes_outside_the_conversions_folder(stub, ws):
    from specforge.api.app import create_app
    from specforge.core.db import get_conn

    with TestClient(create_app()) as client:
        (ws / "conversions").mkdir(exist_ok=True)
        c = get_conn()
        jid = c.execute("INSERT INTO job(type,state,payload_json) VALUES('convert-markdown','failed',?)",
                        ('{"user": "local", "filename": "x.pdf", "dir": ".."}',)).lastrowid
        c.commit()
        assert client.delete(f"/api/convert/markdown/jobs/{jid}").json() == {"ok": True}
        assert ws.is_dir() and (ws / "conversions").is_dir() and (ws / "specs").is_dir()
