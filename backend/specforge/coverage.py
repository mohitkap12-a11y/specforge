"""L6: deterministic coverage engine.

The command source is first analysed (execscen.analyze_execution): every test case becomes an executed scenario linked to
the catalog items it exercised.  Those links are observations and count as they are; requirement links an AI proposed for
an executed scenario count once a reviewer approves them.  Spec-driven scenarios (the gap suggestions of crossgen) count when
approved: their predicate is evaluated against the decoded command signatures.  Item states are rolled up from both;
nothing here calls an LLM.
"""
from __future__ import annotations

import json
from bisect import bisect_left
import sqlite3
from collections import defaultdict
from typing import Any

from . import eventlog
from .core.db import jloads, row, rows

EVIDENCE_LIMIT = 50
MAX_SEARCH_STEPS = 300_000   # per scenario; exceeding it is reported as an error, never silently as "not matched"


class SearchBudgetExceeded(RuntimeError):
    pass
STATES = ("covered", "stimulus_covered", "partial", "not_covered", "no_scenario", "needs_review", "na")


# ----------------------------------------------------------------------------- field access / ops

def get_bits(dwords: dict[str, int], dw: Any, hi: int, lo: int) -> int | None:
    v = dwords.get(str(dw))
    if v is None:
        return None
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def eval_field(f: dict, dwords: dict[str, int], env: dict[str, int]) -> bool:
    v = get_bits(dwords, f["dw"], f["hi"], f["lo"])
    op = f.get("op", "eq")
    if op == "absent":
        return v is None
    if v is None:
        return False
    want = f.get("value")
    if isinstance(want, dict) and "var" in want:
        if want["var"] not in env:
            return False
        want = env[want["var"]]
    if op == "eq":
        return v == want
    if op == "ne":
        return v != want
    if op == "in":
        return v in want
    if op == "range":
        return want["lo"] <= v <= want["hi"]
    if op == "mask":
        return (v & want["mask"]) == want["value"]
    return False


def step_matches_sig(step: dict, sig: dict, env: dict[str, int] | None = None) -> bool:
    if sig["interface"] != step["interface"] or sig["queue"] != step["queue"] or sig["opcode"] != step["opcode"]:
        return False
    if step.get("cs") and sig["cs"] != step["cs"]:
        return False
    env = env or {}
    return all(eval_field(f, sig["dwords"], env) for f in step.get("fields", []))


