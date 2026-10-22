"""Full text of every prompt sent to a model and every reply, for reading in the server console or a log file.

The activity log keeps shortened copies (logging.preview_chars); this keeps the whole exchange.  Each record goes to the
`specforge.llm` logger (printed to stderr, i.e. the terminal running `specforge serve`, unless the application configures
that logger itself) and is appended to <workspace>/logs/llm-prompts.log, rotated at 20 MB.  Switched by logging.print_prompts.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .. import eventlog
from ..core.config import workspace_dir

log = logging.getLogger("specforge.llm")
_lock = threading.Lock()
_file: tuple[Path, RotatingFileHandler] | None = None
_console = False
RULE = "=" * 100


class _Stderr(logging.StreamHandler):
    """Writes to whatever sys.stderr is at the time of the record (it can be redirected after the handler is made)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def log_path() -> Path:
    return workspace_dir() / "logs" / "llm-prompts.log"


def _setup() -> None:
    """Console handler once; the file handler follows the workspace (SPECFORGE_HOME can change between tests / resets)."""
    global _file, _console
    with _lock:
        if not _console:
            _console = True
            log.setLevel(logging.INFO)
            if not log.handlers:            # the application has not configured this logger: print to the terminal
                console = _Stderr()
                console.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                log.addHandler(console)
                log.propagate = False
        path = log_path()
        if _file is None or _file[0] != path:
            if _file is not None:
                log.removeHandler(_file[1])
                _file[1].close()
            path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            log.addHandler(h)
            _file = (path, h)


def _emit(text: str) -> None:
    if not eventlog.print_prompts():
        return
    try:
        _setup()
        log.info(text)
    except Exception:  # noqa: BLE001  logging must never break the model call it describes
        pass


def prompt(task: str, provider: str, model: str, attempt: int, messages: list[dict], schema: dict | None, cached: bool) -> None:
    head = f"LLM PROMPT  task={task}  provider={provider}  model={model or '(unset)'}  attempt={attempt}" + \
        ("  [cache hit: answered from the response cache, not sent]" if cached else "")
    parts = [f"\n{RULE}\n{head}\n{RULE}"]
    for m in messages:
        parts.append(f"----- {str(m.get('role', '')).upper()} -----\n{m.get('content', '')}")
    if schema is not None:
        parts.append(f"----- OUTPUT JSON SCHEMA -----\n{json.dumps(schema)}")
    _emit("\n".join(parts))


def reply(task: str, provider: str, model: str, attempt: int, text: str, seconds: float, cached: bool) -> None:
    _emit(f"\n{RULE}\nLLM REPLY  task={task}  provider={provider}  model={model or '(unset)'}  attempt={attempt}  "
          f"{seconds}s{'  (cached)' if cached else ''}\n{RULE}\n{text}")
