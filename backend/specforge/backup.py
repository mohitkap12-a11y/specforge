"""Workspace backup: export selected areas of the workspace (database rows and files) to a zip, and import such a zip into
another workspace, e.g. to move a SpecForge server to a new machine.

Zip layout (format 2):
  backup_meta.json          {"format", "app_version", "parser_version", "created", "workspace", "items", "tables": {name: rows}}
  db/<table>.jsonl          one JSON object per row (bytes as {"$b64": "..."})
  files/<dir>/...           the workspace folders of the exported items (specs/, uploads/, cmddb-uploads/, conversions/;
                            models/ holds the Docling models folder, wherever Settings -> Offline models puts it)

Import rules:
  * the whole database part runs in one transaction with foreign keys deferred, and is checked with PRAGMA foreign_key_check
    before commit, so it is applied completely or not at all;
  * "replace" clears the selected items first (refused when that would cascade into an item that is not being imported);
    "merge" only adds to tables that can take it (settings, LLM cache, MCP history, users) and refuses to mix id-keyed data
    into an area that already has some;
  * absolute paths that pointed into the old workspace are rewritten to the new one;
  * search indexes are not backed up: they are rebuilt after an import.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path
from typing import IO, Any, Callable

from . import __version__
from .core import config

FORMAT_VERSION = 2
META = "backup_meta.json"
BATCH = 1000
REDACTED = "__redacted__"
SECRET_KEY = re.compile(r"(api[_-]?key|secret|token|password)", re.I)

# Per-table import policy:
#   insert  plain INSERT; in merge mode the table must be empty (its ids are referenced by other tables)
#   append  INSERT without the id column: rows are added after what is there (logs)
#   ignore  INSERT OR IGNORE: rows whose key already exists are kept as they are
#   upsert  INSERT ... ON CONFLICT DO UPDATE (tables nothing references)
# Items are listed parents first, and so are the tables inside each item.
ITEMS: dict[str, dict[str, Any]] = {
    "specs": {"label": "Specs & parse runs", "requires": [], "dirs": ["specs", "uploads"],
              "tables": {"spec_document": "insert", "parse_run": "insert", "ir_node": "insert", "ir_ref": "insert",
                         "parse_issue": "insert", "correction": "insert"}},
    "sets_catalog": {"label": "Sets, catalog & scenarios", "requires": ["specs"], "dirs": [],
                     "tables": {"spec_set": "insert", "spec_set_member": "insert", "ref_choice": "insert", "catalog_item": "insert",
                                "item_override": "insert", "scenario": "insert", "scenario_item": "insert", "enrichment": "insert"}},
    "compare": {"label": "Spec Compare", "requires": ["specs"], "dirs": [],
                "tables": {"spec_compare": "insert", "compare_change": "insert"}},
    "coverage": {"label": "Coverage Tool (command DBs & runs)", "requires": ["sets_catalog"], "dirs": ["cmddb-uploads"],
                 "tables": {"cmd_source": "insert", "cmd_signature": "insert", "decoded_command": "insert", "exec_analysis": "insert",
                            "exec_test": "insert", "coverage_run": "insert", "coverage_result": "insert", "coverage_test": "insert",
                            "item_result": "insert"}},
    "converter": {"label": "To Markdown library", "requires": [], "dirs": ["conversions"], "tables": {}},
    "knowledge": {"label": "Knowledge base (documents for the chatbot)", "requires": [], "dirs": ["kb"],
                  "tables": {"kb_doc": "insert", "kb_chunk": "insert"}},
    "chats": {"label": "Chatbot history (every user's chats)", "requires": [], "dirs": [],
              "tables": {"chat_session": "insert", "chat_message": "insert"}},
    "mcp": {"label": "Spec MCP call history", "requires": [], "dirs": [], "tables": {"mcp_issue": "upsert", "mcp_call": "append"}},
    "settings": {"label": "Settings", "requires": [], "dirs": [], "tables": {"setting": "upsert"}},
    "users": {"label": "User accounts (password hashes, no sessions)", "requires": [], "dirs": [], "tables": {"app_user": "ignore"}},
    "jobs_activity": {"label": "Jobs & activity log", "requires": [], "dirs": [],
                      "tables": {"job": "insert", "job_watcher": "insert", "event_log": "append"}},
    "llm_cache": {"label": "LLM response cache", "requires": [], "dirs": [], "tables": {"llm_cache": "ignore"}},
    # several hundred MB: not ticked by default in the UI, and left out of "all" unless asked for by name
    "models": {"label": "Docling models (offline conversion, large)", "requires": [], "dirs": ["models"], "tables": {},
               "default": False},
}
# Folders that are not <workspace>/<name>: name -> a function giving the folder on this server (set by the API app)
DIR_ROOTS: dict[str, Callable[[], Path]] = {}


def dir_root(ws: Path, dname: str) -> Path:
    return DIR_ROOTS[dname]() if dname in DIR_ROOTS else ws / dname
# Absolute paths stored in the database that usually point into the workspace
PATH_COLUMNS = {"spec_document": "path", "cmd_source": "path"}
# Derived search indexes: dropped after importing what they index, then rebuilt
INDEX_TABLES = ("mcp_fts", "mcp_index", "mcp_lib_index")


class BackupError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def options() -> list[dict]:
    return [{"id": k, "label": v["label"], "requires": v["requires"], "dirs": v["dirs"],
             "mergeable": all(p != "insert" for p in v["tables"].values()), "default": v.get("default", True)}
            for k, v in ITEMS.items()]


def with_requirements(items: set[str]) -> set[str]:
    """The items plus everything they depend on."""
    out, todo = set(), list(items)
    while todo:
        k = todo.pop()
        if k not in out:
            out.add(k)
            todo.extend(ITEMS[k]["requires"])
    return out


def dependents(items: set[str]) -> set[str]:
    """Every item whose rows would be cascade-deleted when the given items are cleared."""
    out = set()
    changed = True
    while changed:
        changed = False
        for k, v in ITEMS.items():
            if k not in out and k not in items and any(r in items or r in out for r in v["requires"]):
                out.add(k)
                changed = True
    return out


def parse_items(raw: str | None) -> set[str]:
    if not raw:
        return {k for k, v in ITEMS.items() if v.get("default", True)}
    picked = {x.strip() for x in raw.split(",") if x.strip()}
    if unknown := picked - set(ITEMS):
        raise BackupError(f"Unknown backup item(s): {', '.join(sorted(unknown))}; use: {', '.join(ITEMS)}")
    if not picked:
        raise BackupError("Select at least one item")
    return picked


def ordered(items: set[str]) -> list[str]:
    return [k for k in ITEMS if k in items]


def table_columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


# ---------------------------------------------------------------------------------------------------------------- export
def _encode(v: Any) -> Any:
    return {"$b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray, memoryview)) else v


def _decode(v: Any) -> Any:
    return base64.b64decode(v["$b64"]) if isinstance(v, dict) and set(v) == {"$b64"} else v


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (REDACTED if SECRET_KEY.search(k) and isinstance(v, str) and v else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _row_filter(table: str, items: set[str]) -> str:
    """Rows that cannot go without an item that is not exported: scenarios derived from a command DB need Coverage."""
    if "coverage" not in items:
        if table == "scenario":
            return " WHERE source_id IS NULL"
        if table == "scenario_item":
            return " WHERE scenario_id IN (SELECT id FROM scenario WHERE source_id IS NULL)"
    return ""


def export(db_path: Path, out: IO[bytes], items: set[str], include_secrets: bool = False) -> dict:
    """Write a backup of the items (and what they depend on) to out. Database rows come from one read transaction, so
    they are a consistent snapshot even while the server keeps writing. Returns the meta written to the zip."""
    items = with_requirements(items)
    ws = config.workspace_dir()
    src = sqlite3.connect(str(db_path), timeout=60)
    counts: dict[str, int] = {}
    try:
        src.execute("BEGIN")          # WAL: every SELECT below sees the same snapshot
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for key in ordered(items):
                for table in ITEMS[key]["tables"]:
                    cur = src.execute(f"SELECT * FROM {table}{_row_filter(table, items)}")
                    cols = [d[0] for d in cur.description]
                    n = 0
                    with z.open(f"db/{table}.jsonl", "w", force_zip64=True) as f:
                        for r in cur:
                            d = {c: _encode(v) for c, v in zip(cols, r)}
                            if table == "setting" and not include_secrets:
                                d["value"] = _redact_setting(d["value"])
                            f.write(json.dumps(d, ensure_ascii=False).encode() + b"\n")
                            n += 1
                    counts[table] = n
            files = 0
            for key in ordered(items):
                for dname in ITEMS[key]["dirs"]:
                    root = dir_root(ws, dname)
                    if not root.is_dir():
                        continue
                    for dirpath, _dirs, names in os.walk(root):
                        for fn in names:
                            full = Path(dirpath) / fn
                            if full.is_symlink() or not full.is_file():
                                continue
                            z.write(full, f"files/{dname}/" + full.relative_to(root).as_posix())
                            files += 1
            meta = {"format": FORMAT_VERSION, "app_version": __version__, "parser_version": config.PARSER_VERSION,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "workspace": str(ws), "items": ordered(items),
                    "tables": counts, "files": files, "secrets_included": include_secrets}
            z.writestr(META, json.dumps(meta, indent=1))
    finally:
        src.rollback()
        src.close()
    return meta


def _redact_setting(value: Any) -> Any:
    try:
        return json.dumps(_redact(json.loads(value)))
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------------------------------------------- import
def read_meta(z: zipfile.ZipFile) -> dict:
    try:
        meta = json.loads(z.read(META))
    except KeyError:
        raise BackupError("Not a SpecForge backup: backup_meta.json is missing") from None
    except ValueError as e:
        raise BackupError(f"Damaged backup: backup_meta.json is not valid JSON ({e})") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("items"), list):
        raise BackupError("Damaged backup: backup_meta.json has no item list")
    fmt = meta.get("format")
    if fmt != FORMAT_VERSION:
        raise BackupError(f"This backup has format {fmt!r}; this SpecForge ({__version__}) reads format {FORMAT_VERSION}. "
                          "Export it again with a matching version.")
    return meta


def _safe_target(ws: Path, name: str, allowed_dirs: set[str]) -> Path | None:
    """Where a files/ entry goes, or None when it is not one of the allowed folders. Raises on paths that escape."""
    rel = name[len("files/"):]
    parts = rel.replace("\\", "/").split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts[:-1]) or parts[-1] in ("", ".", "..") \
            or re.match(r"^[A-Za-z]:", rel):
        raise BackupError(f"Unsafe path in backup: {name}")
    if parts[0] not in allowed_dirs:
        return None
    target = (ws / Path(*parts)).resolve()
    if not target.is_relative_to(ws.resolve()):
        raise BackupError(f"Unsafe path in backup: {name}")
    return target


def _rewrite_path(value: Any, old_ws: str | None, new_ws: Path) -> Any:
    """A path under the old workspace, moved under the new one (works across Windows and POSIX separators)."""
    if not isinstance(value, str) or not old_ws:
        return value
    old = old_ws.rstrip("/\\")
    if value == old:
        return str(new_ws)
    if value.startswith(old) and value[len(old):len(old) + 1] in ("/", "\\"):
        rest = [p for p in re.split(r"[\\/]+", value[len(old) + 1:]) if p]
        return str(new_ws.joinpath(*rest))
    return value


def _rows(z: zipfile.ZipFile, table: str):
    name = f"db/{table}.jsonl"
    try:
        f = z.open(name)
    except KeyError:
        raise BackupError(f"Damaged backup: {name} is missing") from None
    with f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except ValueError as e:
                raise BackupError(f"Damaged backup: {name} line {i} is not valid JSON ({e})") from None
            if not isinstance(d, dict):
                raise BackupError(f"Damaged backup: {name} line {i} is not an object")
            yield d


def _merge_settings(conn, value: str) -> str:
    """Imported settings over the current ones; redacted secrets keep this server's value."""
    cur_row = conn.execute("SELECT value FROM setting WHERE key='settings'").fetchone()
    try:
        cur = json.loads(cur_row[0]) if cur_row else {}
        new = json.loads(value)
    except (TypeError, ValueError):
        return value

    def drop_redacted(o):
        if isinstance(o, dict):
            return {k: drop_redacted(v) for k, v in o.items() if v != REDACTED}
        return o
    return json.dumps(config.deep_merge(cur if isinstance(cur, dict) else {}, drop_redacted(new)))