def _load_sigs(conn: sqlite3.Connection, source_id: int) -> dict[tuple, list[dict]]:
    by: dict[tuple, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT * FROM cmd_signature WHERE source_id=?", (source_id,)):
        d = dict(r)
        d["dwords"] = json.loads(d.pop("dwords_json"))
        by[(d["interface"], d["queue"], d["opcode"])].append(d)
    return by


# ----------------------------------------------------------------------------- scenario evaluation

def _evidence_rows(conn: sqlite3.Connection, source_id: int, sigs: list[str]) -> list[dict]:
    if not sigs:
        return []
    out: list[dict] = []
    for i in range(0, len(sigs), 500):
        part = sigs[i:i + 500]
        q = ",".join("?" * len(part))
        out += rows(conn, f"SELECT src_row_id,test_id,script,seq,signature FROM decoded_command "
                          f"WHERE source_id=? AND signature IN ({q}) ORDER BY test_id,seq LIMIT {EVIDENCE_LIMIT}", [source_id, *part])
        if len(out) >= EVIDENCE_LIMIT:
            break
    return out[:EVIDENCE_LIMIT]


def eval_single(conn: sqlite3.Connection, source_id: int, pred: dict, sigs_by: dict) -> tuple[int, list[dict]]:
    step = pred["steps"][0]
    matched = [s for s in sigs_by.get((step["interface"], step["queue"], step["opcode"]), []) if step_matches_sig(step, s)]
    count = sum(s["count"] for s in matched)
    return count, (_evidence_rows(conn, source_id, [s["signature"] for s in matched]) if matched else [])


def eval_sequence(conn: sqlite3.Connection, source_id: int, pred: dict, sigs_by: dict) -> tuple[int, list[dict]]:
    steps = pred["steps"]
    cand_sigs: list[dict[str, dict]] = []
    for st in steps:
        pool = {s["signature"]: s for s in sigs_by.get((st["interface"], st["queue"], st["opcode"]), [])}
        # drop var-dependent conditions for pre-filtering; they are re-checked with the environment later
        pre = {k: s for k, s in pool.items() if step_matches_sig({**st, "fields": [f for f in st.get("fields", []) if not isinstance(f.get("value"), dict) or "var" not in f["value"]]}, s)}
        if not pre:
            return 0, []
        cand_sigs.append(pre)
    union = sorted({k for c in cand_sigs for k in c})
    scope = pred.get("scope", "global")
    within = pred.get("within")
    order = pred.get("order", "strict")
    rows_by_scope: dict[str, list[dict]] = defaultdict(list)
    for i in range(0, len(union), 500):
        part = union[i:i + 500]
        q = ",".join("?" * len(part))
        for r in conn.execute(f"SELECT id,src_row_id,test_id,script,seq,signature FROM decoded_command WHERE source_id=? "
                              f"AND signature IN ({q}) ORDER BY test_id,seq,id", [source_id, *part]):
            d = dict(r)
            rows_by_scope["" if scope == "global" else (d["test_id"] if scope == "test" else d["script"])].append(d)
    matches = 0
    evidence: list[dict] = []
    for _, seq_rows in rows_by_scope.items():
        seq_rows.sort(key=lambda r: (r["test_id"], r["seq"], r["id"]))     # id breaks ties: rows arrive in several signature chunks

        budget = [MAX_SEARCH_STEPS]
        failed: set = set()    # strict order: (step, start, captured values) proven dead. Rows are only ever consumed, never freed,
        #                        so a dead state stays dead for the rest of this scan and the memo is never cleared.
        # Only rows that can play a step are ever visited: candidate row indices per step, in command order.
        cand_idx = [[i for i, r in enumerate(seq_rows) if r["signature"] in cand_sigs[k]] for k in range(len(steps))]
        lo = [0] * len(steps)  # per step: everything before this position is consumed or known dead
        consumed: set[int] = set()

        def attempt(step_i: int, start: int, env: dict, chosen: list[tuple[int, dict]]) -> list[tuple[int, dict]] | None:
            if step_i == len(steps):
                return chosen
            memo = (step_i, start, tuple(sorted(env.items())))
            if order == "strict" and memo in failed:
                return None
            st = steps[step_i]
            lst = cand_idx[step_i]
            while lo[step_i] < len(lst) and lst[lo[step_i]] in consumed:
                lo[step_i] += 1
            pos0 = max(bisect_left(lst, start), lo[step_i]) if order == "strict" else lo[step_i]
            for pos in range(pos0, len(lst)):
                idx = lst[pos]
                budget[0] -= 1                       # every visited candidate is charged, so the scan always terminates
                if budget[0] < 0:
                    raise SearchBudgetExceeded(f"sequence search exceeded {MAX_SEARCH_STEPS} steps")
                if idx in consumed or any(idx == c[0] for c in chosen):
                    continue
                r = seq_rows[idx]
                if within is not None and chosen and abs(r["id"] - chosen[-1][1]["id"]) > within:   # distance in the command stream, not per-test seq
                    continue
                sig = cand_sigs[step_i][r["signature"]]
                if not step_matches_sig(st, sig, env):
                    continue
                env2 = dict(env)
                for name, cap in (st.get("capture") or {}).items():
                    v = get_bits(sig["dwords"], cap["dw"], cap["hi"], cap["lo"])
                    if v is not None:
                        env2[name] = v
                    else:
                        env2.pop(name, None)     # an absent dword must not leave an earlier binding behind for later comparisons
                res = attempt(step_i + 1, idx + 1, env2, chosen + [(idx, r)])
                if res is not None:
                    return res
            if order == "strict":
                failed.add(memo)
            return None

        # Count disjoint occurrences. Every search starts from the first row, so a later start can still pair rows an earlier
        # match skipped over; rows of earlier matches are never reused. Starts tried before a successful one are dead for good
        # (consumption only removes options), so step 0's pointer moves past them: each candidate is examined O(1) times.
        while True:
            found = attempt(0, 0, {}, [])
            if not found:
                break
            matches += 1
            if len(evidence) < EVIDENCE_LIMIT:
                evidence.extend(r for _, r in found)
            consumed |= {i for i, _ in found}
            lo[0] = max(lo[0], bisect_left(cand_idx[0], found[0][0]))
    return matches, evidence[:EVIDENCE_LIMIT]


# ----------------------------------------------------------------------------- run

def run_coverage(conn: sqlite3.Connection, set_id: int, source_id: int, progress=None, analyze: bool = True) -> int:
    """`analyze` (re)derives the executed scenarios of the source first, so they always match the current catalog and log."""
    from .execscen import analysis, analyze_execution

    src = row(conn, "SELECT * FROM cmd_source WHERE id=?", (source_id,))
    if not src or src["status"] != "imported":
        raise ValueError("command source must be imported before running coverage")
    if analyze:
        analyze_execution(conn, set_id, source_id)
    execution = analysis(conn, set_id, source_id)
    sigs_by = _load_sigs(conn, source_id)
    # executed scenarios belong to the command DB they were read from; another DB's never count here
    scen = rows(conn, "SELECT * FROM scenario WHERE set_id=? AND (origin<>'exec' OR source_id=?)", (set_id, source_id))
    by_review = {}
    for s_ in scen:
        by_review[s_["state"]] = by_review.get(s_["state"], 0) + 1
    eventlog.emit("info", "coverage", f"Matching {len(scen)} scenarios against '{src['name'] or src['path']}' ({src['decoded_count']} commands, {len(sigs_by)} distinct)",
                  set_id=set_id, source_id=source_id, scenarios_by_review_state=by_review)
    cur = conn.execute("INSERT INTO coverage_run(set_id,source_id,meta_json) VALUES(?,?,?)",
                       (set_id, source_id, json.dumps(_run_meta(conn, set_id, src, scen))))
    run_id = cur.lastrowid
    results: dict[int, dict] = {}
    batch = []
    for n, s in enumerate(scen):
        snap = (s["sid"], s["origin"], s["kind"], s["title"], s["observability"], s["state"], s["predicate_json"])
        if s["origin"] == "exec":         # read from this very log: matched by construction, the evidence is the commands that ran it
            prov = jloads(s["provenance_json"], {})
            results[s["id"]] = {"state": "matched", "count": prov.get("commands", 0)}
            batch.append((run_id, s["id"], "matched", prov.get("commands", 0), json.dumps(prov.get("evidence", [])), *snap))
            continue
        if s["state"] != "approved":      # drafts / rejected scenarios never count, but the run still records that they existed
            batch.append((run_id, s["id"], "not_evaluated", 0, "[]", *snap))
            continue
        pred = json.loads(s["predicate_json"])
        try:
            count, ev = (eval_single if len(pred["steps"]) == 1 else eval_sequence)(conn, source_id, pred, sigs_by)
        except Exception as e:  # noqa: BLE001
            count, ev = 0, [{"error": str(e)}]
            st = "error"
            eventlog.emit("warn", "coverage", f"Scenario {s['sid']} could not be evaluated: {e}", scenario_id=s["id"], title=s["title"])
        else:
            st = "matched" if count > 0 else "not_matched"
        results[s["id"]] = {"state": st, "count": count}
        batch.append((run_id, s["id"], st, count, json.dumps(ev), *snap))
        if progress and n % 200 == 0:
            progress(n, len(scen))
    conn.executemany("INSERT INTO coverage_result(run_id,scenario_id,state,match_count,evidence_json,sid,origin,kind,title,observability,review_state,predicate_json)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)

    items = rows(conn, "SELECT * FROM catalog_item WHERE set_id=?", (set_id,))
    links: dict[str, list[tuple[int, str | None]]] = defaultdict(list)
    for r in conn.execute("SELECT si.item_key, si.scenario_id, si.state FROM scenario_item si JOIN scenario s ON s.id=si.scenario_id "
                          "WHERE s.set_id=? AND (s.origin<>'exec' OR s.source_id=?)", (set_id, source_id)):
        links[r[0]].append((r[1], r[2]))
    scen_state = {s["id"]: s["state"] for s in scen}
    item_res: dict[str, dict] = {}
    for it in items:
        lk = links.get(it["key"], [])
        item_res[it["key"]] = _item_state(it, lk, scen_state, results, execution is not None)
        shown = sorted({sid for sid, st in lk if st != "rejected"})
        if shown:                         # which scenarios this item was judged by, as of this run
            item_res[it["key"]].setdefault("detail", {})["scenario_ids"] = shown
    # aggregate fields from their children
    children: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if it["parent_key"]:
            children[it["parent_key"]].append(it)
    for it in items:
        if it["kind"] == "field" and it["observability"] != "unobservable":     # an explicit N/A decision (and its reason) is never rolled up away
            item_res[it["key"]] = _aggregate([item_res[c["key"]] for c in children.get(it["key"], [])])
    by_key = {i["key"]: i for i in items}
    # the catalog is mutable (rebuilds, overrides); a coverage run keeps its own snapshot of every item it judged
    conn.executemany("INSERT INTO item_result(run_id,item_key,state,covered,total,detail_json,kind,parent_key,label,observability,na_reason,attrs_json)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     [(run_id, k, v["state"], v.get("covered", 0), v.get("total", 0), json.dumps(v.get("detail", {})),
                       by_key[k]["kind"], by_key[k]["parent_key"], by_key[k]["label"], by_key[k]["observability"], by_key[k]["na_reason"],
                       by_key[k]["attrs_json"]) for k, v in item_res.items()])
    summary = summarize_run(conn, run_id, set_id, source_id, items, item_res, sigs_by)
    if execution is not None:
        summary["execution"] = {k: v for k, v in execution.items() if k not in ("new", "updated", "removed", "links_reopened")}
    conn.execute("UPDATE coverage_run SET summary_json=? WHERE id=?", (json.dumps(summary), run_id))
    conn.commit()
    o = summary["overall"]
    eventlog.emit("info", "coverage", f"Coverage run #{run_id} done: {o['pct_covered']}% covered ({o['covered']} covered, {o['partial']} partial, {o['not_covered']} not covered, "
                  f"{o['no_scenario']} without scenario, {o['needs_review']} awaiting review, {o['na']} N/A)", run_id=run_id, overall=o, unknown_commands=len(summary["unknown_commands"]))
    if summary["unknown_commands"]:
        eventlog.emit("warn", "coverage", f"{len(summary['unknown_commands'])} command type(s) in the database are not in the catalog", run_id=run_id, top=summary["unknown_commands"][:5])
    return run_id  # type: ignore[return-value]


