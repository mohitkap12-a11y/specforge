"""Command database adapter: read a SQLite dump of submitted commands, decode them into named dwords.

Only submission entries are modelled: NVMe SQEs (64 bytes) and NVMe-MI request messages.  Decoding is
bit-exact; naming of fields comes from the parsed spec catalog at match time, not from hard-coded tables.

The NVMe-MI header layout below is a *default* that must be verified against the MI revision you test.
It can be overridden through settings.mi_layout.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from pathlib import Path
from typing import Any, Iterator

from . import eventlog
from .core.db import row
from .core.hashing import sha256_file, stable_hash

PROGRESS_EVERY = 2000   # rows scanned between progress / cancellation checks

MI_DEFAULT_LAYOUT: dict[str, Any] = {
    "_note": "Default NVMe-MI request layout. VERIFY against the NVMe-MI revision under test; override in Settings.",
    "nmimt": {"byte": 1, "hi": 6, "lo": 3},
    "ror": {"byte": 1, "hi": 7, "lo": 7},
    "csi": {"byte": 1, "hi": 0, "lo": 0},
    "nmimt_map": {"1": "mi", "2": "admin"},  # NMIMT value -> decoded queue
    "mi": {"opcode_byte": 4, "nmd0_off": 8, "nmd1_off": 12},
    "admin": {"opcode_byte": 4, "flags_byte": 5, "ctlid_off": 6,
              "dwords": {"1": 8, "2": 12, "3": 16, "4": 20, "5": 24, "10": 28, "11": 32, "12": 36, "13": 40, "14": 44, "15": 48}},
}

# Dwords that never take part in a signature: CID (bits 31:16 of dword 0), MPTR, DPTR.
_SIG_MASK = {"0": 0x0000FFFF}
_SIG_SKIP = {"4", "5", "6", "7", "8", "9"}


def _deep_update(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_update(dst[k], v)       # a partial override (e.g. one entry of admin.dwords) keeps the other defaults
        else:
            dst[k] = v
    return dst


def merged_layout(overrides: dict | None) -> dict:
    return _deep_update(json.loads(json.dumps(MI_DEFAULT_LAYOUT)), overrides or {})


# ----------------------------------------------------------------------------- decoders

def decode_sqe(blob: bytes) -> dict[str, Any]:
    if blob is None or len(blob) != 64:
        raise ValueError(f"SQE must be 64 bytes, got {0 if blob is None else len(blob)}")
    dws = struct.unpack("<16I", bytes(blob))
    return _from_dwords({str(i): v for i, v in enumerate(dws)})


def _from_dwords(dw: dict[str, int]) -> dict[str, Any]:
    d0 = dw["0"]
    return {"interface": "pcie", "opcode": d0 & 0xFF, "nsid": dw.get("1", 0), "dwords": dw,
            "extra": {"fuse": (d0 >> 8) & 3, "psdt": (d0 >> 14) & 3, "cid": (d0 >> 16) & 0xFFFF}}


def _bits(buf: bytes, spec: dict) -> int:
    v = buf[spec["byte"]]
    width = spec["hi"] - spec["lo"] + 1
    return (v >> spec["lo"]) & ((1 << width) - 1)


def decode_mi(blob: bytes, layout: dict) -> dict[str, Any]:
    b = bytes(blob)
    if len(b) < 8:
        raise ValueError(f"MI message too short ({len(b)} bytes)")
    nmimt = _bits(b, layout["nmimt"])
    kind = layout["nmimt_map"].get(str(nmimt))
    extra = {"nmimt": nmimt, "ror": _bits(b, layout["ror"]), "csi": _bits(b, layout["csi"]), "msg_len": len(b)}
    if kind == "mi":
        L = layout["mi"]
        opcode = b[L["opcode_byte"]]
        dws: dict[str, int] = {}
        for name, off in (("nmd0", L["nmd0_off"]), ("nmd1", L["nmd1_off"])):
            if len(b) >= off + 4:
                dws[name] = struct.unpack_from("<I", b, off)[0]
        return {"interface": "mi", "queue": "mi", "opcode": opcode, "nsid": 0, "dwords": dws, "extra": extra}
    if kind == "admin":
        L = layout["admin"]
        opcode = b[L["opcode_byte"]]
        dws = {"0": opcode}
        for idx, off in L["dwords"].items():
            if len(b) >= off + 4:
                dws[idx] = struct.unpack_from("<I", b, off)[0]
        extra.update({"cflgs": b[L["flags_byte"]], "ctlid": struct.unpack_from("<H", b, L["ctlid_off"])[0]})
        return {"interface": "mi", "queue": "admin", "opcode": opcode, "nsid": dws.get("1", 0), "dwords": dws, "extra": extra}
    raise ValueError(f"unsupported NMIMT {nmimt} (not in nmimt_map)")


def signature(interface: str, queue: str, cs: str, opcode: int, dwords: dict[str, int]) -> str:
    items = []
    for k in sorted(dwords, key=lambda x: (len(x), x)):
        if interface == "pcie" and k in _SIG_SKIP:
            continue
        v = dwords[k]
        if interface == "pcie" and k in _SIG_MASK:
            v &= _SIG_MASK[k]
        items.append((k, v))
    raw = json.dumps([interface, queue, cs, opcode, items], separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


# ----------------------------------------------------------------------------- mapping

KNOWN = {
    "id": ["id", "rowid", "rowid_", "cmd_id", "row_id"],
    "test": ["test_name", "testcase", "test", "test_id", "testname", "case_name"],
    "script": ["script", "script_name", "file"],
    "seq": ["seq", "step", "sequence", "idx", "index", "order_no", "cmd_no"],
    "ts": ["ts", "timestamp", "time"],
    "interface": ["interface", "iface", "transport"],
    "queue": ["queue", "queue_type", "q"],
    "sqe": ["sqe", "sq_entry", "cmd_bytes", "command", "raw_sqe", "submission_entry"],
    "mi_msg": ["mi_msg", "mi_message", "mi_raw", "mi_payload", "mctp_payload"],
    "cs": ["cs", "csi", "command_set"],
}


def _q(name: str) -> str:
    """Quote an SQL identifier (embedded double quotes are doubled)."""
    return '"' + str(name).replace('"', '""') + '"'


def _open_ro(path: str) -> sqlite3.Connection:
    """Open read-only through a properly escaped file URI (paths may contain '?', '#', '%')."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def list_tables(path: str) -> dict[str, list[str]]:
    con = _open_ro(path)
    try:
        out = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"):
            out[name] = [r[1] for r in con.execute(f"PRAGMA table_info({_q(name)})")]
        return out
    finally:
        con.close()


