"""L2: resolve section/figure/spec references inside a spec set (intra- and cross-spec)."""
from __future__ import annotations

import sqlite3
from typing import Any

from .core.db import rows
from .ingest.store import load_ir

STATES = ("resolved", "ambiguous", "external-not-loaded", "broken")


def set_members(conn: sqlite3.Connection, set_id: int) -> list[dict]:
    """Members with their active parse run."""
    out = rows(conn, """SELECT d.id AS document_id, d.family, d.version, d.filename, r.id AS run_id
                        FROM spec_set_member m JOIN spec_document d ON d.id=m.document_id
                        LEFT JOIN parse_run r ON r.document_id=d.id AND r.active=1
                        WHERE m.set_id=? ORDER BY d.family""", (set_id,))
    return out


def load_set_irs(conn: sqlite3.Connection, set_id: int) -> list[dict]:
    irs = []
    for m in set_members(conn, set_id):
        if m["run_id"] is None:
            raise ValueError(f"{m['family']} {m['version']} has no active parse run; parse it first")
        irs.append(load_ir(conn, m["run_id"]))
    return irs


def _exists(ir: dict, kind: str, label: str) -> str | None:
    if kind == "figure":
        t = ir["tables"].get(label)
        return t["key"] if t else None
    if kind == "section":
        s = ir["sections"].get(label)
        return s["key"] if s else None
    return None


def resolve_references(conn: sqlite3.Connection, set_id: int, irs: list[dict] | None = None) -> list[dict[str, Any]]:
    irs = irs if irs is not None else load_set_irs(conn, set_id)
    by_family: dict[str, list[dict]] = {}
    for ir in irs:
        by_family.setdefault(ir["family"], []).append(ir)
    choices = {(r["src_key"], r["raw"]): r["chosen_key"] for r in rows(
        conn, "SELECT * FROM ref_choice WHERE set_id=?", (set_id,))}
    out: list[dict[str, Any]] = []
    for ir in irs:
        for r in ir["refs"]:
            base = {"src_key": r["src_key"], "raw": r["raw"], "kind": r["kind"], "target_family": r["target_family"],
                    "target_label": r["target_label"], "from_family": ir["family"], "from_version": ir["version"]}
            fam = r["target_family"]
            if fam is None:
                key = _exists(ir, r["kind"], r["target_label"])
                out.append({**base, "state": "resolved" if key else "broken", "target_key": key, "candidates": [key] if key else []})
                continue
            targets = by_family.get(fam, [])
            if not targets:
                out.append({**base, "state": "external-not-loaded", "target_key": None, "candidates": []})
                continue
            if r["kind"] == "spec":
                out.append({**base, "state": "resolved", "target_key": f"{targets[0]['prefix']}", "candidates": []})
                continue
            cands = [k for k in (_exists(t, r["kind"], r["target_label"]) for t in targets) if k]
            chosen = choices.get((r["src_key"], r["raw"]))
            if chosen and chosen in cands:
                out.append({**base, "state": "resolved", "target_key": chosen, "candidates": cands, "chosen": True})
            elif len(cands) == 1:
                out.append({**base, "state": "resolved", "target_key": cands[0], "candidates": cands})
            elif len(cands) > 1:
                out.append({**base, "state": "ambiguous", "target_key": None, "candidates": cands})
            else:
                out.append({**base, "state": "broken", "target_key": None, "candidates": []})
    return out


def summarize(refs: list[dict]) -> dict[str, Any]:
    counts = {s: 0 for s in STATES}
    cross = 0
    for r in refs:
        counts[r["state"]] += 1
        if r["target_family"]:
            cross += 1
    return {"total": len(refs), "cross_spec": cross, **counts}


def choose_reference(conn: sqlite3.Connection, set_id: int, src_key: str, raw: str, chosen_key: str) -> None:
    conn.execute("INSERT INTO ref_choice(set_id,src_key,raw,chosen_key) VALUES(?,?,?,?) "
                 "ON CONFLICT(set_id,src_key,raw) DO UPDATE SET chosen_key=excluded.chosen_key",
                 (set_id, src_key, raw, chosen_key))
    conn.commit()