def _run_meta(conn: sqlite3.Connection, set_id: int, src: dict, scen: list[dict]) -> dict[str, Any]:
    """Everything a report needs to describe this run later, captured now (spec versions, audit state, models, inputs)."""
    specs = rows(conn, """SELECT d.family,d.version,d.filename,d.sha256,d.format,r.id AS parse_run,r.parser_version,r.audit_json
                          FROM spec_set_member m JOIN spec_document d ON d.id=m.document_id
                          LEFT JOIN parse_run r ON r.document_id=d.id AND r.active=1 WHERE m.set_id=?""", (set_id,))
    for s in specs:
        a = jloads(s.pop("audit_json"), {})
        s["audit_score"], s["errors"], s["warnings"] = a.get("score"), a.get("errors"), a.get("warnings")
    issues = rows(conn, """SELECT d.family,d.version,p.severity,p.kind,p.node_key,p.page,p.message,p.state FROM parse_issue p
                           JOIN parse_run r ON r.id=p.run_id AND r.active=1 JOIN spec_document d ON d.id=r.document_id
                           JOIN spec_set_member m ON m.document_id=d.id AND m.set_id=? WHERE p.severity<>'info'""", (set_id,))
    models = sorted({(jloads(s["provenance_json"], {}) or {}).get("model", "") for s in scen if s["origin"] == "ai"} - {""})
    sset = row(conn, "SELECT name FROM spec_set WHERE id=?", (set_id,))
    return {"source_sha256": src["sha256"], "scenarios": len(scen), "command_db": src["path"], "commands_imported": src["row_count"],
            "spec_set": sset["name"] if sset else "", "specs": specs, "issues": issues, "ai_models": models}


