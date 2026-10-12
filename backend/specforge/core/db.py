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
  provenance_json TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(set_id, sid)
);
CREATE TABLE IF NOT EXISTS scenario_item(
  scenario_id INTEGER NOT NULL REFERENCES scenario(id) ON DELETE CASCADE, item_key TEXT NOT NULL,
  PRIMARY KEY(scenario_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_si_item ON scenario_item(item_key);

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

CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, state TEXT NOT NULL, progress INTEGER DEFAULT 0,
  total INTEGER DEFAULT 0, message TEXT, payload_json TEXT, result_json TEXT, error TEXT,
  created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

_local = threading.local()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    p = str(path or db_path())
    conn = sqlite3.connect(p, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
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
