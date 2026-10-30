"""SQLite metadata store (workspace/app.db). Plain sqlite3, WAL mode, JSON columns."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS spec_document(
  id INTEGER PRIMARY KEY, family TEXT NOT NULL, version TEXT NOT NULL, title TEXT,
  sha256 TEXT NOT NULL UNIQUE, path TEXT NOT NULL, filename TEXT, format TEXT NOT NULL,
  pages INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parse_run(
  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES spec_document(id) ON DELETE CASCADE,
  parser_version TEXT NOT NULL, config_hash TEXT, status TEXT NOT NULL,
  stats_json TEXT, audit_json TEXT, active INTEGER DEFAULT 0, error TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ir_node(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES parse_run(id) ON DELETE CASCADE,
  key TEXT NOT NULL, kind TEXT NOT NULL, parent_key TEXT, ord INTEGER NOT NULL,
  number TEXT, title TEXT, text TEXT, page_from INTEGER, page_to INTEGER, attrs_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_ir_run_key ON ir_node(run_id, key);
CREATE INDEX IF NOT EXISTS ix_ir_run_kind ON ir_node(run_id, kind);
CREATE INDEX IF NOT EXISTS ix_ir_run_parent ON ir_node(run_id, parent_key);

CREATE TABLE IF NOT EXISTS ir_ref(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES parse_run(id) ON DELETE CASCADE,
  src_key TEXT NOT NULL, raw TEXT NOT NULL, kind TEXT NOT NULL,
  target_family TEXT, target_label TEXT NOT NULL, ord INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ref_run ON ir_ref(run_id);

CREATE TABLE IF NOT EXISTS parse_issue(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES parse_run(id) ON DELETE CASCADE,
  severity TEXT NOT NULL, kind TEXT NOT NULL, node_key TEXT, page INTEGER, message TEXT,
  state TEXT DEFAULT 'open', note TEXT
);
CREATE INDEX IF NOT EXISTS ix_issue_run ON parse_issue(run_id);

-- Manual table corrections keyed by stable key; survive reparse.
CREATE TABLE IF NOT EXISTS correction(
  id INTEGER PRIMARY KEY, family TEXT NOT NULL, version TEXT NOT NULL, stable_key TEXT NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(family, version, stable_key, kind)
);

CREATE TABLE IF NOT EXISTS spec_set(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS spec_set_member(
  set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  document_id INTEGER NOT NULL REFERENCES spec_document(id) ON DELETE CASCADE,
  PRIMARY KEY(set_id, document_id)
);
-- User choices for ambiguous cross-spec references
CREATE TABLE IF NOT EXISTS ref_choice(
  set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE, src_key TEXT NOT NULL, raw TEXT NOT NULL, chosen_key TEXT NOT NULL,
  PRIMARY KEY(set_id, src_key, raw)
);

CREATE TABLE IF NOT EXISTS catalog_item(
  id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  key TEXT NOT NULL, kind TEXT NOT NULL, parent_key TEXT, label TEXT, observability TEXT NOT NULL,
  na_reason TEXT, attrs_json TEXT, UNIQUE(set_id, key)
);
CREATE INDEX IF NOT EXISTS ix_cat_set_kind ON catalog_item(set_id, kind);

-- Reviewer decisions on catalog items; survive catalog rebuilds
CREATE TABLE IF NOT EXISTS item_override(
  set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE, key TEXT NOT NULL, observability TEXT, na_reason TEXT,
  PRIMARY KEY(set_id, key)
);

CREATE TABLE IF NOT EXISTS scenario(
  id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  sid TEXT NOT NULL, origin TEXT NOT NULL, kind TEXT, title TEXT, description TEXT,
  observability TEXT NOT NULL, predicate_json TEXT NOT NULL, state TEXT NOT NULL,
  provenance_json TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
  source_id INTEGER REFERENCES cmd_source(id) ON DELETE CASCADE,     -- set for scenarios derived from a command DB (origin 'exec')
  UNIQUE(set_id, sid)
);
-- state: NULL for links of spec-driven scenarios (judged by the scenario's own review state); for scenarios derived from a
-- command DB: 'observed' (read from the log), or 'proposed' / 'approved' / 'rejected' (an AI-proposed requirement link and its review)
CREATE TABLE IF NOT EXISTS scenario_item(
  scenario_id INTEGER NOT NULL REFERENCES scenario(id) ON DELETE CASCADE, item_key TEXT NOT NULL,
  state TEXT, detail_json TEXT,
  PRIMARY KEY(scenario_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_si_item ON scenario_item(item_key);

-- The last analysis of a command DB against a spec set (scenarios derived from what the test scripts executed)
CREATE TABLE IF NOT EXISTS exec_analysis(
  set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES cmd_source(id) ON DELETE CASCADE,
  stats_json TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(set_id, source_id)
);

-- What each executed test (one database of a folder source, or one test name) exercised, as of the last analysis
CREATE TABLE IF NOT EXISTS exec_test(
  set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES cmd_source(id) ON DELETE CASCADE,
  test_id TEXT NOT NULL, script TEXT, scenario_sid TEXT, commands INTEGER, unknown INTEGER, items_json TEXT,
  PRIMARY KEY(set_id, source_id, test_id)
);

CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, provider TEXT, model TEXT, response TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS enrichment(
  id INTEGER PRIMARY KEY, set_id INTEGER, item_key TEXT, task TEXT, provider TEXT, model TEXT,
  prompt_version TEXT, state TEXT, output_json TEXT, error TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cmd_source(
  id INTEGER PRIMARY KEY, name TEXT, path TEXT NOT NULL, sha256 TEXT, mapping_json TEXT NOT NULL,
  row_count INTEGER DEFAULT 0, decoded_count INTEGER DEFAULT 0, unknown_json TEXT,
  imported_at TEXT, status TEXT DEFAULT 'new'
);
CREATE TABLE IF NOT EXISTS cmd_signature(
  source_id INTEGER NOT NULL REFERENCES cmd_source(id) ON DELETE CASCADE, signature TEXT NOT NULL,
  interface TEXT, queue TEXT, cs TEXT, opcode INTEGER, nsid INTEGER, dwords_json TEXT, count INTEGER DEFAULT 0,
  PRIMARY KEY(source_id, signature)
);
CREATE INDEX IF NOT EXISTS ix_sig_op ON cmd_signature(source_id, interface, queue, opcode);
CREATE TABLE IF NOT EXISTS decoded_command(
  id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES cmd_source(id) ON DELETE CASCADE,
  src_row_id INTEGER, test_id TEXT, script TEXT, seq INTEGER, interface TEXT, queue TEXT, cs TEXT,
  opcode INTEGER, nsid INTEGER, signature TEXT, dwords_json TEXT, extra_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_dc_src_op ON decoded_command(source_id, interface, queue, opcode);
CREATE INDEX IF NOT EXISTS ix_dc_src_test ON decoded_command(source_id, test_id, seq);
CREATE INDEX IF NOT EXISTS ix_dc_sig ON decoded_command(source_id, signature);

CREATE TABLE IF NOT EXISTS coverage_run(
  id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL REFERENCES spec_set(id) ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES cmd_source(id) ON DELETE CASCADE, summary_json TEXT,
  meta_json TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS coverage_result(
  run_id INTEGER NOT NULL REFERENCES coverage_run(id) ON DELETE CASCADE, scenario_id INTEGER NOT NULL,
  state TEXT NOT NULL, match_count INTEGER DEFAULT 0, evidence_json TEXT,
  sid TEXT, origin TEXT, kind TEXT, title TEXT, observability TEXT, review_state TEXT, predicate_json TEXT,   -- snapshot of the scenario as judged
  PRIMARY KEY(run_id, scenario_id)
);
CREATE TABLE IF NOT EXISTS coverage_test(
  run_id INTEGER NOT NULL REFERENCES coverage_run(id) ON DELETE CASCADE, test_id TEXT NOT NULL,
  stats_json TEXT, items_json TEXT, PRIMARY KEY(run_id, test_id)
);
CREATE TABLE IF NOT EXISTS item_result(
  run_id INTEGER NOT NULL REFERENCES coverage_run(id) ON DELETE CASCADE, item_key TEXT NOT NULL,
  state TEXT NOT NULL, covered INTEGER DEFAULT 0, total INTEGER DEFAULT 0, detail_json TEXT,
  kind TEXT, parent_key TEXT, label TEXT, observability TEXT, na_reason TEXT, attrs_json TEXT,   -- run-time snapshot of the catalog item
  PRIMARY KEY(run_id, item_key)
);

CREATE TABLE IF NOT EXISTS event_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL, source TEXT NOT NULL,
  job_id INTEGER, message TEXT NOT NULL, data_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_event_job ON event_log(job_id, id);
CREATE INDEX IF NOT EXISTS ix_event_source ON event_log(source, id);

-- A comparison of two parsed versions of a spec (see compare.py); its changes are rows so they can be filtered and paged
CREATE TABLE IF NOT EXISTS spec_compare(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_a INTEGER NOT NULL REFERENCES parse_run(id) ON DELETE CASCADE,
  run_b INTEGER NOT NULL REFERENCES parse_run(id) ON DELETE CASCADE,
  algo TEXT, summary_json TEXT NOT NULL, ai_json TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_a, run_b)
);
CREATE TABLE IF NOT EXISTS compare_change(
  id INTEGER PRIMARY KEY, compare_id INTEGER NOT NULL REFERENCES spec_compare(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL, kind TEXT NOT NULL, change TEXT NOT NULL, impact TEXT NOT NULL, score INTEGER DEFAULT 0,
  chapter TEXT, number_from TEXT, number_to TEXT, title TEXT, nested INTEGER DEFAULT 0, detail_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_cc ON compare_change(compare_id, ord);
-- Accounts and login sessions (see auth.py). Sessions store only the SHA-256 of their token.
CREATE TABLE IF NOT EXISTS app_user(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'pending', note TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP, decided_at TEXT, decided_by TEXT, last_login TEXT
);
CREATE TABLE IF NOT EXISTS user_session(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
  created TEXT NOT NULL, last_seen TEXT NOT NULL, expires TEXT NOT NULL, ip TEXT, user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_session_user ON user_session(user_id);
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, state TEXT NOT NULL, progress INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0, message TEXT, payload_json TEXT, result_json TEXT, error TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP
);
-- Search index of the MCP endpoint (specquery.py): one entry per section, table and table row of each served parse run.
-- Created here with the rest of the schema, never at run time: DDL on a busy database fails other connections' statements.
CREATE VIRTUAL TABLE IF NOT EXISTS mcp_fts USING fts5(number, title, body, run_id UNINDEXED, key UNINDEXED, kind UNINDEXED,
  page UNINDEXED, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS mcp_index(run_id INTEGER PRIMARY KEY, run_created TEXT, version TEXT, entries INTEGER, seconds REAL,
  built TEXT DEFAULT CURRENT_TIMESTAMP);
-- the same, for converted documents of the To Markdown library that are not parsed (their mcp_fts run_id is 'lib:<folder>')
CREATE TABLE IF NOT EXISTS mcp_lib_index(dir TEXT PRIMARY KEY, filename TEXT, version TEXT, entries INTEGER, seconds REAL,
  built TEXT DEFAULT CURRENT_TIMESTAMP);
-- Every MCP tool call (and rate-limited / refused request), for the admin Metrics, Misses & errors and Call log tabs (mcp_metrics.py)
CREATE TABLE IF NOT EXISTS mcp_call(id INTEGER PRIMARY KEY, ts REAL NOT NULL, tool TEXT NOT NULL, outcome TEXT NOT NULL, ms REAL NOT NULL,
  args_json TEXT, error TEXT, reason TEXT, fingerprint TEXT, ip TEXT, agent TEXT, agent_version TEXT, user_agent TEXT, transport TEXT,
  spec TEXT, version TEXT, answer_chars INTEGER, page INTEGER, paged INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_mcp_call_ts ON mcp_call(ts);
CREATE INDEX IF NOT EXISTS ix_mcp_call_fp ON mcp_call(fingerprint, ts);
CREATE INDEX IF NOT EXISTS ix_mcp_call_client ON mcp_call(ip, ts);
-- An admin's triage of one group of misses or errors (mcp_call.fingerprint): open / investigating / fixed / ignored, and a note
CREATE TABLE IF NOT EXISTS mcp_issue(fingerprint TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open', note TEXT, updated TEXT, updated_by TEXT);
-- Other users following a job they asked for while it was already queued or running (same file, same spec)
CREATE TABLE IF NOT EXISTS job_watcher(
  job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE, user TEXT NOT NULL, PRIMARY KEY(job_id, user)
);
"""