OBSERVED_KINDS = ("command", "field", "value", "boundary", "reserved", "mi_tunnel")   # what a log of submissions shows directly


def _item_state(it: dict, links: list[tuple[int, str | None]], scen_state: dict[int, str], results: dict[int, dict], analysed: bool = False) -> dict:
    """links: (scenario id, link state).  A link without a state belongs to a spec-driven scenario and counts when that scenario is
    approved; 'observed' links (read from the log) and 'approved' requirement links count; 'proposed' ones wait for review."""
    if it["observability"] == "unobservable":
        return {"state": "na", "covered": 0, "total": 0, "detail": {"reason": it.get("na_reason") or "not observable at submission level"}}
    counted, pending, executed = [], 0, False
    for sid, st in links:
        if st is None:
            if scen_state.get(sid) == "approved":
                counted.append(sid)
            else:
                pending += 1
        elif st in ("observed", "approved"):
            counted.append(sid)
            executed = True
        elif st == "proposed":
            pending += 1
    if not counted:
        if pending:
            return {"state": "needs_review", "covered": 0, "total": 1, "detail": {"scenarios": pending}}
        if analysed and it["kind"] in OBSERVED_KINDS:
            return {"state": "not_covered", "covered": 0, "total": 1, "detail": {"reason": "not exercised by any test in the command DB"}}
        return {"state": "no_scenario", "covered": 0, "total": 1}
    hit = sum(1 for s in counted if results.get(s, {}).get("state") == "matched")
    detail = {"scenarios": len(counted), "matched": hit, **({"executed": True} if executed else {})}
    if hit == len(counted) or executed:      # an executed test is direct evidence; an unmatched spec suggestion does not undo it
        return {"state": "covered" if it["observability"] == "submission" else "stimulus_covered", "covered": 1, "total": 1, "detail": detail}
    return {"state": "partial" if hit else "not_covered", "covered": 1 if hit else 0, "total": 1, "detail": detail}


