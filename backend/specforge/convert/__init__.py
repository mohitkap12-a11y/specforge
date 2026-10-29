"""Convert a PDF, DOC or DOCX specification into a clean, structured Markdown file.

    from specforge.convert import convert_file
    result = convert_file("spec.pdf")
    result.markdown, result.stats, result.warnings

Two engines: "docling" (the default; layout model + TableFormer, best for tables) and "builtin" (PyMuPDF /
python-docx, no models, fast). The default comes from `SPECFORGE_CONVERT_ENGINE`; if Docling is not installed the
builtin engine is used with a warning.

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
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import md
from .md import Code, Heading, Item, Table

SUPPORTED = (".pdf", ".docx", ".doc")
ENGINES = ("docling", "builtin")


class ConversionError(Exception):
    """The file cannot be converted; the message is safe to show to the user."""


@dataclass
class ConvertResult:
    markdown: str
    title: str
    source_format: str
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    engine: str = "builtin"


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


def _usable_title(t: str | None) -> str:
    t = (t or "").strip()
    if t and t.lower() not in ("(anonymous)", "anonymous", "untitled") and not re.search(r"\.(docx?|pdf|indd|tmp)$", t, re.I):
        return t
    return ""


def _title_of(blocks, meta: dict, filename: str) -> str:
    if t := _usable_title(meta.get("title")):
        return t
    first = next((b for b in blocks if isinstance(b, Heading)), None)
    return first.text.strip() if first else Path(filename).stem


# Conversions run one at a time on this thread (Docling is CPU-heavy and its converter is not thread-safe); the API
# hands work here instead of blocking a request thread while another conversion holds the engine.
_serial = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sf-convert")


def serial_pool() -> ThreadPoolExecutor:
    return _serial


def submit(*args, **kw) -> Future:
    """convert_file(*args, **kw) on the conversion thread."""
    return _serial.submit(convert_file, *args, **kw)


def default_engine() -> str:
    e = os.environ.get("SPECFORGE_CONVERT_ENGINE", "docling").strip().lower()
    return e if e in ENGINES else "docling"


IMAGE_MARK = "<!-- image -->"


def _words(body: str) -> int:
    return len(re.findall(r"\w+", re.sub(r"[#*|`>\-]+", " ", body.replace(IMAGE_MARK, " "))))


def _meta_title(path: str, fmt: str) -> str:
    """The document's own Title property (PDF info / Word core properties), if it is a real title."""
    try:
        if fmt == "pdf":
            import pymupdf

            with pymupdf.open(path) as d:
                return _usable_title((d.metadata or {}).get("title"))
        import docx

        return _usable_title(docx.Document(path).core_properties.title)
    except Exception:  # noqa: BLE001 - a title is optional; Docling reports a broken file itself
        return ""


def _front(body: str, title: str, filename: str, fmt: str, pages: int | None, engine: str) -> str:
    head = ["---", f"title: {json.dumps(title, ensure_ascii=False)}", f"source: {json.dumps(filename, ensure_ascii=False)}", f"format: {fmt}"]
    if pages is not None:
        head.append(f"pages: {pages}")
    head.append(f"engine: {engine}")
    return "\n".join(head + ["---", "", body])


def convert_file(path: str | Path, filename: str | None = None, front_matter: bool = True, engine: str | None = None) -> ConvertResult:
    path = str(path)
    filename = filename or os.path.basename(path)
    engine = (engine or default_engine()).lower()
    if engine not in ENGINES:
        raise ConversionError(f"Unknown conversion engine '{engine}'; use one of: {', '.join(ENGINES)}.")
    fmt = sniff(path)
    warnings: list[str] = []
    if engine == "docling":
        from . import docling_engine

        if not docling_engine.available():
            warnings.append("Docling is not installed or cannot be loaded (pip install docling); the built-in converter was used, tables may be less accurate.")
        else:
            try:
                docling_engine.ready("pdf" if fmt == "pdf" else "docx")
            except docling_engine.DoclingUnavailable as e:
                warnings.append(f"{e}; the built-in converter was used, tables may be less accurate. "
                                "On an offline machine run `specforge prefetch-models` and set DOCLING_ARTIFACTS_PATH.")
            else:
                return _convert_docling(path, filename, fmt, front_matter)
    res = _convert_builtin(path, filename, fmt, front_matter)
    res.warnings[:0] = warnings
    return res


def _convert_docling(path: str, filename: str, fmt: str, front_matter: bool) -> ConvertResult:
    from . import docling_engine

    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="specforge-md-") as tmp:
        src = _doc_to_docx(path, tmp) if fmt == "doc" else path
        if Path(src).suffix.lower() not in (".pdf", ".docx"):
            # docling picks the format from the extension; uploads are stored under a temp name
            link = Path(tmp, "input." + ("pdf" if fmt == "pdf" else "docx"))
            shutil.copyfile(src, link)
            src = str(link)
        try:
            body, meta = docling_engine.convert(src)
        except docling_engine.DoclingError as e:
            raise ConversionError(str(e)) from e
        doc_title = _meta_title(src, fmt)
    body = body.strip() + "\n"
    if not body.strip():
        raise ConversionError("No text could be extracted from the file.")
    if meta.get("partial"):
        warnings.append("Docling converted the file only partially; some pages may be missing.")
    if meta.get("pictures"):
        warnings.append(f"{meta['pictures']} image(s) are not included in the Markdown; each is marked {IMAGE_MARK}.")
    title = doc_title or (meta.get("title") or "").strip() or Path(filename).stem
    stats = {"words": _words(body), "headings": meta["headings"], "tables": meta["tables"], "list_items": meta["list_items"],
             "code_blocks": meta["code_blocks"], "characters": len(body)}
    pages = meta.get("pages") if fmt == "pdf" else None
    if pages is not None:
        stats["pages"] = pages
    if fmt == "pdf" and not stats["headings"]:
        warnings.append("No headings were detected; the text is kept as plain paragraphs.")
    if front_matter:
        body = _front(body, title, filename, fmt, pages, "docling")
    return ConvertResult(body, title, fmt, stats, warnings, "docling")


def _convert_builtin(path: str, filename: str, fmt: str, front_matter: bool) -> ConvertResult:
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
    stats = {"words": _words(body), "headings": sum(isinstance(b, Heading) for b in blocks),
             "tables": sum(isinstance(b, Table) for b in blocks), "list_items": sum(isinstance(b, Item) for b in blocks),
             "code_blocks": sum(isinstance(b, Code) for b in blocks), "characters": len(body)}
    if fmt == "pdf":
        stats["pages"] = meta["pages"]
        if not stats["headings"]:
            warnings.append("No headings were detected (the PDF has no bookmarks and no larger or bold titles); the text is kept as plain paragraphs.")
    if body and not stats["words"]:
        warnings.append("The converted text is empty.")
    if front_matter:
        body = _front(body, title, filename, fmt, meta["pages"] if fmt == "pdf" else None, "builtin")
    return ConvertResult(body, title, fmt, stats, warnings, "builtin")


def output_name(filename: str) -> str:
    """'NVM Express Base 2.4.pdf' -> 'NVM Express Base 2.4.md' (safe for a download header)."""
    stem = re.sub(r"[^\w .()+-]+", "_", Path(filename or "document").stem).strip(" .") or "document"
    return stem + ".md"
