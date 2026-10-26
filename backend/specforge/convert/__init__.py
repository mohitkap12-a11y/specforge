"""Convert a PDF, DOC or DOCX specification into a clean, structured Markdown file.

    from specforge.convert import convert_file
    result = convert_file("spec.pdf")
    result.markdown, result.stats, result.warnings

Nothing is stored in the workspace; this is a pure file -> text utility used by the API (`POST /api/convert/markdown`)
and the CLI (`specforge to-md`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import md
from .md import Code, Heading, Item, Table

SUPPORTED = (".pdf", ".docx", ".doc")


class ConversionError(Exception):
    """The file cannot be converted; the message is safe to show to the user."""


@dataclass
class ConvertResult:
    markdown: str
    title: str
    source_format: str
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def sniff(path: str | Path) -> str:
    """'pdf' | 'docx' | 'doc' from the file's first bytes, not its extension (a renamed file still converts)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError as e:
        raise ConversionError(f"Cannot read {path}: {e.strerror or e}") from e
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        return "docx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc"
    raise ConversionError("Unsupported file: expected a PDF, DOC or DOCX document.")


def _doc_to_docx(path: str, workdir: str) -> str:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ConversionError("Legacy .doc files need LibreOffice (soffice) installed on the server; save the file as .docx and upload that instead.")
    profile = Path(workdir, "profile").resolve().as_uri()
    try:
        subprocess.run([exe, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "docx", "--outdir", workdir, path],
                       check=True, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        raise ConversionError("Converting the .doc file timed out.") from e
    except subprocess.CalledProcessError as e:
        raise ConversionError("LibreOffice could not read the .doc file.") from e
    out = Path(workdir, Path(path).stem + ".docx")
    if not out.exists():
        raise ConversionError("LibreOffice could not convert the .doc file.")
    return str(out)


def _title_of(blocks, meta: dict, filename: str) -> str:
    t = (meta.get("title") or "").strip()
    if t and t.lower() not in ("(anonymous)", "anonymous", "untitled") and not re.search(r"\.(docx?|pdf|indd|tmp)$", t, re.I):
        return t
    first = next((b for b in blocks if isinstance(b, Heading)), None)
    return first.text.strip() if first else Path(filename).stem


def convert_file(path: str | Path, filename: str | None = None, front_matter: bool = True) -> ConvertResult:
    path = str(path)
    filename = filename or os.path.basename(path)
    fmt = sniff(path)
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="specforge-md-") as tmp:
        try:
            if fmt == "pdf":
                from . import pdf

                blocks, meta = pdf.convert(path)
            else:
                from . import word

                blocks, meta = word.convert(_doc_to_docx(path, tmp) if fmt == "doc" else path)
        except (ImportError, ModuleNotFoundError) as e:   # pragma: no cover - dependency missing at install
            raise ConversionError(f"A required library is missing: {e}") from e
        except ConversionError:
            raise
        except Exception as e:  # noqa: BLE001 - parser errors carry a user-facing message; anything else is a corrupt file
            if e.__class__.__name__ in ("PdfError", "DocxError"):
                raise ConversionError(str(e)) from e
            raise ConversionError(f"The file could not be read ({e.__class__.__name__}: {e}).") from e

    body = md.render(blocks)
    notes = meta.get("footnotes") or []
    if notes:
        body += "\n" + "\n".join(f"[^{i}]: {t}" for i, t in notes) + "\n"
    if not body.strip():
        raise ConversionError("No text could be extracted from the file.")
    if meta.get("images_skipped"):
        warnings.append(f"{meta['images_skipped']} image(s) without alt text are not included in the Markdown.")
    title = _title_of(blocks, meta, filename)
    words = len(re.findall(r"\w+", re.sub(r"[#*|`>\-]+", " ", body)))
    stats = {"words": words, "headings": sum(isinstance(b, Heading) for b in blocks),
             "tables": sum(isinstance(b, Table) for b in blocks), "list_items": sum(isinstance(b, Item) for b in blocks),
             "code_blocks": sum(isinstance(b, Code) for b in blocks), "characters": len(body)}
    if fmt == "pdf":
        stats["pages"] = meta["pages"]
        if not stats["headings"]:
            warnings.append("No headings were detected (the PDF has no bookmarks and no larger or bold titles); the text is kept as plain paragraphs.")
    if body and not stats["words"]:
        warnings.append("The converted text is empty.")
    if front_matter:
        head = ["---", f"title: {json.dumps(title, ensure_ascii=False)}", f"source: {json.dumps(filename, ensure_ascii=False)}", f"format: {fmt}"]
        if fmt == "pdf":
            head.append(f"pages: {meta['pages']}")
        body = "\n".join(head + ["---", "", body])
    return ConvertResult(body, title, fmt, stats, warnings)


def output_name(filename: str) -> str:
    """'NVM Express Base 2.4.pdf' -> 'NVM Express Base 2.4.md' (safe for a download header)."""
    stem = re.sub(r"[^\w .()+-]+", "_", Path(filename or "document").stem).strip(" .") or "document"
    return stem + ".md"