def _aggregate(kids: list[dict]) -> dict:
    live = [k for k in kids if k["state"] != "na"]
    if not live:
        return {"state": "na", "covered": 0, "total": 0}
    cov = sum(1 for k in live if k["state"] in ("covered", "stimulus_covered"))
    total = len(live)
    detail = {"covered": cov, "total": total}
    if cov == total:
        st = "covered" if all(k["state"] == "covered" for k in live) else "stimulus_covered"
    elif cov or any(k["state"] == "partial" for k in live):
        st = "partial"
    elif all(k["state"] in ("no_scenario", "needs_review") for k in live):
        st = live[0]["state"]
    else:
        st = "not_covered"
    return {"state": st, "covered": cov, "total": total, "detail": detail}


DIRECT_KINDS = ("command", "requirement")      # what cross-command scenarios are judged against; fields / values only provide the vocabulary


def _counts(states: list[str]) -> dict[str, Any]:
    c = {s: 0 for s in STATES}
    for s in states:
        c[s] += 1
    live = sum(v for k, v in c.items() if k != "na")
    full = c["covered"]
    return {**c, "total": len(states), "denominator": live,
            "pct_covered": round(100 * full / live, 2) if live else None,
            "pct_incl_stimulus": round(100 * (full + c["stimulus_covered"]) / live, 2) if live else None}


