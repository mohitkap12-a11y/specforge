"""AI enrichment: requirement -> grounded, validated draft scenarios.

The model never invents opcodes or bit positions: it may only reference catalog command keys and bit
ranges that exist in the catalog; opcode/queue/cs are filled in from the catalog.  Every output is
  1. schema-validated (pydantic),
  2. grounded (each quote must occur verbatim in the supplied context),
  3. predicate-validated against the catalog,
  4. de-duplicated against existing scenarios.
Anything that fails stays out of coverage and is recorded in `enrichment` as needs_review/failed.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, Field

from .core.db import jloads, rows
from .ingest.normalize import norm_title
from .llm.base import LLMError
from .llm.service import LLMService
from .scenarios import catalog_context, validate_predicate

PROMPT_VERSION = "ai-scenarios-v1"


class AIField(BaseModel):
    dw: int | Literal["nmd0", "nmd1"]
    hi: int
    lo: int
    op: Literal["eq", "ne", "in", "range", "mask"] = "eq"
    value: Any


class AICapture(BaseModel):
    dw: int | Literal["nmd0", "nmd1"]
    hi: int
    lo: int


class AIStep(BaseModel):
    command: str = Field(description="catalog command key, e.g. cmd:admin:06")
    fields: list[AIField] = []
    capture: dict[str, AICapture] = {}


class AIScenario(BaseModel):
    title: str
    description: str = ""
    observability: Literal["submission", "stimulus"]
    steps: list[AIStep]
    order: Literal["strict", "any"] = "strict"
    scope: Literal["global", "test", "script"] = "test"
    within: int | None = None
    quotes: list[str] = Field(description="verbatim excerpts of the requirement that justify the scenario")


class AIOutput(BaseModel):
    testable: bool
    reason: str = ""
    scenarios: list[AIScenario] = []


SYSTEM = """You turn ONE normative statement from an NVMe specification into test scenarios that can be checked
against a log of SUBMITTED commands (submission queue entries / NVMe-MI requests). No completions are logged.