# Columns added after the first release: (table, column, declaration).  CREATE TABLE IF NOT EXISTS leaves an existing table as it
# was, so a workspace created by an older version gets them here.
MIGRATIONS = [
    ("scenario", "source_id", "INTEGER REFERENCES cmd_source(id) ON DELETE CASCADE"),
    ("scenario_item", "state", "TEXT"),
    ("scenario_item", "detail_json", "TEXT"),
    ("job", "user", "TEXT"),        # who asked for it (NULL: started from the CLI or before jobs had owners)
    ("job", "label", "TEXT"),       # what it is about, as the notification bell shows it ("Parse NVMe Base 2.4")
    ("job", "queue", "TEXT"),       # the first-come-first-served line it waits in ('docling': one conversion at a time)
]

_local = threading.local()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError as e:      # another connection (a writer thread) migrated it first
                if "duplicate column" not in str(e):
                    raise
    conn.execute("CREATE INDEX IF NOT EXISTS ix_scenario_source ON scenario(set_id, source_id)")
    conn.commit()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = str(path or db_path())
    conn = sqlite3.connect(p, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def get_conn(path: str | Path | None = None) -> sqlite3.Connection:
    """Per-thread connection."""
    key = str(path or db_path())
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    if key not in conns:
        conns[key] = connect(key)
    return conns[key]


def rows(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


def row(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(args)).fetchone()
    return dict(r) if r else None


def jloads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    return json.loads(s)