def summarize_run(conn, run_id, set_id, source_id, items, item_res, sigs_by) -> dict[str, Any]:
    attrs = {i["key"]: jloads(i["attrs_json"], {}) for i in items}
    cmd_of = {}
    for i in items:
        a = attrs[i["key"]]
        cmd_of[i["key"]] = i["key"] if i["kind"] == "command" else a.get("command")
    direct = [i for i in items if i["kind"] in DIRECT_KINDS]
    by_kind: dict[str, list[str]] = defaultdict(list)
    by_cmd: dict[str, list[str]] = defaultdict(list)
    by_family: dict[str, list[str]] = defaultdict(list)
    for i in direct:
        st = item_res[i["key"]]["state"]
        by_kind[i["kind"]].append(st)
        c = cmd_of.get(i["key"])
        if c:
            by_cmd[c].append(st)
        fam = attrs[c]["family"] if c and c in attrs else attrs[i["key"]].get("family")
        if fam:
            by_family[fam].append(st)
    known = set()
    for it in items:
        if it["kind"] == "command":
            a = attrs[it["key"]]
            known.add((a["interface"] if a["interface"] != "mi" else "mi", a["queue"], a["opcode"]))
    unknown = []
    for (iface, queue, opc), sl in sigs_by.items():
        if (iface, queue, opc) in known:
            continue
        if iface == "mi" and queue == "admin" and ("pcie", "admin", opc) in known:
            continue
        unknown.append({"interface": iface, "queue": queue, "opcode": opc, "commands": sum(s["count"] for s in sl)})
    every_kind: dict[str, list[str]] = defaultdict(list)
    for i in items:
        every_kind[i["kind"]].append(item_res[i["key"]]["state"])
    return {"overall": _counts([item_res[i["key"]]["state"] for i in direct]),
            "by_kind": {k: _counts(v) for k, v in by_kind.items()},
            "by_kind_all": {k: _counts(v) for k, v in every_kind.items()},
            "by_command": {k: _counts(v) for k, v in by_cmd.items()},
            "by_family": {k: _counts(v) for k, v in by_family.items()},
            "unknown_commands": sorted(unknown, key=lambda u: -u["commands"]),
            "scenarios": rows(conn, "SELECT state, COUNT(*) AS n FROM scenario WHERE set_id=? GROUP BY state", (set_id,)),
            "scenario_errors": conn.execute("SELECT COUNT(*) FROM coverage_result WHERE run_id=? AND state='error'", (run_id,)).fetchone()[0]}


def diff_runs(conn: sqlite3.Connection, a: int, b: int) -> dict[str, Any]:
    ra = {r["item_key"]: r["state"] for r in rows(conn, "SELECT item_key,state FROM item_result WHERE run_id=?", (a,))}
    rb = {r["item_key"]: r["state"] for r in rows(conn, "SELECT item_key,state FROM item_result WHERE run_id=?", (b,))}
    good = {"covered", "stimulus_covered"}
    gained = sorted(k for k in rb if rb[k] in good and ra.get(k) not in good)
    lost = sorted(k for k in ra if ra[k] in good and rb.get(k) not in good)
    return {"from": a, "to": b, "gained": gained, "lost": lost,
            "summary": {"gained": len(gained), "lost": len(lost), "added_items": len(set(rb) - set(ra)), "removed_items": len(set(ra) - set(rb))}}