Rules:
- Only use commands and bit ranges listed under CATALOG. Never invent opcodes, commands or bit positions.
- observability 'submission' = the scenario is fully visible in what was submitted; 'stimulus' = only the
  triggering submission is visible (the controller's response cannot be verified).
- If the statement describes controller-internal behaviour, timing, or anything not visible in a submission,
  reply testable=false with a reason and no scenarios.
- Each scenario needs 1+ quotes copied EXACTLY (character for character) from the REQUIREMENT or CONTEXT text.
- Field values are integers (decimal). Use 'eq','ne','in' (list),'range' ({"lo","hi"}) or 'mask' ({"mask","value"}).
- Use multiple steps only when the statement requires an ordering of several commands.
Reply with one JSON object only."""


def build_context(conn: sqlite3.Connection, set_id: int, item: dict) -> dict[str, Any]:
    a = item["attrs"]
    cmd_key = a.get("command")
    catalog: list[dict] = []
    if cmd_key:
        for r in rows(conn, "SELECT key,kind,label,attrs_json FROM catalog_item WHERE set_id=? AND (key=? OR parent_key=? OR "
                            "parent_key IN (SELECT key FROM catalog_item WHERE set_id=? AND parent_key=?))",
                      (set_id, cmd_key, cmd_key, set_id, cmd_key)):
            x = jloads(r["attrs_json"], {})
            if r["kind"] == "command":
                catalog.append({"command": r["key"], "name": x["name"], "interface": x["interface"], "queue": x["queue"]})
            elif r["kind"] in ("field", "reserved"):
                catalog.append({"command": cmd_key, "field": r["label"], "dw": x["dw"], "hi": x["hi"], "lo": x["lo"],
                                "reserved": r["kind"] == "reserved"})
            elif r["kind"] == "value" and not x.get("reserved_value"):
                catalog.append({"command": cmd_key, "value_of": x["field"], "value": x["vlo"] if x["vlo"] == x["vhi"] else [x["vlo"], x["vhi"]],
                                "meaning": x.get("desc", "")[:60]})
    ctx_text = a["text"]
    # neighbouring sentences from the same section (grounding corpus)
    section_text = _section_text(conn, set_id, item["key"])
    return {"requirement": a["text"], "catalog": catalog, "command": cmd_key, "context_text": ctx_text + (("\n" + section_text) if section_text else "")}


def _section_text(conn: sqlite3.Connection, set_id: int, req_key: str) -> str:
    """Text of the section holding a requirement, looked up only in the set's ACTIVE parse runs."""
    parent = req_key.rsplit("/req:", 1)[0]
    runs = "JOIN parse_run r ON r.id=n.run_id AND r.active=1 JOIN spec_set_member m ON m.document_id=r.document_id AND m.set_id=?"
    if "/fig:" in parent:  # requirement inside a table: use the section that contains the table
        t = conn.execute(f"SELECT n.attrs_json FROM ir_node n {runs} WHERE n.key=?", (set_id, parent)).fetchone()
        sec = jloads(t[0], {}).get("section") if t else None
        if not sec:
            return ""
        parent = parent.split("/fig:", 1)[0] + f"/sec:{sec}"
    r = conn.execute(f"SELECT n.text FROM ir_node n {runs} WHERE n.key=?", (set_id, parent)).fetchone()
    return r[0] or "" if r else ""


def _grounded(quote: str, corpus: str) -> bool:
    q = norm_title(quote)
    return len(q) >= 8 and q in norm_title(corpus)


def to_predicate(sc: AIScenario, cmds: dict[str, dict]) -> dict:
    steps = []
    for st in sc.steps:
        c = cmds.get(st.command)
        if c is None:
            raise ValueError(f"unknown command {st.command}")
        steps.append({"command": st.command, "interface": c["interface"], "queue": c["queue"],
                      "cs": c["cs"] if c["queue"] == "io" else None, "opcode": c["opcode"],
                      "fields": [f.model_dump() for f in st.fields],
                      **({"capture": {k: v.model_dump() for k, v in st.capture.items()}} if st.capture else {})})
    pred: dict[str, Any] = {"scope": sc.scope, "order": sc.order, "steps": steps}
    if sc.within is not None:   # 0 must reach validation (it is invalid), not silently mean 'unlimited'
        pred["within"] = sc.within
    return pred


def generate_ai_scenarios(conn: sqlite3.Connection, set_id: int, svc: LLMService, item_keys: list[str] | None = None,
                          limit: int | None = None, progress=None) -> dict[str, int]:
    cmds, fields = catalog_context(conn, set_id)
    q = "SELECT * FROM catalog_item WHERE set_id=? AND kind='requirement' AND observability<>'unobservable'"
    reqs = rows(conn, q, (set_id,))
    if item_keys is not None:
        reqs = [r for r in reqs if r["key"] in set(item_keys)]
    # skip requirements that already have scenarios
    have = {r[0] for r in conn.execute("SELECT DISTINCT si.item_key FROM scenario_item si JOIN scenario s ON s.id=si.scenario_id WHERE s.set_id=?", (set_id,))}
    reqs = [r for r in reqs if r["key"] not in have]
    if limit:
        reqs = reqs[:limit]
    existing = {r["predicate_json"] for r in rows(conn, "SELECT predicate_json FROM scenario WHERE set_id=?", (set_id,))}
    stats = {"requirements": len(reqs), "created": 0, "not_testable": 0, "rejected": 0, "failed": 0, "duplicates": 0}
    for n, req in enumerate(reqs):
        req["attrs"] = jloads(req.pop("attrs_json"), {})
        ctx = build_context(conn, set_id, req)
        user = ("REQUIREMENT:\n" + ctx["requirement"] + "\n\nCATALOG (the only commands/fields you may use):\n" +
                json.dumps(ctx["catalog"], indent=1)[:12000] + "\n\nCONTEXT:\n" + ctx["context_text"][:6000])
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
        try:
            out, meta = svc.generate_model("generate", msgs, AIOutput, PROMPT_VERSION)
        except LLMError as e:
            stats["failed"] += 1
            _record(conn, set_id, req["key"], "generate", svc, "failed", None, str(e))
            continue
        problems: list[str] = []
        created = 0
        if not out.testable or not out.scenarios:
            stats["not_testable"] += 1
            _record(conn, set_id, req["key"], "generate", svc, "not_testable", out.model_dump(), out.reason, meta)
            continue
        for sc in out.scenarios:
            errs: list[str] = []
            if not sc.quotes or not all(_grounded(qt, ctx["context_text"]) for qt in sc.quotes):
                errs.append("ungrounded quote(s)")
            try:
                pred = to_predicate(sc, cmds)
                errs += validate_predicate(pred, cmds, fields)
            except ValueError as e:
                pred, errs = None, errs + [str(e)]
            if errs:
                stats["rejected"] += 1
                problems.append(f"{sc.title}: {'; '.join(errs)}")
                continue
            pj = json.dumps(pred)
            if pj in existing:
                stats["duplicates"] += 1
                dup = conn.execute("SELECT id FROM scenario WHERE set_id=? AND predicate_json=? ORDER BY id LIMIT 1", (set_id, pj)).fetchone()
                if dup:   # the scenario already exists: this requirement must still be linked to it to count as covered by it
                    conn.execute("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key) VALUES(?,?)", (dup[0], req["key"]))
                continue
            existing.add(pj)
            sid = f"AI:{req['key']}#{created + 1}"
            cur = conn.execute("INSERT OR IGNORE INTO scenario(set_id,sid,origin,kind,title,description,observability,predicate_json,state,provenance_json)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (set_id, sid, "ai", "requirement", sc.title, sc.description, sc.observability, pj, "draft",
                                json.dumps({**meta, "prompt_version": PROMPT_VERSION, "quotes": sc.quotes})))
            if cur.rowcount:
                conn.execute("INSERT OR IGNORE INTO scenario_item(scenario_id,item_key) VALUES(?,?)", (cur.lastrowid, req["key"]))
                created += 1
                stats["created"] += 1
        _record(conn, set_id, req["key"], "generate", svc, "ok" if created else "needs_review", out.model_dump(),
                "; ".join(problems) or None, meta)
        conn.commit()
        if progress:
            progress(n + 1, len(reqs))
    return stats


def _record(conn, set_id, key, task, svc, state, output, error, meta=None):
    cfg = svc.task_cfg(task)
    conn.execute("INSERT INTO enrichment(set_id,item_key,task,provider,model,prompt_version,state,output_json,error) VALUES(?,?,?,?,?,?,?,?,?)",
                 (set_id, key, task, (meta or {}).get("provider", cfg["provider"]), cfg["model"], PROMPT_VERSION, state,
                  json.dumps(output) if output is not None else None, error))
    conn.commit()
