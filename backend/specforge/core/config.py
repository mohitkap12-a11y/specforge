"""Workspace location and persistent settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PARSER_VERSION = "4"  # bump when parsing output changes; flags existing runs as "reparse available"
CATALOG_VERSION = "2"

DEFAULT_SETTINGS: dict[str, Any] = {
    "ollama": {"base_url": "http://127.0.0.1:11434", "num_ctx": 16384, "timeout": 600},
    "opencode": {"binary": "opencode", "timeout": 600, "mode": "run"},
    "tasks": {
        "classify": {"provider": "ollama", "model": "", "temperature": 0.0},
        "generate": {"provider": "ollama", "model": "", "temperature": 0.1},
        "embed": {"provider": "ollama", "model": "nomic-embed-text"},
    },
    "parser": {"workers": 0, "table_strategy": "auto"},  # workers=0 -> cpu_count-1
    "llm": {"max_concurrency": 2, "max_retries": 2},
    "logging": {"min_level": "info", "capture_prompts": True, "preview_chars": 4000},  # capture_prompts: keep LLM prompts/replies in the activity log
    "mi_layout": {},  # overrides for the NVMe-MI header layout, see cmddb.MI_DEFAULT_LAYOUT
    # the MCP endpoint (/mcp) for coding agents: on/off, requests per minute per address (0: no limit), how long a tool waits for a
    # version comparison it started, one Activity-log line per call, and the URL agents use (empty: the address the UI is reached at)
    "mcp": {"enabled": True, "rate_limit_per_min": 120, "compare_wait_s": 60, "log_calls": True, "public_url": "",
            "library_search": True},
}


def workspace_dir() -> Path:
    root = Path(os.environ.get("SPECFORGE_HOME", Path.home() / ".specforge"))
    root.mkdir(parents=True, exist_ok=True)
    (root / "specs").mkdir(exist_ok=True)
    (root / "uploads").mkdir(exist_ok=True)
    return root


def auth_enabled() -> bool:
    """Login required for the API (default). `specforge serve --no-auth` turns it off for a single user on this machine."""
    return os.environ.get("SPECFORGE_AUTH", "1").strip().lower() not in ("0", "off", "false", "no")


def db_path() -> Path:
    return workspace_dir() / "app.db"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings(conn) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM setting WHERE key='settings'").fetchone()
    stored = json.loads(row[0]) if row else {}
    return deep_merge(DEFAULT_SETTINGS, stored)


def save_settings(conn, settings: dict[str, Any]) -> dict[str, Any]:
    merged = deep_merge(DEFAULT_SETTINGS, settings)
    conn.execute(
        "INSERT INTO setting(key,value) VALUES('settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(merged),),
    )
    conn.commit()
    return merged