# Command-history databases written by the test framework: one file per test script, NVMe commands in one table, NVMe-MI
# commands in another. Values are hex text ("0x6"); the SQE is stored field by field (OP, NS, FUSE, ...) rather than as dword 0/1.
HISTORY_TABLES = {"nvme": "commandhistorytable", "mi": "micommandhistorytable"}   # kind -> table name (lower case)
KINDS = ("nvme", "mi")
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", ".db3")
FROM_FILE = {"from": "file"}       # mapping value: the test name is the database's path (one database per test script)


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def discover(path: str | Path) -> list[Path]:
    """The databases of a source: the file itself, or every SQLite database under a folder (sub-folders included, hidden ones
    skipped), in path order."""
    p = Path(path)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise FileNotFoundError(str(path))
    found = []
    for f in p.rglob("*"):
        rel = f.relative_to(p)
        if f.suffix.lower() in DB_SUFFIXES and not any(x.startswith(".") for x in rel.parts) and f.is_file() and is_sqlite(f):
            found.append(f)
    return sorted(found, key=lambda f: f.relative_to(p).as_posix().lower())


def test_name(root: str | Path, f: Path) -> str:
    """A database's test name: its path inside the folder without the extension (for a single file, folder/file)."""
    root = Path(root)
    if root.is_dir():
        return f.relative_to(root).with_suffix("").as_posix()
    return f"{f.parent.name}/{f.stem}" if f.parent.name else f.stem


