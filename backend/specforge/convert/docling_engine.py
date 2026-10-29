"""Docling engine for the spec -> Markdown converter (the default).

Docling reads the page layout with its layout model and rebuilds tables with TableFormer, so multi-row headers,
merged cells and tables split across pages come out aligned. The converter loads its models once per process and is
reused; conversions are serialised because a DocumentConverter is not safe to share between threads.

Offline machines: download the model weights once with `docling-tools models download` (or `specforge
prefetch-models`) and point `DOCLING_ARTIFACTS_PATH` at the folder if it is not the default cache.
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_converter = None


class DoclingError(Exception):
    """Docling could not read the file; the message is safe to show to the user."""


def available() -> bool:
    try:
        import docling.document_converter  # noqa: F401
    except ImportError:
        return False
    return True


def _make_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    if os.environ.get("DOCLING_ARTIFACTS_PATH"):
        opts.artifacts_path = os.environ["DOCLING_ARTIFACTS_PATH"]
    if os.environ.get("SPECFORGE_DOCLING_OCR", "").lower() in ("0", "false", "no", "off"):
        opts.do_ocr = False   # born-digital specs do not need OCR; turning it off is faster
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def converter():
    global _converter
    if _converter is None:
        _converter = _make_converter()
    return _converter


def _stats(doc) -> dict:
    labels = [str(getattr(t.label, "value", t.label)) for t in doc.texts]
    return {"headings": sum(lb in ("title", "section_header") for lb in labels),
            "tables": len(doc.tables),
            "list_items": labels.count("list_item"),
            "code_blocks": labels.count("code"),
            "pictures": len(getattr(doc, "pictures", ()))}


def _title(doc) -> str:
    for t in doc.texts:
        if str(getattr(t.label, "value", t.label)) in ("title", "section_header") and t.text.strip():
            return t.text.strip()
    return ""


def convert(path: str) -> tuple[str, dict]:
    """Markdown text and metadata (title, pages, counts) for a PDF or DOCX file."""
    with _lock:
        try:
            result = converter().convert(path, raises_on_error=False)
        except Exception as e:  # noqa: BLE001 - docling raises many types for unreadable input
            raise DoclingError(f"Docling could not read the file ({e.__class__.__name__}: {e}).") from e
        status = str(getattr(result.status, "value", result.status))
        if status not in ("success", "partial_success"):
            errs = "; ".join(str(getattr(e, "error_message", e)) for e in (result.errors or [])[:3])
            raise DoclingError("Docling could not convert the file" + (f": {errs}" if errs else "."))
        doc = result.document
        markdown = doc.export_to_markdown()
    meta = {"title": _title(doc), "pages": doc.num_pages(), "partial": status == "partial_success", **_stats(doc)}
    return markdown, meta
