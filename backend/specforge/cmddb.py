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


def suggest_mapping(path: str) -> dict[str, Any]:
    tables = list_tables(path)
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


def decode_row(r: sqlite3.Row, mapping: dict, layout: dict) -> dict[str, Any]:
    iface = _norm_iface(_value(r, _col(mapping, "interface"), "pcie"))
    cs = _norm_cs(_value(r, _col(mapping, "cs"), "nvm"), mapping)
    mi_col = _col(mapping, "mi_msg")
    sqe_col = _col(mapping, "sqe")
    cdw = mapping.get("cdw")
    if iface == "mi":
        blob = r[mi_col] if mi_col and mi_col in r.keys() else None
        if blob is None:
            raise ValueError("interface=mi but no MI message bytes")
        d = decode_mi(blob, layout)
        cs = "mi" if d["queue"] == "mi" else "base"
    else:
        if sqe_col and r[sqe_col] is not None:
            d = decode_sqe(r[sqe_col])
        elif cdw:
            # SQL NULL means "not recorded": it is omitted (never turned into 0, which would satisfy zero-valued scenarios)
            dw = {k: int(r[c]) & 0xFFFFFFFF for k, c in cdw.items() if r[c] is not None}
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


def register_source(conn: sqlite3.Connection, path: str, mapping: dict | None = None, name: str | None = None) -> int:
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    mapping = mapping or suggest_mapping(path)["mapping"]
    cur = conn.execute("INSERT INTO cmd_source(name,path,sha256,mapping_json,status) VALUES(?,?,?,?, 'new')",
                       (name or Path(path).name, str(path), sha256_file(path), json.dumps(mapping)))
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def import_source(conn: sqlite3.Connection, source_id: int, mi_layout: dict | None = None, progress=None) -> dict[str, Any]:
    src = row(conn, "SELECT * FROM cmd_source WHERE id=?", (source_id,))
    if not src:
        raise KeyError(source_id)
    mapping = json.loads(src["mapping_json"])
    layout = merged_layout(mi_layout)
    eventlog.emit("info", "import", f"Importing '{src['name'] or src['path']}' (table {mapping.get('table')})", source_id=source_id, mapping=mapping,
                  mi_layout_overrides=mi_layout or {})
    # the file may have changed since it was registered: record the hash of the bytes actually imported
    conn.execute("UPDATE cmd_source SET sha256=? WHERE id=?", (sha256_file(src["path"]), source_id))
    conn.execute("DELETE FROM decoded_command WHERE source_id=?", (source_id,))
    conn.execute("DELETE FROM cmd_signature WHERE source_id=?", (source_id,))
    sigs: dict[str, dict] = {}
    batch: list[tuple] = []
    total = bad = 0
    errors: list[dict] = []
    id_col, test_col, script_col, seq_col = mapping.get("id"), mapping.get("test"), mapping.get("script"), mapping.get("seq")
    for r in iter_source_rows(src["path"], mapping):
        total += 1
        if progress and total % PROGRESS_EVERY == 0:      # polled on rows SCANNED (decode failures included), so cancellation is honoured
            progress(total)
        try:
            d = decode_row(r, mapping, layout)
            seq = int(_meta(r, seq_col, total))
        except Exception as e:  # noqa: BLE001
            bad += 1
            if len(errors) < 25:
                errors.append({"row": _meta(r, id_col, total), "error": str(e)})
            continue
        s = sigs.get(d["signature"])
        if s is None:
            s = sigs[d["signature"]] = {"interface": d["interface"], "queue": d["queue"], "cs": d["cs"], "opcode": d["opcode"],
                                        "nsid": d["nsid"], "dwords": d["dwords"], "count": 0}
        s["count"] += 1
        batch.append((source_id, _meta(r, id_col, total), str(_meta(r, test_col, "")), str(_meta(r, script_col, "")),
                      seq,
                      d["interface"], d["queue"], d["cs"], d["opcode"], d["nsid"], d["signature"]))
        if len(batch) >= 20000:
            _flush(conn, batch)
            batch = []
    if progress:
        progress(total)                                    # last chance to cancel before the import is finalised
    _flush(conn, batch)
    conn.executemany("INSERT INTO cmd_signature(source_id,signature,interface,queue,cs,opcode,nsid,dwords_json,count) VALUES(?,?,?,?,?,?,?,?,?)",
                     [(source_id, k, v["interface"], v["queue"], v["cs"], v["opcode"], v["nsid"], json.dumps(v["dwords"]), v["count"])
                      for k, v in sigs.items()])
    unknown = {"undecodable_rows": bad, "errors": errors}
    conn.execute("UPDATE cmd_source SET row_count=?, decoded_count=?, unknown_json=?, imported_at=CURRENT_TIMESTAMP, status='imported' WHERE id=?",
                 (total, total - bad, json.dumps(unknown), source_id))
    conn.commit()
    eventlog.emit("warn" if bad else "info", "import", f"Import done: {total} rows scanned, {total - bad} decoded, {bad} undecodable, {len(sigs)} distinct command signatures",
                  source_id=source_id, rows=total, decoded=total - bad, undecodable=bad, signatures=len(sigs))
    for e_ in errors[:5]:
        eventlog.emit("warn", "import", f"Row {e_['row']} could not be decoded: {e_['error']}", source_id=source_id)
    return {"rows": total, "decoded": total - bad, "undecodable": bad, "signatures": len(sigs), "errors": errors,
            "config_hash": stable_hash([mapping, layout])}


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    if batch:
        conn.executemany("INSERT INTO decoded_command(source_id,src_row_id,test_id,script,seq,interface,queue,cs,opcode,nsid,signature)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
