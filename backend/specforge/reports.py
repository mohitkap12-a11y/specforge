"""Report export. Every report records the inputs needed to reproduce its numbers."""
from __future__ import annotations

import csv
import html
import io
import json
import sqlite3
from typing import Any

from . import __version__
from .core.db import jloads, row, rows

GAP_STATES = ("not_covered", "partial", "no_scenario", "needs_review")


def build_dataset(conn: sqlite3.Connection, run_id: int) -> dict[str, Any]:
    run = row(conn, "SELECT * FROM coverage_run WHERE id=?", (run_id,))
    if not run:
        raise KeyError(f"coverage run {run_id}")
    summary = jloads(run["summary_json"], {})
    src = row(conn, "SELECT * FROM cmd_source WHERE id=?", (run["source_id"],))
    sset = row(conn, "SELECT * FROM spec_set WHERE id=?", (run["set_id"],))
    specs = rows(conn, """SELECT d.family,d.version,d.filename,d.sha256,d.format,r.id AS parse_run,r.parser_version,r.audit_json
                          FROM spec_set_member m JOIN spec_document d ON d.id=m.document_id
                          LEFT JOIN parse_run r ON r.document_id=d.id AND r.active=1 WHERE m.set_id=?""", (run["set_id"],))
    for s in specs:
        a = jloads(s.pop("audit_json"), {})
        s["audit_score"], s["errors"], s["warnings"] = a.get("score"), a.get("errors"), a.get("warnings")
    items = rows(conn, """SELECT item_key AS key,kind,parent_key,label,observability,na_reason,attrs_json,state,covered,total
                          FROM item_result WHERE run_id=? ORDER BY rowid""", (run_id,))     # the run's own snapshot
    for i in items:
        a = jloads(i.pop("attrs_json"), {})
        i["command"] = a.get("command") or (i["key"] if i["kind"] == "command" else "")
    scen = rows(conn, """SELECT s.id,s.sid,s.origin,s.kind,s.title,s.observability,s.state,s.predicate_json,
                                r.state AS result, r.match_count, r.evidence_json
                         FROM scenario s LEFT JOIN coverage_result r ON r.scenario_id=s.id AND r.run_id=?
                         WHERE s.set_id=? ORDER BY s.id""", (run_id, run["set_id"]))
    models = sorted({(jloads(s["provenance_json"], {}) or {}).get("model", "") for s in rows(
        conn, "SELECT provenance_json FROM scenario WHERE set_id=? AND origin='ai'", (run["set_id"],))} - {""})
    issues = rows(conn, """SELECT d.family,d.version,p.severity,p.kind,p.node_key,p.page,p.message,p.state FROM parse_issue p
                           JOIN parse_run r ON r.id=p.run_id AND r.active=1 JOIN spec_document d ON d.id=r.document_id
                           JOIN spec_set_member m ON m.document_id=d.id AND m.set_id=? WHERE p.severity<>'info'""", (run["set_id"],))
    header = {"tool_version": __version__, "coverage_run": run_id, "created": run["created"], "spec_set": sset["name"] if sset else "",
              "command_db": src["path"] if src else "", "command_db_sha256": src["sha256"] if src else "",
              "commands_imported": src["row_count"] if src else 0, "ai_models": models,
              "scenarios_approved": sum(1 for s in scen if s["state"] == "approved"),
              "scenarios_draft": sum(1 for s in scen if s["state"] == "draft")}
    return {"header": header, "summary": summary, "specs": specs, "items": items, "scenarios": scen, "issues": issues}


def _pct(v):
    return "" if v is None else f"{v:.1f}%"


def _gap_rows(ds):
    return [i for i in ds["items"] if i["state"] in GAP_STATES]


def to_json(ds) -> bytes:
    return json.dumps(ds, indent=1, default=str).encode()


def to_csv(ds, which: str = "gaps") -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    if which == "gaps":
        w.writerow(["key", "kind", "command", "label", "observability", "state"])
        for i in _gap_rows(ds):
            w.writerow([i["key"], i["kind"], i["command"], i["label"], i["observability"], i["state"]])
    else:
        w.writerow(["key", "kind", "command", "label", "observability", "state", "na_reason"])
        for i in ds["items"]:
            w.writerow([i["key"], i["kind"], i["command"], i["label"], i["observability"], i["state"], i["na_reason"] or ""])
    return buf.getvalue().encode()