def _rel(root: Path, f: Path) -> str:
    return f.relative_to(root).as_posix() if root.is_dir() else f.name


def source_sha256(path: str | Path) -> str:
    root = Path(path)
    if root.is_file():
        return sha256_file(root)
    return stable_hash([(_rel(root, f), sha256_file(f)) for f in discover(root)])


def _history_tables(tables: dict[str, list[str]]) -> dict[str, str]:
    """kind -> actual table name, for the command-history tables present in this database."""
    low = {t.lower(): t for t in tables}
    return {k: low[t] for k, t in HISTORY_TABLES.items() if t in low}


def _history_mapping(kind: str, table: str, cols: list[str]) -> dict[str, Any]:
    low = {c.lower(): c for c in cols}

    def pick(names: dict[str, str]) -> dict[str, str]:
        return {k: low[c] for k, c in names.items() if c in low}

    m: dict[str, Any] = {"table": table, "format": "command_history", "kind": kind, "test": dict(FROM_FILE)}   # no test column
    if "cmdtype" in low:
        m["queue"] = low["cmdtype"]
    if kind == "nvme":
        if "sn" in low:
            m["seq"] = low["sn"]
        m.update({"interface": {"const": "pcie"}, "cs": {"const": "nvm"},
                  "fields": pick({"opcode": "op", "nsid": "ns", "fuse": "fuse", "psdt": "psdt", "cid": "cmdid"}),
                  "cdw": pick({str(i): f"dword{i:02d}" for i in (2, 3, 10, 11, 12, 13, 14, 15)})})
        if "queue" not in m:
            m["queue"] = {"const": "admin"}
    else:
        m.update({"interface": {"const": "mi"}, "mi_fields": pick({"opcode": "op", "nmd0": "dword0", "nmd1": "dword1"})})
    return m


def _count(path: Path, table: str) -> int:
    con = _open_ro(str(path))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0]
    finally:
        con.close()


def scan(path: str, kind: str | None = None, limit: int = 5000) -> dict[str, Any]:
    """Every database of a file or folder with its test name and how many NVMe / NVMe-MI commands it holds, plus the mapping
    that would be used for them (see suggest_mapping)."""
    root = Path(path)
    files = discover(root)
    dbs = []
    for f in files[:limit]:
        e: dict[str, Any] = {"path": str(f), "rel": _rel(root, f), "test": test_name(root, f), "size": f.stat().st_size}
        try:
            tables = list_tables(str(f))
            hist = _history_tables(tables)
            if hist:
                e["commands"] = {k: _count(f, t) for k, t in hist.items()}
            else:
                e["tables"] = sorted(tables)
        except sqlite3.DatabaseError as ex:
            e["error"] = str(ex)
        dbs.append(e)
    out: dict[str, Any] = {"path": str(root), "folder": root.is_dir(), "count": len(files), "databases": dbs, "truncated": len(files) > limit}
    try:
        out.update(suggest_mapping(path, kind))
    except ValueError as ex:
        out["error"] = str(ex)
    return out


