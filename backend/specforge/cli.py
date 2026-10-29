"""specforge command line: serve the UI, ingest specs, build catalogs, analyse command DBs, run coverage, export reports."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog, cmddb, compare, coverage, execscen, refs, reports
from .convert import ENGINES
from .core import config
from .core.db import connect, row, rows
from .ingest import store


def _conn():
    return connect()


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def cmd_serve(a):
    import os

    import uvicorn

    from . import auth

    if a.no_auth:
        os.environ["SPECFORGE_AUTH"] = "0"
    if not config.auth_enabled() and a.host not in LOOPBACK:      # --no-auth, or SPECFORGE_AUTH=0 left in the environment
        sys.exit("sign-in is off (--no-auth / SPECFORGE_AUTH=0): only allowed with a localhost --host, "
                 "otherwise anyone on the network would be an admin")
    print(f"SpecForge workspace: {config.workspace_dir()}")
    if config.auth_enabled():
        conn = _conn()
        if not auth.count_admins(conn):
            print("No admin account yet. Create one before signing in:  specforge user create <name> --admin")
        n = auth.pending_count(conn)
        if n:
            print(f"{n} access request(s) waiting for approval (Users page, or `specforge user list`)")
    else:
        print("Sign-in is OFF (--no-auth): local single-user mode")
    uvicorn.run("specforge.api.app:app", host=a.host, port=a.port, log_level="info")


def _password(given: str | None, prompt: str = "Password: ") -> str:
    if given:
        return given
    import getpass

    p1 = getpass.getpass(prompt)
    if getpass.getpass("Repeat password: ") != p1:
        sys.exit("passwords do not match")
    return p1


def _user(conn, name: str) -> dict:
    from . import auth

    u = auth.find_user(conn, name)
    if not u:
        sys.exit(f"no user named {name!r}")
    return u


def cmd_user(a):
    """Accounts for `serve` with sign-in. Run on the server; works while the server is running."""
    from . import auth

    conn = _conn()
    try:
        if a.action == "create":
            u = auth.create_user(conn, a.username, _password(a.password), role="admin" if a.admin else "user", status="active",
                                 display_name=a.name, decided_by="cli")
            print(f"created {u['role']} {u['username']!r} (active)")
        elif a.action == "list":
            us = auth.list_users(conn)
            if not us:
                print("no users yet; create the first admin with: specforge user create <name> --admin")
            for u in us:
                print(f"{u['username']:<24} {u['role']:<6} {u['status']:<9} last sign-in: {u['last_login'] or '-':<20} {u['display_name'] or ''}")
        elif a.action == "passwd":
            u = _user(conn, a.username)
            auth.set_password(conn, u["id"], _password(a.password, "New password: "))
            print(f"password changed for {u['username']!r}; their sessions were signed out")
        elif a.action == "set":
            u = _user(conn, a.username)
            u = auth.update_user(conn, u["id"], role=a.role, status=a.status)
            print(f"{u['username']!r}: role={u['role']} status={u['status']}")
        elif a.action == "delete":
            u = auth.delete_user(conn, _user(conn, a.username)["id"])
            print(f"deleted {u['username']!r}")
    except auth.AuthError as e:
        sys.exit(f"error: {e}")


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


def _free_target(target: Path, source_name: str, written: set, sources: set) -> Path:
    """spec.pdf + spec.docx, or two spec.pdf from different folders, must not overwrite each other or an input file."""
    cands = [target, target.with_name(source_name + ".md")]
    cands += [target.with_name(f"{target.stem}-{i}{target.suffix}") for i in range(2, 10000)]
    return next(c for c in cands if c not in written and c.resolve() not in sources)


def cmd_to_md(a):
    from .convert import ConversionError, convert_file, output_name

    out_dir = Path(a.out) if a.out else None
    failed, written = 0, set()
    sources = {Path(f).resolve() for f in a.files}
    for f in a.files:
        try:
            res = convert_file(f, front_matter=not a.no_front_matter, engine=a.engine)
        except ConversionError as e:
            print(f"{f}: {e}", file=sys.stderr)
            failed += 1
            continue
        target = _free_target((out_dir or Path(f).parent) / output_name(Path(f).name), Path(f).name, written, sources)
        written.add(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(res.markdown, encoding="utf-8")
        st = res.stats
        print(f"{f} -> {target}  [{res.engine}] ({st['words']} words, {st['headings']} headings, {st['tables']} tables, {st['list_items']} list items)")
        for w in res.warnings:
            print("  warning:", w)
    if failed:
        sys.exit(1)


def cmd_prefetch_models(a):
    """Download Docling's model weights once, for machines that convert offline afterwards."""
    try:
        import docling
    except Exception as e:  # noqa: BLE001 - a broken torch install fails with OSError / RuntimeError too
        raise SystemExit(f"Docling cannot be loaded ({e.__class__.__name__}: {e}); install it with: pip install 'docling>=2.19'")
    try:
        from docling.utils.model_downloader import download_models
    except ImportError:
        raise SystemExit(f"Docling {getattr(docling, '__version__', '')} is too old to download models; upgrade: pip install -U 'docling>=2.19'")
    out = Path(a.out).resolve() if a.out else None
    path = download_models(output_dir=out, progress=True)
    print(f"Docling models are in {path or out or 'the default cache'}")
    if out:
        print(f"On the offline machine set DOCLING_ARTIFACTS_PATH={path}")