def to_xlsx(ds) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="1F2937")
    hdr_font = Font(color="FFFFFF", bold=True)
    fills = {"covered": "D1FAE5", "stimulus_covered": "CFFAFE", "partial": "FEF3C7", "not_covered": "FEE2E2",
             "no_scenario": "E5E7EB", "needs_review": "EDE9FE", "na": "F3F4F6"}

    def sheet(name: str, head: list[str], data: list[list], state_col: int | None = None):
        ws = wb.create_sheet(name)
        ws.append(head)
        for c in ws[1]:
            c.fill, c.font = hdr_fill, hdr_font
        for r in data:
            ws.append(r)
            if state_col is not None and r[state_col] in fills:
                ws.cell(row=ws.max_row, column=state_col + 1).fill = PatternFill("solid", fgColor=fills[r[state_col]])
        for i, h in enumerate(head, start=1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in data[:300]]) if data else len(h)
            ws.column_dimensions[get_column_letter(i)].width = min(70, max(10, width + 2))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        return ws

    ws = wb.active
    ws.title = "Summary"
    ws.append(["SpecForge coverage report"])
    ws["A1"].font = Font(bold=True, size=14)
    for k, v in ds["header"].items():
        ws.append([k, ", ".join(v) if isinstance(v, list) else v])
    ws.append([])
    o = ds["summary"].get("overall", {})
    ws.append(["Overall", "count"])
    for k in ("covered", "stimulus_covered", "partial", "not_covered", "no_scenario", "needs_review", "na", "denominator"):
        ws.append([k, o.get(k, 0)])
    ws.append(["pct_covered (submission-complete)", _pct(o.get("pct_covered"))])
    ws.append(["pct incl. stimulus-only", _pct(o.get("pct_incl_stimulus"))])
    ws.append(["N/A items are excluded from the denominator and listed on the 'N/A' sheet."])
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 60

    by_cmd = ds["summary"].get("by_command", {})
    names = {i["key"]: i["label"] for i in ds["items"] if i["kind"] == "command"}
    sheet("By command", ["command", "name", "covered", "stimulus_covered", "partial", "not_covered", "no_scenario", "na", "% covered", "% incl. stimulus"],
          [[k, names.get(k, ""), v["covered"], v["stimulus_covered"], v["partial"], v["not_covered"], v["no_scenario"], v["na"],
            _pct(v["pct_covered"]), _pct(v["pct_incl_stimulus"])] for k, v in sorted(by_cmd.items())])
    cols = ["key", "kind", "command", "label", "observability", "state"]
    sheet("All items", cols, [[i[c] for c in cols] for i in ds["items"]], state_col=5)
    sheet("Gaps", cols, [[i[c] for c in cols] for i in _gap_rows(ds)], state_col=5)
    sheet("Stimulus only", cols, [[i[c] for c in cols] for i in ds["items"] if i["state"] == "stimulus_covered"], state_col=5)
    sheet("N-A", cols + ["reason"], [[*[i[c] for c in cols], i["na_reason"] or "not observable at submission level"] for i in ds["items"] if i["state"] == "na"], state_col=5)
    sheet("Scenarios", ["sid", "origin", "kind", "title", "observability", "review state", "result", "match_count"],
          [[s["sid"], s["origin"], s["kind"], s["title"], s["observability"], s["state"], s["result"] or "", s["match_count"] or 0] for s in ds["scenarios"]], state_col=None)
    ev = []
    for s in ds["scenarios"]:
        for e in jloads(s["evidence_json"], []) or []:
            ev.append([s["sid"], e.get("src_row_id"), e.get("test_id"), e.get("script"), e.get("seq")])
    sheet("Evidence", ["scenario", "db row id", "test", "script", "seq"], ev)
    sheet("Unknown commands", ["interface", "queue", "opcode", "commands"],
          [[u["interface"], u["queue"], f"{u['opcode']:#04x}", u["commands"]] for u in ds["summary"].get("unknown_commands", [])])
    sheet("Specs", ["family", "version", "file", "sha256", "format", "parse run", "parser", "audit score", "errors", "warnings"],
          [[s["family"], s["version"], s["filename"], s["sha256"], s["format"], s["parse_run"], s["parser_version"], s["audit_score"], s["errors"], s["warnings"]] for s in ds["specs"]])
    sheet("Parse issues", ["family", "version", "severity", "kind", "node", "page", "message", "state"],
          [[i["family"], i["version"], i["severity"], i["kind"], i["node_key"], i["page"], i["message"], i["state"]] for i in ds["issues"]])
    for w in wb.worksheets:
        for row_ in w.iter_rows(min_row=2):
            for c in row_:
                c.alignment = Alignment(vertical="top", wrap_text=False)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def to_html(ds) -> bytes:
    e = html.escape
    o = ds["summary"].get("overall", {})
    colors = {"covered": "#10b981", "stimulus_covered": "#06b6d4", "partial": "#f59e0b", "not_covered": "#ef4444",
              "no_scenario": "#9ca3af", "needs_review": "#8b5cf6", "na": "#d1d5db"}
    def bar(c):
        tot = max(1, sum(c[k] for k in colors))
        return "".join(f'<span title="{k}: {c[k]}" style="display:inline-block;height:12px;width:{100 * c[k] / tot:.2f}%;background:{colors[k]}"></span>' for k in colors if c[k])
    names = {i["key"]: i["label"] for i in ds["items"] if i["kind"] == "command"}
    cmd_rows = "".join(
        f"<tr><td>{e(k)}</td><td>{e(names.get(k, ''))}</td><td>{v['covered']}/{v['denominator']}</td><td>{_pct(v['pct_covered'])}</td>"
        f"<td style='width:30%'>{bar(v)}</td></tr>" for k, v in sorted(ds["summary"].get("by_command", {}).items()))
    gap_rows = "".join(f"<tr><td>{e(i['command'])}</td><td>{e(i['kind'])}</td><td>{e(i['label'])}</td><td>{e(i['state'])}</td></tr>" for i in _gap_rows(ds))
    hdr = "".join(f"<tr><th>{e(k)}</th><td>{e(', '.join(v) if isinstance(v, list) else str(v))}</td></tr>" for k, v in ds["header"].items())
    legend = "".join(f'<span style="margin-right:12px"><i style="display:inline-block;width:10px;height:10px;background:{c}"></i> {k}</span>' for k, c in colors.items())
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>SpecForge coverage report</title>
<style>body{{font:14px system-ui,sans-serif;margin:32px;color:#111}}table{{border-collapse:collapse;margin:12px 0}}th,td{{border:1px solid #ddd;padding:4px 8px;text-align:left}}
th{{background:#f3f4f6}}.big{{font-size:32px;font-weight:700}}@media print{{body{{margin:12px}}}}</style></head><body>
<h1>SpecForge coverage report</h1><table>{hdr}</table>
<p><span class="big">{_pct(o.get('pct_covered'))}</span> covered (submission-complete) &nbsp; <b>{_pct(o.get('pct_incl_stimulus'))}</b> including stimulus-only
 &nbsp; denominator {o.get('denominator', 0)} (N/A excluded: {o.get('na', 0)})</p><p>{legend}</p>
<h2>By command</h2><table><tr><th>key</th><th>command</th><th>covered</th><th>%</th><th>distribution</th></tr>{cmd_rows}</table>
<h2>Gaps</h2><table><tr><th>command</th><th>kind</th><th>item</th><th>state</th></tr>{gap_rows}</table></body></html>"""
    return doc.encode()


EXPORTS = {
    "xlsx": (to_xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "json": (to_json, "application/json"),
    "html": (to_html, "text/html"),
    "csv": (to_csv, "text/csv"),
}


def export(conn: sqlite3.Connection, run_id: int, fmt: str) -> tuple[bytes, str, str]:
    if fmt not in EXPORTS:
        raise ValueError(f"unknown format {fmt}")
    ds = build_dataset(conn, run_id)
    fn, mime = EXPORTS[fmt]
    return fn(ds), mime, f"specforge-coverage-run{run_id}.{fmt}"