def suggest_mapping(path: str, kind: str | None = None) -> dict[str, Any]:
    """Guess a column mapping for a database or a folder of databases (read from the first one that has the commands).

    `kind` ("nvme" | "mi") picks which commands to read from command-history databases."""
    if kind is not None and kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    root = Path(path)
    files = discover(root)
    if not files:
        raise ValueError("No SQLite databases found in this folder")
    first = files[0]
    tables = list_tables(str(first))
    hist = _history_tables(tables)
    if root.is_dir() and hist:                 # the first database may predate MI logging: use one that has the requested table
        want = HISTORY_TABLES[kind or "nvme"]
        for f in files:
            t = list_tables(str(f))
            if want in {x.lower() for x in t}:
                first, tables, hist = f, t, _history_tables(t)
                break
    if hist:
        kind = kind or ("nvme" if "nvme" in hist else "mi")
        if kind not in hist:
            raise ValueError(f"This command-history database has no {'MICommandHistoryTable' if kind == 'mi' else 'CommandHistoryTable'} table")
        table = hist[kind]
        m = _history_mapping(kind, table, tables[table])
        warnings = []
        need = ("opcode",) if kind == "mi" else ("opcode", "nsid")
        missing = [f for f in need if f not in m.get("fields" if kind == "nvme" else "mi_fields", {})]
        if missing:
            warnings.append(f"{table}: no column found for {', '.join(missing)}; set the mapping manually.")
        if not root.is_dir() and _count(first, table) == 0:
            warnings.append(f"{table} is empty: this run recorded no {'NVMe-MI' if kind == 'mi' else 'NVMe'} commands.")
        return {"mapping": m, "tables": tables, "warnings": warnings, "format": "command_history", "kinds": sorted(hist)}
    out = _suggest_generic(tables)
    if root.is_dir() and "test" not in out["mapping"]:
        out["mapping"]["test"] = dict(FROM_FILE)
    if kind is not None:
        out["warnings"].append("Not a command-history database: the NVMe / NVMe-MI choice does not apply; the mapping was guessed from the schema.")
    return out


def _suggest_generic(tables: dict[str, list[str]]) -> dict[str, Any]:
    best, best_score = None, -1
    for t, cols in tables.items():
        low = {c.lower(): c for c in cols}
        score = sum(1 for k, names in KNOWN.items() if any(n in low for n in names))
        score += 3 * sum(1 for i in range(16) if f"cdw{i}" in low)
        if score > best_score:
            best, best_score = t, score
    if best is None:
        raise ValueError("No tables found in the command database")
    cols = tables[best]
    low = {c.lower(): c for c in cols}
    m: dict[str, Any] = {"table": best}
    for k, names in KNOWN.items():
        for n in names:
            if n in low:
                m[k] = low[n]
                break
    cdw = {str(i): low[f"cdw{i}"] for i in range(16) if f"cdw{i}" in low}
    if len(cdw) >= 11 and "0" in cdw:
        m["cdw"] = cdw
    warnings = []
    if cdw and "0" not in cdw:
        warnings.append("CDW columns found but cdw0 (opcode) is missing; map cdw0 or provide SQE bytes.")
    elif "sqe" not in m and "cdw" not in m and "mi_msg" not in m:
        warnings.append("No SQE blob, CDW columns or MI message column recognised; set the mapping manually.")
    if "interface" not in m:
        m["interface"] = {"const": "mi" if "mi_msg" in m and "sqe" not in m and "cdw" not in m else "pcie"}
    if "queue" not in m:
        m["queue"] = {"const": "admin"}
        warnings.append("No queue column; every command is treated as an Admin command. Map a queue column or set a constant.")
    if "cs" not in m:
        m["cs"] = {"const": "nvm"}
    return {"mapping": m, "tables": tables, "warnings": warnings}


def _col(mapping: dict, key: str):
    v = mapping.get(key)
    if v is None:
        return None
    return v


def _value(row_: sqlite3.Row, spec: Any, default=None):
    if spec is None:
        return default
    if isinstance(spec, dict):
        return spec.get("const", default)
    return row_[spec] if spec in row_.keys() else default


DEFAULT_CSI_NAMES = {"0": "nvm", "1": "kv", "2": "zns"}  # Command Set Identifier values; extend via mapping["cs_map"]


def _norm_cs(v: Any, mapping: dict) -> str:
    s = str(v if v is not None else "nvm").strip().lower()
    names = {**DEFAULT_CSI_NAMES, **{str(k): str(x).lower() for k, x in (mapping.get("cs_map") or {}).items()}}
    if s.startswith("0x"):
        try:
            s = str(int(s, 16))
        except ValueError:
            pass
    return names.get(s, s) or "nvm"


def _meta(r: sqlite3.Row, spec: Any, default: Any) -> Any:
    """Resolve a mapping entry for bookkeeping columns: a column name, a {"const": ...}, or the default when absent/NULL."""
    if spec is None:
        return default
    if isinstance(spec, dict):
        return spec.get("const", default)
    return r[spec] if spec in r.keys() and r[spec] is not None else default


