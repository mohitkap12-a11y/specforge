"""Docling model files and offline mode (Settings -> Offline models).

settings["offline"] = {"enabled": bool, "models_path": str}
  enabled      on (the default): Hugging Face libraries run in offline mode, Docling loads its models only from the models
               folder, and this process refuses connections to public internet addresses (core/netguard.py).
  models_path  where the models are downloaded to; empty means <workspace>/models/docling.

Typical use: deploy, turn Offline off, press Download models, turn Offline on again. The folder can also be carried to
another server in a backup (item "Docling models").
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..core import config, netguard
from . import docling_engine

MARKER = ".specforge-models.json"     # written once a download has finished and the models loaded from the folder
HF_OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
_set_by_us: set[str] = set()
_download_lock = threading.Lock()


class ModelsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def cfg(settings: dict) -> dict:
    c = settings.get("offline") or {}
    return {"enabled": bool(c.get("enabled", True)), "models_path": str(c.get("models_path") or "").strip()}


def default_folder() -> Path:
    return config.workspace_dir() / "models" / "docling"


def folder(settings: dict) -> Path:
    p = cfg(settings)["models_path"]
    return Path(p).expanduser() if p else default_folder()


def downloaded(path: Path) -> dict | None:
    """What the marker of a finished download says, or None when the folder has no complete download."""
    try:
        d = json.loads((path / MARKER).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return None


def effective_artifacts(settings: dict) -> str | None:
    """The folder Docling loads from: DOCLING_ARTIFACTS_PATH, else the models folder once downloaded, else None
    (Docling's default Hugging Face cache)."""
    if env := os.environ.get("DOCLING_ARTIFACTS_PATH"):
        return env
    f = folder(settings)
    return str(f) if downloaded(f) is not None else None


def _set_hf_offline(on: bool) -> None:
    for k, v in HF_OFFLINE_ENV.items():
        if on and k not in os.environ:
            os.environ[k] = v
            _set_by_us.add(k)
        elif not on and k in _set_by_us:
            os.environ.pop(k, None)
            _set_by_us.discard(k)
    if "huggingface_hub.constants" in sys.modules:      # read once at import: keep an already imported copy in step
        c = sys.modules["huggingface_hub.constants"]
        if hasattr(c, "HF_HUB_OFFLINE"):
            c.HF_HUB_OFFLINE = on or os.environ.get("HF_HUB_OFFLINE", "").upper() in ("1", "ON", "YES", "TRUE")


def apply(settings: dict) -> None:
    """Put the offline setting and the models folder into effect for this process."""
    on = cfg(settings)["enabled"]
    _set_hf_offline(on)
    netguard.enable() if on else netguard.disable()
    if not docling_engine.configure(effective_artifacts(settings)):
        docling_engine.reset()       # same folder, but a model load that failed while offline may work now (or the reverse)


def _folder_stats(path: Path) -> tuple[int, int]:
    files = size = 0
    if path.is_dir():
        for dirpath, _d, names in os.walk(path):
            for fn in names:
                try:
                    size += (Path(dirpath) / fn).stat().st_size
                    files += 1
                except OSError:
                    pass
    return files, size


def status(settings: dict) -> dict:
    c = cfg(settings)
    f = folder(settings)
    files, size = _folder_stats(f)
    version = None
    if available := docling_engine.available():
        try:
            from importlib.metadata import version as _v
            version = _v("docling")
        except Exception:  # noqa: BLE001
            pass
    return {"offline": c["enabled"], "models_path": c["models_path"], "folder": str(f), "default_folder": str(default_folder()),
            "downloaded": downloaded(f), "files": files, "bytes": size, "env_override": os.environ.get("DOCLING_ARTIFACTS_PATH") or None,
            "loading_from": docling_engine.artifacts_path(), "docling_available": available, "docling_version": version,
            "guard": netguard.status(), "downloading": _download_lock.locked()}


def validate_path(p: str) -> str:
    p = (p or "").strip()
    if p and not Path(p).expanduser().is_absolute():
        raise ModelsError("The models folder must be an absolute path (or empty for the default)", 422)
    return p


def download(settings: dict, prog: Callable[..., None], out: Path | None = None, explicit: bool = False) -> dict:
    """Download every model the default Docling pipeline uses into the models folder (or out), then load them from it.
    explicit: asked for on the command line (`specforge prefetch-models`), which is allowed while offline mode is on."""
    if cfg(settings)["enabled"] and not explicit:
        raise ModelsError("Offline mode is on: turn it off to download the models, then turn it on again", 409)
    if not docling_engine.available():
        raise ModelsError("Docling is not installed on this server (pip install 'docling>=2.19')")
    try:
        from docling.utils.model_downloader import download_models
    except ImportError:
        raise ModelsError("This Docling version cannot download models; upgrade it: pip install -U 'docling>=2.19'") from None
    if not _download_lock.acquire(blocking=False):
        raise ModelsError("A download is already running", 409)
    try:
        out = out or folder(settings)
        out.mkdir(parents=True, exist_ok=True)
        (out / MARKER).unlink(missing_ok=True)
        params = inspect.signature(download_models).parameters
        kw = {"output_dir": out, "progress": False}
        if "with_easyocr" in params:           # the OCR engine Docling picks when EasyOCR is installed
            kw["with_easyocr"] = importlib.util.find_spec("easyocr") is not None
        kw = {k: v for k, v in kw.items() if k in params}
        result: dict = {}

        def run():
            try:
                download_models(**kw)
            except BaseException as e:  # noqa: BLE001 - reported by the job
                result["error"] = e
        t = threading.Thread(target=run, name="sf-docling-download", daemon=True)
        t0 = time.monotonic()
        t.start()
        while t.is_alive():
            t.join(2.0)
            _files, size = _folder_stats(out)
            prog(f"Downloading Docling models to {out} ({size / 1e6:.0f} MB so far)")
        if err := result.get("error"):
            raise ModelsError(f"Download failed ({err.__class__.__name__}: {str(err)[:300]})")
        prog(f"Checking that the models load from {out}")
        docling_engine.configure(str(out)) or docling_engine.reset()
        for fmt in ("pdf", "docx"):
            try:
                docling_engine.ready(fmt)
            except docling_engine.DoclingUnavailable as e:
                raise ModelsError(f"The models were downloaded but do not load: {e}") from None
        files, size = _folder_stats(out)
        marker = {"downloaded": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "docling_version": status(settings)["docling_version"],
                  "files": files, "bytes": size, "seconds": round(time.monotonic() - t0)}
        (out / MARKER).write_text(json.dumps(marker, indent=1))
        return {"folder": str(out), **marker}
    finally:
        _download_lock.release()