def import_(conn: sqlite3.Connection, zf: IO[bytes], items: set[str] | None, replace: bool,
            keep_user_id: int | None = None, on_files_ready: Callable[[], None] | None = None) -> dict:
    """Import a backup zip. items None means every item in the backup. Raises BackupError (nothing changed) on any problem
    found before or during the database part."""
    try:
        z = zipfile.ZipFile(zf)
    except (zipfile.BadZipFile, OSError) as e:
        raise BackupError(f"Not a zip file: {e}") from None
    with z:
        meta = read_meta(z)
        in_zip = {k for k in meta["items"] if k in ITEMS}
        if items is None:
            items = in_zip
        if missing := items - in_zip:
            raise BackupError("The backup does not contain: " + ", ".join(ITEMS[k]["label"] for k in ordered(missing)))
        if not items:
            raise BackupError("Nothing to import")
        warnings: list[str] = []

        # what a replace would cascade into
        if replace:
            hit = [k for k in ordered(dependents(items)) if any(_count(conn, t) for t in ITEMS[k]["tables"])]
            if hit:
                raise BackupError("Replacing " + ", ".join(ITEMS[k]["label"] for k in ordered(items))
                                  + " would also delete this workspace's " + ", ".join(ITEMS[k]["label"] for k in hit)
                                  + ", which depend on it. Select those items too.", 409)
        else:
            busy = [(k, t) for k in ordered(items) for t, p in ITEMS[k]["tables"].items() if p == "insert" and _count(conn, t)]
            if busy:
                raise BackupError("This workspace already has data for " + ", ".join(dict.fromkeys(ITEMS[k]["label"] for k, _ in busy))
                                  + ". Turn on 'Replace existing data' to overwrite it, or leave those items out.", 409)

        # stage the files first: unsafe names fail the import before anything is changed
        ws = config.workspace_dir()
        dirs = {d for k in items for d in ITEMS[k]["dirs"]}
        staging = ws / f".backup-import-{uuid.uuid4().hex[:8]}"
        staged = 0
        try:
            for info in z.infolist():
                if not info.filename.startswith("files/") or info.is_dir():
                    continue
                target = _safe_target(ws, info.filename, dirs)
                if target is None:
                    continue
                dest = staging / target.relative_to(ws.resolve())
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                staged += 1

            counts = _import_db(conn, z, items, replace, meta, ws, keep_user_id, warnings)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # database committed: put the files in place
        files = 0
        try:
            for dname in sorted(dirs):
                root = dir_root(ws, dname)
                if replace and root.is_dir():
                    for f in root.iterdir():
                        if f.is_dir() and not f.is_symlink():
                            shutil.rmtree(f, ignore_errors=True)
                        else:
                            f.unlink(missing_ok=True)
                sroot = staging / dname
                if not sroot.is_dir():
                    continue
                for dirpath, _d, names in os.walk(sroot):
                    for fn in names:
                        s = Path(dirpath) / fn
                        t = root / s.relative_to(sroot)
                        t.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(s, t)
                        files += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if files != staged:
            warnings.append(f"{staged - files} file(s) could not be put in place")

        for table, col in PATH_COLUMNS.items():
            if not any(table in ITEMS[k]["tables"] for k in items):
                continue
            gone = [r[0] for r in conn.execute(f"SELECT {col} FROM {table}") if r[0] and not Path(r[0]).exists()]
            if gone:
                warnings.append(f"{len(gone)} {table} path(s) do not exist on this machine (e.g. {gone[0]})")
        if on_files_ready:
            on_files_ready()
        return {"ok": True, "items": ordered(items), "rows": counts, "files": files, "warnings": warnings,
                "from_version": meta.get("app_version")}


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _import_db(conn: sqlite3.Connection, z: zipfile.ZipFile, items: set[str], replace: bool, meta: dict, ws: Path,
               keep_user_id: int | None, warnings: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    old_ws = meta.get("workspace") if isinstance(meta.get("workspace"), str) else None
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("PRAGMA defer_foreign_keys=ON")     # parents and children are checked together at the end
        if replace:
            for key in reversed(ordered(items)):
                for table in reversed(list(ITEMS[key]["tables"])):
                    if table == "app_user" and keep_user_id is not None:
                        conn.execute("DELETE FROM app_user WHERE id<>?", (keep_user_id,))
                    elif table != "setting":            # settings are merged, never wiped
                        conn.execute(f"DELETE FROM {table}")
        for key in ordered(items):
            for table, policy in ITEMS[key]["tables"].items():
                counts[table] = _import_table(conn, z, table, policy, old_ws, ws, warnings)
        if {"specs", "converter"} & items:
            for t in INDEX_TABLES:
                conn.execute(f"DELETE FROM {t}")
        if "knowledge" in items:         # the search index is derived: rebuilt from the chunks
            conn.execute("DELETE FROM kb_fts")
            conn.execute("INSERT INTO kb_fts(heading, text, doc_id, chunk_id) SELECT trim(d.title || ' ' || coalesce(c.heading, '')), c.text, c.doc_id, c.id "
                         "FROM kb_chunk c JOIN kb_doc d ON d.id = c.doc_id")
        if ("knowledge" in items) != ("jobs_activity" in items):
            # only one side was restored: job numbers in the knowledge base no longer name the conversions they came from
            conn.execute("UPDATE kb_doc SET job_id=NULL")
        if "jobs_activity" in items:
            conn.execute("UPDATE job SET state='failed', error='interrupted: imported from a backup' WHERE state IN ('queued','running')")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            by = {}
            for r in bad:
                by.setdefault((r[0], r[2]), 0)
                by[(r[0], r[2])] += 1
            raise BackupError("The backup does not fit this workspace: "
                              + "; ".join(f"{n} {t} row(s) refer to missing {p}" for (t, p), n in sorted(by.items()))
                              + ". Import the items they depend on too.")
        conn.commit()
    except BackupError:
        conn.rollback()
        raise
    except sqlite3.Error as e:
        conn.rollback()
        raise BackupError(f"Import failed, nothing was changed: {e}") from None
    except BaseException:
        conn.rollback()
        raise
    return counts


def _import_table(conn, z, table: str, policy: str, old_ws: str | None, ws: Path, warnings: list[str]) -> int:
    known = table_columns(conn, table)
    if not known:
        raise BackupError(f"Table {table} does not exist in this workspace")
    cols: list[str] | None = None
    sql = ""
    n = read = 0
    batch: list[tuple] = []
    path_col = PATH_COLUMNS.get(table)
    for d in _rows(z, table):
        if cols is None:
            extra = [c for c in d if c not in known]
            if extra:
                warnings.append(f"{table}: column(s) {', '.join(extra)} are not in this version and were skipped")
            cols = [c for c in d if c in known and not (policy == "append" and c == "id")
                    and not (table == "app_user" and c == "id")]
            if not cols:
                raise BackupError(f"Damaged backup: {table} rows have no known columns")
            qcols = ",".join(f'"{c}"' for c in cols)
            marks = ",".join("?" * len(cols))
            if policy == "upsert":
                pk = [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[5]]
                upd = ",".join(f'"{c}"=excluded."{c}"' for c in cols if c not in pk)
                sql = f"INSERT INTO {table}({qcols}) VALUES({marks}) ON CONFLICT({','.join(pk)}) DO UPDATE SET {upd}"
            elif policy == "ignore":
                sql = f"INSERT OR IGNORE INTO {table}({qcols}) VALUES({marks})"
            else:
                sql = f"INSERT INTO {table}({qcols}) VALUES({marks})"
        vals = []
        for c in cols:
            v = _decode(d.get(c))
            if c == path_col:
                v = _rewrite_path(v, old_ws, ws)
            if table == "setting" and c == "value" and d.get("key") == "settings":
                v = _merge_settings(conn, v)
            vals.append(v)
        batch.append(tuple(vals))
        read += 1
        if len(batch) >= BATCH:
            n += _flush(conn, sql, batch)
            batch = []
    if batch:
        n += _flush(conn, sql, batch)
    if policy == "ignore" and n < read:
        what = "user account(s) with a username that already exists here" if table == "app_user" else f"{table} row(s) already present"
        warnings.append(f"{read - n} {what} were kept as they are")
    return n


def _flush(conn, sql: str, batch: list[tuple]) -> int:
    before = conn.total_changes
    conn.executemany(sql, batch)
    return conn.total_changes - before