def _norm_queue(q: Any) -> str:
    s = str(q or "").strip().lower()
    if s in ("admin", "adm", "0", "asq", "admin_queue"):
        return "admin"
    if s in ("io", "i/o", "nvm", "1", "iosq", "io_queue"):
        return "io"
    return s or "admin"


def _norm_iface(i: Any) -> str:
    s = str(i or "pcie").strip().lower()
    if s in ("mi", "nvme-mi", "nvmemi", "mctp", "smbus", "i2c"):
        return "mi"
    return "pcie"


def iter_source_rows(path: str, mapping: dict) -> Iterator[sqlite3.Row]:
    con = _open_ro(path)
    con.row_factory = sqlite3.Row
    try:
        cols = [c for c in (mapping.get("seq"), mapping.get("id")) if isinstance(c, str)]       # constants cannot be ordered by
        order = cols[0] if cols else None
        test = mapping.get("test") if isinstance(mapping.get("test"), str) else None
        sql = f'SELECT * FROM {_q(mapping["table"])}'
        if order:
            sql += f' ORDER BY {_q(test)}, {_q(order)}' if test else f' ORDER BY {_q(order)}'
        cur = con.execute(sql)
        while True:
            chunk = cur.fetchmany(20000)
            if not chunk:
                break
            yield from chunk
    finally:
        con.close()


def _int(v: Any) -> int | None:
    """An integer column value or text in decimal / hex ("0x3ff0005", "06h"); None for NULL or empty text."""
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = (v.decode(errors="replace") if isinstance(v, bytes) else str(v)).strip().lower()
    if not s:
        return None
    try:
        if s.startswith("0x"):
            return int(s, 16)
        if s.endswith("h"):
            return int(s[:-1], 16)
        return int(s)
    except ValueError:
        raise ValueError(f"not a number: {v!r}") from None


def _field(r: sqlite3.Row, col: str | None) -> int | None:
    return _int(r[col]) if col and col in r.keys() else None


def _norm_mi_queue(q: Any) -> str:
    """Commands in an MI command table are NVMe-MI commands unless the row says it is a tunnelled Admin command."""
    s = str(q or "").strip().lower()
    return "admin" if s in ("admin", "adm", "nvme_admin", "nvme-admin", "mi_admin", "mi-admin", "2") else "mi"


def _mi_from_fields(r: sqlite3.Row, mapping: dict) -> dict[str, Any]:
    f = mapping["mi_fields"]
    opcode = _field(r, f.get("opcode"))
    if opcode is None:
        raise ValueError("opcode is NULL")
    queue = _norm_mi_queue(_value(r, _col(mapping, "queue"), "mi"))
    if queue == "admin":                                   # only the opcode of a tunnelled Admin command is recorded here
        return {"interface": "mi", "queue": "admin", "opcode": opcode & 0xFF, "nsid": 0, "dwords": {"0": opcode & 0xFF}, "extra": {}}
    dws = {k: v & 0xFFFFFFFF for k in ("nmd0", "nmd1") if (v := _field(r, f.get(k))) is not None}
    return {"interface": "mi", "queue": "mi", "opcode": opcode & 0xFF, "nsid": 0, "dwords": dws, "extra": {}}


def _dwords_from_columns(r: sqlite3.Row, mapping: dict) -> dict[str, int]:
    # SQL NULL means "not recorded": it is omitted (never turned into 0, which would satisfy zero-valued scenarios)
    dw = {k: v & 0xFFFFFFFF for k, c in (mapping.get("cdw") or {}).items() if (v := _field(r, c)) is not None}
    f = mapping.get("fields") or {}
    if "0" not in dw and f.get("opcode"):                  # dword 0 recorded field by field: OPC, FUSE, PSDT, CID
        opcode = _field(r, f["opcode"])
        if opcode is not None:
            fuse, psdt, cid = (_field(r, f.get(k)) or 0 for k in ("fuse", "psdt", "cid"))
            dw["0"] = (opcode & 0xFF) | (fuse & 3) << 8 | (psdt & 3) << 14 | (cid & 0xFFFF) << 16
    if "1" not in dw and f.get("nsid") and (nsid := _field(r, f["nsid"])) is not None:
        dw["1"] = nsid & 0xFFFFFFFF
    return dw