def _run_of(conn, family: str, version: str) -> int:
    r = row(conn, "SELECT p.id FROM parse_run p JOIN spec_document d ON d.id=p.document_id "
                  "WHERE d.family=? AND d.version=? AND p.status='done' ORDER BY p.active DESC, p.id DESC", (family, version))
    if not r:
        known = ", ".join(sorted({f"{d['family']} {d['version']}" for d in rows(conn, "SELECT family,version FROM spec_document")})) or "none"
        raise SystemExit(f"no parsed spec '{family}' version '{version}' (ingest it first). Known: {known}")
    return r["id"]


def cmd_compare(a):
    conn = _conn()
    try:
        cid = compare.run_comparison(conn, _run_of(conn, a.family, a.old), _run_of(conn, a.family, a.new))
    except compare.CompareError as e:
        raise SystemExit(str(e))
    if a.ai:
        from . import compare_ai
        from .llm.base import LLMError
        from .llm.service import LLMService

        try:
            compare_ai.summarize(conn, cid, LLMService(conn, config.load_settings(conn)), redo=True, progress=lambda m, x, y: print(f"  {m} {x}/{y}", file=sys.stderr))
        except LLMError as e:
            print(f"AI summary failed: {e}", file=sys.stderr)
    c = compare.get_comparison(conn, cid)
    print(c["overview"])
    if c["ai"]:
        print("\nAI summary:", c["ai"]["overview"])
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(compare.export_markdown(conn, cid), encoding="utf-8")
        print("wrote", p)


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
    s = sub.add_parser("serve", help="start the API + web UI"); s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s.add_argument("--no-auth", action="store_true", help="no sign-in (single user, localhost only)"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("user", help="manage sign-in accounts (create the first admin, reset a password, approve, disable)")
    us = s.add_subparsers(dest="action", required=True)
    u = us.add_parser("create", help="add an active account"); u.add_argument("username"); u.add_argument("--admin", action="store_true")
    u.add_argument("--name", help="display name"); u.add_argument("--password", help="omit to be prompted (keeps it out of shell history)")
    us.add_parser("list", help="list accounts and pending requests")
    u = us.add_parser("passwd", help="set a new password (signs the user out everywhere)"); u.add_argument("username"); u.add_argument("--password")
    u = us.add_parser("set", help="change role or status, e.g. approve a request: --status active"); u.add_argument("username")
    u.add_argument("--role", choices=["admin", "user"]); u.add_argument("--status", choices=["active", "disabled", "rejected"])
    u = us.add_parser("delete", help="remove an account"); u.add_argument("username")
    s.set_defaults(fn=cmd_user)
    s = sub.add_parser("make-demo", help="write synthetic demo specs and command DB"); s.add_argument("dir"); s.set_defaults(fn=cmd_demo)
    s = sub.add_parser("ingest", help="register + parse spec files"); s.add_argument("files", nargs="+"); s.add_argument("--family"); s.add_argument("--version")
    s.add_argument("--workers", type=int, default=None); s.add_argument("--set", help="add to (and create) this spec set"); s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("to-md", help="convert PDF / DOC / DOCX files to Markdown"); s.add_argument("files", nargs="+")
    s.add_argument("--out", help="output directory (default: next to each source file)"); s.add_argument("--no-front-matter", action="store_true", help="omit the YAML header")
    s.add_argument("--engine", choices=ENGINES, help="docling (default: layout model, best tables) or builtin (fast, no models)"); s.set_defaults(fn=cmd_to_md)
    s = sub.add_parser("prefetch-models", help="download Docling's model weights for offline conversion")
    s.add_argument("--out", help="folder for the weights (default: Docling's cache)"); s.set_defaults(fn=cmd_prefetch_models)
    s = sub.add_parser("compare", help="compare two parsed versions of a spec and list what changed"); s.add_argument("family"); s.add_argument("old"); s.add_argument("new")
    s.add_argument("--out", help="write the full report as Markdown"); s.add_argument("--ai", action="store_true", help="also write an AI summary (needs a configured model)"); s.set_defaults(fn=cmd_compare)
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
