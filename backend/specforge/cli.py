"""specforge command line: serve the UI, ingest specs, build catalogs, analyse command DBs, run coverage, export reports."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog, cmddb, coverage, execscen, refs, reports
from .core import config
from .core.db import connect, row, rows
from .ingest import store


def _conn():
    return connect()


def cmd_serve(a):
    import uvicorn

    print(f"SpecForge workspace: {config.workspace_dir()}")
    uvicorn.run("specforge.api.app:app", host=a.host, port=a.port, log_level="info")


def cmd_demo(a):
    from .devtools.synth import make_command_db, make_demo_specs

    out = Path(a.dir)
    make_demo_specs(out)
    make_command_db(out / "demo-commands.db")
    print("Wrote synthetic fixtures (NOT real NVMe specs):")
    for p in sorted(out.iterdir()):
        print("  ", p)


def cmd_ingest(a):
    conn = _conn()
    ids = []
    for f in a.files:
        d = store.register_document(conn, f, a.family, a.version)
        print(f"registered #{d['id']} {d['family']} {d['version']} ({d['format']}, {d['pages']} pages)")
        rid = store.run_parse(conn, d["id"], workers=a.workers, progress=lambda s, x, y: print(f"\r  {s} {x}/{y}", end="", file=sys.stderr))
        print(file=sys.stderr)
        r = row(conn, "SELECT * FROM parse_run WHERE id=?", (rid,))
        audit = json.loads(r["audit_json"])
        print(f"  parse run {rid}: errors={audit['errors']} warnings={audit['warnings']} coverage_ready={audit['coverage_ready']}")
        ids.append(d["id"])
    if a.set:
        s = row(conn, "SELECT id FROM spec_set WHERE name=?", (a.set,))
        sid = s["id"] if s else conn.execute("INSERT INTO spec_set(name) VALUES(?)", (a.set,)).lastrowid
        for d in ids:
            conn.execute("INSERT OR IGNORE INTO spec_set_member VALUES(?,?)", (sid, d))
        conn.commit()
        print(f"spec set '{a.set}' (#{sid}) now has {len(rows(conn, 'SELECT 1 FROM spec_set_member WHERE set_id=?', (sid,)))} members")


def cmd_reparse(a):
    conn = _conn()
    rid = store.run_parse(conn, a.document_id, workers=a.workers, activate=a.activate)
    print(f"new parse run {rid} (active={a.activate}); diff against previous: "
          f"{store.diff_runs(conn, a.previous, rid)['summary']}" if a.previous else f"new parse run {rid}")


def cmd_catalog(a):
    conn = _conn()
    res = catalog.build_catalog(conn, a.set_id)
    print(f"catalog: {res['items']} items {res['kinds']}")
    print("references:", refs.summarize(refs.resolve_references(conn, a.set_id)))


def _import(conn, path: str) -> int:
    """Re-import the source already registered for this file, so `analyze` and `coverage` share its executed scenarios and reviews."""
    want = Path(path).resolve()
    known = [r["id"] for r in rows(conn, "SELECT id,path FROM cmd_source ORDER BY id DESC") if Path(r["path"]).resolve() == want]
    sid = known[0] if known else cmddb.register_source(conn, path, None)
    print("import:", {k: v for k, v in cmddb.import_source(conn, sid, config.load_settings(conn).get("mi_layout")).items() if k != "errors"})
    return sid


def _print_analysis(res: dict) -> None:
    print(f"executed: {res['tests']} test(s), {res['commands']} commands -> {res['scenarios']} scenario(s)"
          + (f", {res['unknown_commands']} command(s) with opcodes not in the catalog" if res["unknown_commands"] else ""))
    for k, v in res["observed"].items():
        print(f"  {k:<10} {v['observed']:>6} / {v['total']:<6} exercised")


def cmd_analyze(a):
    conn = _conn()
    sid = _import(conn, a.db)
    _print_analysis(execscen.analyze_execution(conn, a.set_id, sid))
    for s in rows(conn, "SELECT title,provenance_json FROM scenario WHERE set_id=? AND origin='exec' AND source_id=? ORDER BY id", (a.set_id, sid))[:a.show]:
        print("  -", s["title"])


def cmd_coverage(a):
    conn = _conn()
    sid = _import(conn, a.db)
    _print_analysis(execscen.analyze_execution(conn, a.set_id, sid))
    rid = coverage.run_coverage(conn, a.set_id, sid, analyze=False)
    o = json.loads(row(conn, "SELECT summary_json FROM coverage_run WHERE id=?", (rid,))["summary_json"])["overall"]
    print(f"coverage run {rid}: covered {o['covered']}/{o['denominator']} ({o['pct_covered']}%), stimulus-only {o['stimulus_covered']}, N/A {o['na']}")
    if a.out:
        data, _, name = reports.export(conn, rid, a.format)
        p = Path(a.out) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        print("wrote", p)


def cmd_models(a):
    from .llm.service import make_provider

    st = config.load_settings(_conn())
    for name in ("ollama", "opencode"):
        try:
            p = make_provider(name, st)
            print(name, p.health())
            for m in p.list_models():
                print("   ", m.id, m.detail)
        except Exception as e:  # noqa: BLE001
            print(name, "error:", e)


def main(argv=None):
    p = argparse.ArgumentParser(prog="specforge")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the API + web UI"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("make-demo", help="write synthetic demo specs and command DB"); s.add_argument("dir"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("ingest", help="register + parse spec files"); s.add_argument("files", nargs="+"); s.add_argument("--family"); s.add_argument("--version")
    s.add_argument("--workers", type=int, default=None); s.add_argument("--set", help="add to (and create) this spec set"); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("reparse", help="parse a registered document again"); s.add_argument("document_id", type=int); s.add_argument("--activate", action="store_true")
    s.add_argument("--previous", type=int, help="run id to diff against"); s.add_argument("--workers", type=int, default=None); s.set_defaults(fn=cmd_reparse)
    s = sub.add_parser("catalog", help="build the catalog for a spec set"); s.add_argument("set_id", type=int); s.set_defaults(fn=cmd_catalog)
    s = sub.add_parser("analyze", help="import a command DB and derive one scenario per executed test case"); s.add_argument("set_id", type=int)
    s.add_argument("db"); s.add_argument("--show", type=int, default=50, help="list this many scenarios"); s.set_defaults(fn=cmd_analyze)
    s = sub.add_parser("coverage", help="import a command DB, analyse what it executed and run coverage"); s.add_argument("set_id", type=int); s.add_argument("db")
    s.add_argument("--out", help="directory for the report"); s.add_argument("--format", default="xlsx", choices=sorted(reports.EXPORTS)); s.set_defaults(fn=cmd_coverage)
    s = sub.add_parser("models", help="list models from Ollama and OpenCode"); s.set_defaults(fn=cmd_models)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