def decode_row(r: sqlite3.Row, mapping: dict, layout: dict) -> dict[str, Any]:
    iface = _norm_iface(_value(r, _col(mapping, "interface"), "pcie"))
    cs = _norm_cs(_value(r, _col(mapping, "cs"), "nvm"), mapping)
    mi_col = _col(mapping, "mi_msg")
    sqe_col = _col(mapping, "sqe")
    if iface == "mi":
        blob = r[mi_col] if mi_col and mi_col in r.keys() else None
        if blob is not None:
            d = decode_mi(blob, layout)
        elif mapping.get("mi_fields"):
            d = _mi_from_fields(r, mapping)
        else:
            raise ValueError("interface=mi but no MI message bytes")
        cs = "mi" if d["queue"] == "mi" else "base"
    else:
        if sqe_col and r[sqe_col] is not None:
            d = decode_sqe(r[sqe_col])
        elif mapping.get("cdw") or mapping.get("fields"):
            dw = _dwords_from_columns(r, mapping)
            if "0" not in dw:
                raise ValueError("cdw0 (opcode) is NULL")
            d = _from_dwords(dw)
        else:
            raise ValueError("no SQE blob or CDW columns mapped")
        d["queue"] = _norm_queue(_value(r, _col(mapping, "queue"), "admin"))
        if d["queue"] == "admin":
            cs = "base"
    d["cs"] = cs
    d["signature"] = signature(d["interface"], d["queue"], d["cs"], d["opcode"], d["dwords"])
    return d


def register_source(conn: sqlite3.Connection, path: str, mapping: dict | None = None, name: str | None = None,
                    kind: str | None = None) -> int:
    """`path` is one database or a folder of databases (one per test script; sub-folders included)."""
    if not Path(path).exists():
        raise FileNotFoundError(path)
    mapping = mapping or suggest_mapping(path, kind)["mapping"]
    if not name:
        name = Path(path).name + ("/" if Path(path).is_dir() else "")
        if mapping.get("format") == "command_history":
            name += f" ({'NVMe-MI' if mapping.get('kind') == 'mi' else 'NVMe'})"
    cur = conn.execute("INSERT INTO cmd_source(name,path,sha256,mapping_json,status) VALUES(?,?,?,?, 'new')",
                       (name, str(path), source_sha256(path), json.dumps(mapping)))
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def import_source(conn: sqlite3.Connection, source_id: int, mi_layout: dict | None = None, progress=None) -> dict[str, Any]:
    src = row(conn, "SELECT * FROM cmd_source WHERE id=?", (source_id,))
    if not src:
        raise KeyError(source_id)
    mapping = json.loads(src["mapping_json"])
    layout = merged_layout(mi_layout)
    root = Path(src["path"])
    files = discover(root)               # a folder is scanned again: databases added since the last import are picked up
    eventlog.emit("info", "import", f"Importing '{src['name'] or src['path']}' (table {mapping.get('table')}, {len(files)} database(s))",
                  source_id=source_id, mapping=mapping, mi_layout_overrides=mi_layout or {})
    # the files may have changed since they were registered: record the hash of the bytes actually imported
    conn.execute("UPDATE cmd_source SET sha256=? WHERE id=?", (source_sha256(root), source_id))
    conn.execute("DELETE FROM decoded_command WHERE source_id=?", (source_id,))
    conn.execute("DELETE FROM cmd_signature WHERE source_id=?", (source_id,))
    sigs: dict[str, dict] = {}
    batch: list[tuple] = []
    total = bad = 0
    errors: list[dict] = []
    per_file: list[dict] = []
    id_col, test_col, script_col, seq_col = mapping.get("id"), mapping.get("test"), mapping.get("script"), mapping.get("seq")
    table = str(mapping.get("table", "")).lower()
    for f in files:
        rel, ftest = _rel(root, f), test_name(root, f)
        fs: dict[str, Any] = {"rel": rel, "test": ftest, "rows": 0, "decoded": 0}
        per_file.append(fs)
        try:
            if table not in {t.lower() for t in list_tables(str(f))}:
                fs["error"] = f"no table {mapping.get('table')}"
                continue
            n = 0
            for r in iter_source_rows(str(f), mapping):
                n += 1
                total += 1
                fs["rows"] += 1
                if progress and total % PROGRESS_EVERY == 0:      # polled on rows SCANNED (decode failures included), so cancellation is honoured
                    progress(total)
                try:
                    d = decode_row(r, mapping, layout)
                    seq = _int(_meta(r, seq_col, n))
                    seq = n if seq is None else seq
                except Exception as e:  # noqa: BLE001
                    bad += 1
                    if len(errors) < 25:
                        errors.append({"row": _meta(r, id_col, n), "error": str(e), **({"file": rel} if root.is_dir() else {})})
                    continue
                fs["decoded"] += 1
                s = sigs.get(d["signature"])
                if s is None:
                    s = sigs[d["signature"]] = {"interface": d["interface"], "queue": d["queue"], "cs": d["cs"], "opcode": d["opcode"],
                                                "nsid": d["nsid"], "dwords": d["dwords"], "count": 0}
                s["count"] += 1
                batch.append((source_id, _meta(r, id_col, n), str(_meta(r, test_col, ftest if test_col == FROM_FILE or root.is_dir() else "")),
                              str(_meta(r, script_col, rel if root.is_dir() else "")), seq,
                              d["interface"], d["queue"], d["cs"], d["opcode"], d["nsid"], d["signature"]))
                if len(batch) >= 20000:
                    _flush(conn, batch)
                    batch = []
        except sqlite3.DatabaseError as e:                  # a corrupt or locked database is reported, the others are still imported
            fs["error"] = str(e)
    if progress:
        progress(total)                                    # last chance to cancel before the import is finalised
    _flush(conn, batch)
    conn.executemany("INSERT INTO cmd_signature(source_id,signature,interface,queue,cs,opcode,nsid,dwords_json,count) VALUES(?,?,?,?,?,?,?,?,?)",
                     [(source_id, k, v["interface"], v["queue"], v["cs"], v["opcode"], v["nsid"], json.dumps(v["dwords"]), v["count"])
                      for k, v in sigs.items()])
    failed = [x for x in per_file if "error" in x]
    unknown = {"undecodable_rows": bad, "errors": errors, "files": per_file, "failed_files": len(failed)}
    conn.execute("UPDATE cmd_source SET row_count=?, decoded_count=?, unknown_json=?, imported_at=CURRENT_TIMESTAMP, status='imported' WHERE id=?",
                 (total, total - bad, json.dumps(unknown), source_id))
    conn.commit()
    eventlog.emit("warn" if bad or failed else "info", "import",
                  f"Import done: {len(files)} database(s), {total} rows scanned, {total - bad} decoded, {bad} undecodable, {len(sigs)} distinct command signatures"
                  + (f"; {len(failed)} database(s) could not be read" if failed else ""),
                  source_id=source_id, databases=len(files), rows=total, decoded=total - bad, undecodable=bad, signatures=len(sigs))
    for e_ in errors[:5]:
        eventlog.emit("warn", "import", f"Row {e_['row']}{' of ' + e_['file'] if 'file' in e_ else ''} could not be decoded: {e_['error']}", source_id=source_id)
    for x in failed[:5]:
        eventlog.emit("warn", "import", f"{x['rel']} skipped: {x['error']}", source_id=source_id)
    return {"rows": total, "decoded": total - bad, "undecodable": bad, "signatures": len(sigs), "errors": errors,
            "databases": len(files), "failed_databases": len(failed), "config_hash": stable_hash([mapping, layout])}


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    if batch:
        conn.executemany("INSERT INTO decoded_command(source_id,src_row_id,test_id,script,seq,interface,queue,cs,opcode,nsid,signature)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
