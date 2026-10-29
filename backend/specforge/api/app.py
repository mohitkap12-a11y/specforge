"""FastAPI application: REST + SSE for the React UI. Binds to localhost by default (see cli.serve); every /api/ route needs a
signed-in user unless the server runs with --no-auth (see AuthGate and auth.py)."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import __version__, auth, catalog, cmddb, compare, compare_ai, convert, coverage, crossgen, eventlog, execscen, jobs, refs, reports, scenarios
from ..crossgen import generate_cross_scenarios
from ..convert import ENGINES, ConversionError, SUPPORTED, convert_file, output_name
from ..core import config
from ..core.db import get_conn, jloads, row, rows
from ..ingest import store
from ..llm.base import LLMError
from ..llm.service import LLMService, make_provider


MAX_CONVERT_BYTES = 200 * 1024 * 1024
SCENARIO_STATES = {"draft", "approved", "rejected"}
LINK_STATES = {"proposed", "approved", "rejected"}
EXEC_READONLY = "executed scenarios are read from the command DB: they have no review state and their steps cannot be edited; review their requirement links instead"
OBSERVABILITY = {"submission", "stimulus", "unobservable"}


def need(body: Any, *keys: str) -> None:
    """422 (not a 500) when a JSON body is not an object or lacks required fields."""
    if not isinstance(body, dict) or any(k not in body for k in keys):
        missing = keys if not isinstance(body, dict) else [k for k in keys if k not in body]
        raise HTTPException(422, f"required fields: {', '.join(missing)}")


def safe_join(root: Path, rel: str) -> Path | None:
    """Resolve rel under root; None unless the result is an existing file INSIDE root (is_relative_to, not a string prefix test)."""
    root = root.resolve()
    f = (root / rel).resolve()
    return f if rel and f.is_relative_to(root) and f.is_file() else None


# Reachable without a session (the login page needs them). Everything else under /api/ needs an active account.
PUBLIC = {("POST", "/api/auth/login"), ("POST", "/api/auth/register"), ("POST", "/api/auth/logout"), ("GET", "/api/auth/me")}
# Admin only: user management, global settings, wiping shared data.
ADMIN_ONLY = {("PUT", "/api/settings"), ("POST", "/api/reset"), ("DELETE", "/api/logs")}
ADMIN_PREFIX = "/api/users"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# With login turned off (`serve --no-auth`, localhost only) the single local user is the admin.
LOCAL_USER = {"id": 0, "username": "local", "display_name": None, "role": "admin", "status": "active"}


LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def is_https(req: Request) -> bool:
    return req.url.scheme == "https" or req.headers.get("x-forwarded-proto") == "https"


def session_cookie(token: str, https: bool) -> tuple[bytes, bytes]:
    """The Set-Cookie header for a session token (also re-sent whenever the server extends the session)."""
    r = Response()
    r.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict", secure=https, path="/")
    return next((k, v) for k, v in r.raw_headers if k == b"set-cookie")


def client_ip(req: Request) -> str:
    """The caller's address for throttling. Behind a reverse proxy on this machine (peer is loopback), the address the
    proxy saw: the rightmost X-Forwarded-For entry, which the proxy appends and the client cannot forge."""
    peer = req.client.host if req.client else "?"
    fwd = req.headers.get("x-forwarded-for")
    if peer in LOOPBACK and fwd:
        return fwd.split(",")[-1].strip() or peer
    return peer


def admin_only(method: str, path: str) -> bool:
    return (method, path) in ADMIN_ONLY or path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/")


class AuthGate:
    """ASGI middleware in front of /api/: refuses cross-origin writes, resolves the session cookie to a user (401 without
    one, 403 for admin-only routes) and makes the user available to the endpoint (`auth.current_user`). Plain ASGI rather
    than BaseHTTPMiddleware so the server-sent event streams pass through untouched. The static UI is not gated: it holds
    no data and shows the login page itself."""

    def __init__(self, app, enabled: bool):
        self.app, self.enabled = app, enabled

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        req = Request(scope)
        method = scope["method"]
        if method not in SAFE_METHODS and (origin := req.headers.get("origin")):
            # a page on another site, on another port of this host, or sandboxed / opaque ("null"), must not drive the API
            hosts = {req.headers.get("host"), req.headers.get("x-forwarded-host")}
            if urlsplit(origin).netloc not in hosts:
                return await JSONResponse({"detail": "cross-origin request refused"}, 403)(scope, receive, send)
        renewed = False
        if self.enabled:
            token = req.cookies.get(auth.COOKIE)
            user, renewed = await run_in_threadpool(lambda: auth.lookup_session(get_conn(), token)) if token else (None, False)
        else:
            user = LOCAL_USER
        if user is None and (method, path) not in PUBLIC:
            return await JSONResponse({"detail": "sign in required"}, 401)(scope, receive, send)
        if user is not None and user["role"] != "admin" and admin_only(method, path):
            return await JSONResponse({"detail": "only an admin can do this"}, 403)(scope, receive, send)
        if renewed:          # the server extended the session: extend the browser's cookie too
            cookie, inner = session_cookie(token, is_https(req)), send

            async def send(message):
                if message["type"] == "http.response.start" and not any(k == b"set-cookie" and auth.COOKIE.encode() in v
                                                                         for k, v in message.get("headers", [])):
                    message = {**message, "headers": [*message.get("headers", []), cookie]}
                await inner(message)
        tok = auth.current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            auth.current_user.reset(tok)


def create_app() -> FastAPI:
    app = FastAPI(title="SpecForge", version=__version__)
    DB = str(config.db_path())
    AUTH = config.auth_enabled()
    app.add_middleware(AuthGate, enabled=AUTH)

    def conn():
        return get_conn()

    def settings() -> dict:
        return config.load_settings(conn())

    def audit(message: str, level: str = "info", **data) -> None:
        u = auth.current_user.get()
        eventlog.emit(level, "api", message, **({"user": u["username"]} if u else {}), **data)

    @app.on_event("startup")
    def _startup():
        n = jobs.recover_interrupted(conn())
        eventlog.configure(settings())
        eventlog.emit("info", "system", f"SpecForge {__version__} started", workspace=str(config.workspace_dir()), parser_version=config.PARSER_VERSION)
        if n:
            eventlog.emit("warn", "system", f"{n} job(s) were interrupted by the previous shutdown and marked failed")
        if AUTH and not auth.count_admins(conn()):
            eventlog.emit("warn", "system", "Sign-in is on but there is no admin account yet: run `specforge user create <name> --admin` on the server")
        elif not AUTH:
            eventlog.emit("warn", "system", "Sign-in is turned off (--no-auth): everyone who can reach this server is an admin")

    # ------------------------------------------------------------------ sign-in and users
    login_throttle, ip_throttle, register_throttle = auth.Throttle(10, 600), auth.Throttle(50, 600), auth.Throttle(10, 3600)

    def auth_call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except auth.AuthError as e:
            raise HTTPException(e.status, str(e)) from None

    def me() -> dict:
        return auth.current_user.get()

    def public_user(u: dict) -> dict:
        return {k: u.get(k) for k in ("id", "username", "display_name", "role", "status")}

    def need_auth_on():
        if not AUTH:
            raise HTTPException(409, "sign-in is turned off on this server (started with --no-auth)")

    @app.get("/api/auth/me")
    def auth_me():
        u = me()
        return {"auth": AUTH, "user": public_user(u) if u else None,
                "pending": auth.pending_count(conn()) if AUTH and u and u["role"] == "admin" else 0}

    @app.post("/api/auth/login")
    def auth_login(request: Request, response: Response, body: Any = Body(...)):
        need_auth_on()
        need(body, "username", "password")
        ip = client_ip(request)
        key = f"{ip}|{str(body['username']).strip().lower()}"
        if login_throttle.blocked(key) or ip_throttle.blocked(ip):
            raise HTTPException(429, "too many failed sign-in attempts; wait a few minutes and try again")
        try:
            u = auth.authenticate(conn(), body["username"], body["password"])
        except auth.AuthError as e:
            if e.status == 401:
                login_throttle.fail(key)
                ip_throttle.fail(ip)
                eventlog.emit("warn", "api", f"Failed sign-in for '{str(body['username'])[:40]}'", ip=ip)
            raise HTTPException(e.status, str(e)) from None
        login_throttle.clear(key)
        token = auth.create_session(conn(), u["id"], ip, request.headers.get("user-agent"))
        response.raw_headers.append(session_cookie(token, is_https(request)))
        eventlog.emit("info", "api", f"{u['username']} signed in", user=u["username"], ip=ip)
        return {"user": public_user(u)}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        auth.end_session(conn(), request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/auth/register")
    def auth_register(request: Request, body: Any = Body(...)):
        """Request an account. It stays pending (cannot sign in) until an admin approves it."""
        need_auth_on()
        need(body, "username", "password")
        ip = client_ip(request)
        if register_throttle.blocked(ip):
            raise HTTPException(429, "too many access requests from this address; try again later")
        register_throttle.fail(ip)
        u = auth_call(auth.create_user, conn(), body["username"], body["password"], display_name=body.get("display_name"), note=body.get("note"))
        eventlog.emit("info", "api", f"Access requested by '{u['username']}' (waiting for an admin)", user=u["username"], ip=ip)
        return {"ok": True, "status": u["status"]}

    @app.post("/api/auth/password")
    def auth_password(request: Request, body: Any = Body(...)):
        need_auth_on()
        need(body, "current", "new")
        auth_call(auth.change_own_password, conn(), me()["id"], body["current"], body["new"], request.cookies.get(auth.COOKIE))
        audit("Password changed (own account)")
        return {"ok": True}

    @app.get("/api/users")
    def users_list():
        need_auth_on()
        return auth.list_users(conn())

    @app.post("/api/users")
    def users_create(body: Any = Body(...)):
        """An admin adds an account directly (active straight away)."""
        need_auth_on()
        need(body, "username", "password")
        u = auth_call(auth.create_user, conn(), body["username"], body["password"], role=body.get("role", "user"), status="active",
                      display_name=body.get("display_name"), decided_by=me()["username"])
        audit(f"User '{u['username']}' created ({u['role']})", target=u["username"])
        return u

    @app.patch("/api/users/{user_id}")
    def users_update(user_id: int, body: Any = Body(...)):
        """Approve / reject a request (status active / rejected), disable or re-enable an account, change role or name."""
        need_auth_on()
        if not isinstance(body, dict):
            raise HTTPException(422, "body must be a JSON object")
        before = auth.get_user(conn(), user_id)
        u = auth_call(auth.update_user, conn(), user_id, actor=me(), role=body.get("role"), status=body.get("status"), display_name=body.get("display_name"))
        changes = [f"{k} {before[k]} -> {u[k]}" for k in ("role", "status") if before and before[k] != u[k]]
        if changes:
            verb = {("pending", "active"): "approved", ("pending", "rejected"): "rejected"}.get((before["status"], u["status"]))
            audit(f"User '{u['username']}' {verb or 'changed'}: {', '.join(changes)}", target=u["username"])
        return u

    @app.post("/api/users/{user_id}/password")
    def users_reset_password(request: Request, user_id: int, body: Any = Body(...)):
        need_auth_on()
        need(body, "password")
        own = user_id == me()["id"]          # an admin resetting their own password keeps this session, like /api/auth/password
        auth_call(auth.set_password, conn(), user_id, body["password"], keep_session=request.cookies.get(auth.COOKIE) if own else None)
        u = auth.get_user(conn(), user_id)
        audit(f"Password reset by an admin for '{u['username']}' (signed out everywhere)", target=u["username"])
        return {"ok": True}

    @app.delete("/api/users/{user_id}")
    def users_delete(user_id: int):
        need_auth_on()
        u = auth_call(auth.delete_user, conn(), user_id, actor=me())
        audit(f"User '{u['username']}' deleted", "warn", target=u["username"])
        return {"ok": True}

    # ------------------------------------------------------------------ misc / settings
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "workspace": str(config.workspace_dir()),
                "parser_version": config.PARSER_VERSION, "reparse_available": store.reparse_available(conn())}

    @app.get("/api/settings")
    def get_settings():
        return settings()

    @app.put("/api/settings")
    def put_settings(body: Any = Body(...)):
        if not isinstance(body, dict):
            raise HTTPException(422, "settings must be a JSON object")
        if "logging" in body:      # validated BEFORE anything is stored: a rejected request must not leave a value that breaks startup
            lg = body["logging"]
            if not isinstance(lg, dict):
                raise HTTPException(422, "logging must be an object")
            if "min_level" in lg and not (isinstance(lg["min_level"], str) and lg["min_level"] in eventlog.LEVELS):
                raise HTTPException(422, f"logging.min_level must be one of {sorted(eventlog.LEVELS)}")
            if "capture_prompts" in lg and not isinstance(lg["capture_prompts"], bool):
                raise HTTPException(422, "logging.capture_prompts must be true or false")
            pc = lg.get("preview_chars", 4000)
            if not (isinstance(pc, int) and not isinstance(pc, bool) and 200 <= pc <= 50_000):
                raise HTTPException(422, "logging.preview_chars must be a whole number between 200 and 50000")
        saved = config.save_settings(conn(), body)
        eventlog.configure(saved)
        audit("Settings saved", sections=sorted(body) if isinstance(body, dict) else [])
        return saved

    @app.post("/api/reset")
    def reset_workspace():
        """Wipe everything produced while working (specs, sets, scenarios, command DBs, coverage, jobs, log, cache). Settings are kept."""
        if jobs.active(conn()):
            raise HTTPException(409, "Jobs are still running. Cancel them or wait for them to finish, then reset.")
        eventlog.drain(1.0)
        c = conn()
        counts = {}
        for table in ("spec_document", "spec_set", "cmd_source", "correction", "llm_cache", "enrichment", "job", "event_log"):
            counts[table] = c.execute(f"DELETE FROM {table}").rowcount    # parse runs, nodes, catalogs, scenarios and coverage go with these via ON DELETE CASCADE
        c.commit()
        files = 0
        shutil.rmtree(config.workspace_dir() / "conversions", ignore_errors=True)
        for sub in ("specs", "uploads"):
            for f in (config.workspace_dir() / sub).iterdir():
                if f.is_file():
                    f.unlink(missing_ok=True)
                    files += 1
        eventlog.emit("warn", "system", "Workspace reset: all specs, sets, scenarios, command databases, coverage runs and logs removed (settings kept)", **counts, files=files)
        return {"ok": True, "deleted": counts, "files": files}

    @app.get("/api/providers/{name}/models")
    def provider_models(name: str):
        try:
            prov = make_provider(name, settings())
            models = [m.__dict__ for m in prov.list_models()]
            audit(f"Connection test: {name} reachable, {len(models)} model(s)", provider=name)
            return {"ok": True, "models": models, "health": prov.health()}
        except LLMError as e:
            audit(f"Connection test: {name} failed: {e}", "warn", provider=name)
            return {"ok": False, "error": str(e), "models": []}

    # ------------------------------------------------------------------ spec -> markdown
    # Conversions run one at a time on the converter's own thread. The UI uses the job endpoint (a large spec takes
    # minutes to hours with Docling, longer than a proxy keeps a request open); its result is kept for a day.
    CONVERT_KEEP_SECONDS = 24 * 3600

    def _convert_params(file: UploadFile, engine: str | None) -> tuple[str, str]:
        if engine is not None and engine not in ENGINES:
            raise HTTPException(400, f"Unknown engine '{engine}'; use one of: {', '.join(ENGINES)}.")
        name = os.path.basename(file.filename or "document")
        suffix = Path(name).suffix.lower()
        if suffix not in SUPPORTED:
            raise HTTPException(400, f"Unsupported file type: {name}. Upload a PDF, DOC or DOCX file.")
        return name, suffix

    def _save_upload(file: UploadFile, suffix: str, folder: Path | None = None) -> str:
        size = 0
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=folder) as tmp:
            try:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_CONVERT_BYTES:
                        raise HTTPException(413, f"File is larger than {MAX_CONVERT_BYTES // (1024 * 1024)} MB.")
                    tmp.write(chunk)
            except BaseException:
                tmp.close()
                os.unlink(tmp.name)
                raise
        return tmp.name

    def _conversion_json(name: str, res) -> dict:
        return {"filename": output_name(name), "title": res.title, "format": res.source_format, "engine": res.engine,
                "stats": res.stats, "warnings": res.warnings}

    def _conversions_dir() -> Path:
        d = config.workspace_dir() / "conversions"
        d.mkdir(exist_ok=True)
        return d

    def _conversion_dir(name: str | None) -> Path | None:
        """The folder of one conversion, only if it really is a folder directly inside the conversions folder."""
        if not name:
            return None
        root = _conversions_dir().resolve()
        d = (root / name).resolve()
        return d if d.parent == root and d.is_dir() else None

    def _prune_conversions() -> None:
        """Delete results older than a day (a folder's mtime is when its Markdown was written); never a queued or running upload."""
        busy = {jloads(j["payload_json"], {}).get("dir") for j in
                rows(conn(), "SELECT payload_json FROM job WHERE type='convert-markdown' AND state IN ('queued','running')")}
        cutoff = time.time() - CONVERT_KEEP_SECONDS
        for d in _conversions_dir().iterdir():
            if d.is_dir() and d.name not in busy and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)

    @app.post("/api/convert/markdown")
    async def convert_markdown(file: UploadFile = File(...), front_matter: bool = Query(True), download: bool = Query(False),
                               engine: str | None = Query(None)):
        """Synchronous conversion (scripts, small files); waits on the conversion thread without holding a worker thread."""
        name, suffix = _convert_params(file, engine)
        path = await run_in_threadpool(_save_upload, file, suffix)
        try:
            res = await asyncio.wrap_future(convert.submit(path, filename=name, front_matter=front_matter, engine=engine))
        except ConversionError as e:
            raise HTTPException(422, str(e)) from e
        finally:
            os.unlink(path)
        out = output_name(name)
        audit(f"Converted {name} to Markdown", format=res.source_format, engine=res.engine, **res.stats)
        if download:
            return Response(res.markdown, media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(out)}"})
        return {**_conversion_json(name, res), "markdown": res.markdown}

    @app.post("/api/convert/markdown/jobs")
    def convert_markdown_job(file: UploadFile = File(...), front_matter: bool = Query(True), engine: str | None = Query(None)):
        """Background conversion: returns {job}; fetch the result from /api/convert/markdown/jobs/{job} once it is done."""
        name, suffix = _convert_params(file, engine)
        _prune_conversions()
        folder = Path(tempfile.mkdtemp(dir=_conversions_dir()))
        try:
            path = _save_upload(file, suffix, folder)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        user = me()["username"]

        def work(c, prog):
            try:
                prog(f"Converting {name} to Markdown")
                res = convert_file(path, filename=name, front_matter=front_matter, engine=engine)
                prog(f"Converted {name}")       # a Docling run cannot be interrupted: a cancel asked meanwhile stops here
                (folder / "out.md").write_text(res.markdown, encoding="utf-8")
            except BaseException:      # failed or cancelled: nothing to keep
                shutil.rmtree(folder, ignore_errors=True)
                raise
            finally:
                Path(path).unlink(missing_ok=True)
            eventlog.emit("info", "api", f"Converted {name} to Markdown", user=user, format=res.source_format, engine=res.engine, **res.stats)
            return {**_conversion_json(name, res), "dir": folder.name}
        job = jobs.submit(conn(), "convert-markdown", work, {"filename": name, "user": user, "engine": engine, "dir": folder.name}, DB,
                          pool=convert.serial_pool())
        audit(f"Conversion of {name} to Markdown queued", job_id=job, engine=engine or convert.default_engine())
        return {"job": job}

    def _conversion_entry(j: dict) -> dict | None:
        """A conversion job as the To Markdown page lists it; None once its file has expired."""
        j = jobs.with_progress(j, conn())
        payload = jloads(j["payload_json"], {})
        res = jloads(j["result_json"], None) or {}
        if j["state"] == "done" and safe_join(_conversions_dir(), f"{res.get('dir', '')}/out.md") is None:
            return None
        active = j["state"] in ("queued", "running")
        return {"job": j["id"], "source": payload.get("filename"), "state": j["state"], "message": j["message"], "created": j["created"],
                "cancelling": active and jobs.cancel_requested(j["id"], DB), "error": j["error"],
                "result": {k: v for k, v in res.items() if k != "dir"} if j["state"] == "done" else None}

    def _own_conversion(job_id: int) -> dict:
        j = jobs.get(conn(), job_id)
        payload = jloads(j["payload_json"], {}) if j else {}
        if not j or j["type"] != "convert-markdown" or payload.get("hidden") or \
                (payload.get("user") != me()["username"] and me()["role"] != "admin"):
            raise HTTPException(404, "conversion not found")
        return j

    @app.get("/api/convert/markdown/jobs")
    def convert_markdown_list():
        """The signed-in user's conversions, newest first: every queued or running one, and those that ended in the last 24 hours."""
        found = rows(conn(), "SELECT * FROM job WHERE type='convert-markdown' AND json_extract(payload_json, '$.user') = ? "
                             "AND json_extract(payload_json, '$.hidden') IS NULL "
                             "AND (state IN ('queued','running') OR updated >= datetime('now', ?)) ORDER BY id DESC",
                     (me()["username"], f"-{CONVERT_KEEP_SECONDS} seconds"))
        return [e for j in found if (e := _conversion_entry(j)) is not None]

    @app.delete("/api/convert/markdown/jobs/{job_id}")
    def convert_markdown_remove(job_id: int):
        """Remove a conversion from the list: cancel it if it is still queued or running, delete its files and forget the file name."""
        j = _own_conversion(job_id)
        payload = jloads(j["payload_json"], {})
        if j["state"] in ("queued", "running"):
            jobs.cancel(job_id, DB)
        j = jobs.get(conn(), job_id)
        # a running conversion still reads its upload: its worker deletes the folder when it stops (see work above)
        if j["state"] != "running" and (d := _conversion_dir(payload.get("dir") or (jloads(j["result_json"], None) or {}).get("dir"))):
            shutil.rmtree(d, ignore_errors=True)
        # keep the job row (its id must not be reused while a worker may still finish it) but drop what names the file
        scrubbed = {"user": payload.get("user"), "dir": payload.get("dir"), "hidden": True}
        conn().execute("UPDATE job SET payload_json=?, result_json=NULL, message=NULL, error=NULL WHERE id=?", (json.dumps(scrubbed), job_id))
        conn().commit()
        audit("Conversion removed from the To Markdown list", job_id=job_id)
        return {"ok": True}

    @app.get("/api/convert/markdown/jobs/{job_id}")
    def convert_markdown_result(job_id: int, download: bool = Query(False)):
        j = _own_conversion(job_id)
        if j["state"] != "done":
            raise HTTPException(409, f"the conversion is {j['state']}")
        res = jloads(j["result_json"], {})
        md_file = safe_join(_conversions_dir(), f"{res.get('dir', '')}/out.md")
        if md_file is None:
            raise HTTPException(410, "the converted file has expired; convert it again")
        markdown = md_file.read_text(encoding="utf-8")
        if download:
            return Response(markdown, media_type="text/markdown; charset=utf-8",
                            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(res['filename'])}"})
        return {**{k: v for k, v in res.items() if k != "dir"}, "markdown": markdown}

    # ------------------------------------------------------------------ compare two spec versions
    _summary_lock = threading.Lock()

    def _compare_or_404(cid: int) -> dict:
        c = compare.get_comparison(conn(), cid)
        if c is None:
            raise HTTPException(404, "comparison not found")
        return c

    @app.get("/api/compare/options")
    def compare_options():
        """Every spec in the data store, with the parse run that would be compared (the active one, else the newest successful one)."""
        out = []
        for d in rows(conn(), "SELECT * FROM spec_document ORDER BY family, id"):
            runs = rows(conn(), "SELECT id,active,status,parser_version FROM parse_run WHERE document_id=? AND status='done' ORDER BY active DESC, id DESC", (d["id"],))
            r = runs[0] if runs else None
            out.append({"doc_id": d["id"], "family": d["family"], "version": d["version"], "filename": d["filename"], "pages": d["pages"],
                        "run_id": r["id"] if r else None, "parsed": r is not None,
                        "outdated": bool(r and r["parser_version"] != config.PARSER_VERSION)})
        out.sort(key=lambda o: (o["family"], [(0, int(t), "") if t.isdigit() else (1, 0, t.lower()) for t in re.split(r"(\d+)", o["version"]) if t], o["doc_id"]))
        return out

    @app.post("/api/compare")
    def compare_run(body: dict = Body(...)):
        need(body, "from_run", "to_run")
        a, b = body["from_run"], body["to_run"]
        if any(not isinstance(x, int) or isinstance(x, bool) for x in (a, b)):
            raise HTTPException(422, "from_run and to_run must be parse run ids")
        try:
            cid = compare.run_comparison(conn(), a, b)
        except compare.CompareError as e:
            raise HTTPException(422, str(e)) from e
        c = _compare_or_404(cid)
        audit(f"Compared {c['summary']['from']['family']} {c['summary']['from']['version']} with {c['summary']['to']['version']}", compare_id=cid,
              changes=c["summary"]["total"])
        return c

    @app.get("/api/compare")
    def compare_list():
        return compare.list_comparisons(conn())

    @app.get("/api/compare/{cid}")
    def compare_get(cid: int):
        return _compare_or_404(cid)

    @app.get("/api/compare/{cid}/changes")
    def compare_changes(cid: int, kind: str | None = None, impact: str | None = None, change: str | None = None, chapter: str | None = None,
                        q: str | None = None, nested: bool = True, sort: str = Query("document", pattern="^(document|importance)$"),
                        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500),
                        ids: str | None = Query(None, pattern=r"^\d+(,\d+)*$", description="only these change ids (comma separated)")):
        _compare_or_404(cid)
        return compare.query_changes(conn(), cid, kind, impact, change, chapter, q, nested, offset, limit, sort,
                                     [int(x) for x in ids.split(",")] if ids else None)

    @app.get("/api/compare/{cid}/export")
    def compare_export(cid: int):
        _compare_or_404(cid)
        md = compare.export_markdown(conn(), cid)
        return Response(md, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": f"attachment; filename=\"spec-comparison-{cid}.md\""})

    @app.post("/api/compare/{cid}/summarize")
    def compare_summarize(cid: int, body: dict = Body(default={})):
        _compare_or_404(cid)
        with _summary_lock:            # the check and the reservation (the job row written by submit) must not interleave with another request
            return _start_summary(cid, body)

    def _start_summary(cid: int, body: Any) -> dict:
        busy = row(conn(), "SELECT id FROM job WHERE type='compare-summary' AND state IN ('queued','running') AND payload_json LIKE ?", (f'%"compare_id": {cid}}}%',))
        if busy:
            raise HTTPException(409, f"an AI summary of this comparison is already being written (job {busy['id']})")
        redo = bool(body.get("redo")) if isinstance(body, dict) else False
        st = settings()

        def work(c, prog):
            return compare_ai.summarize(c, cid, LLMService(c, st), redo=redo, progress=prog)
        audit(f"AI summary of comparison #{cid} requested", compare_id=cid, redo=redo)
        return {"job": jobs.submit(conn(), "compare-summary", work, {"compare_id": cid}, DB)}

    @app.delete("/api/compare/{cid}")
    def compare_delete(cid: int):
        if not compare.delete_comparison(conn(), cid):
            raise HTTPException(404, "comparison not found")
        return {"ok": True}

    # ------------------------------------------------------------------ specs
    @app.post("/api/specs/upload")
    def upload(files: list[UploadFile] = File(...)):   # sync on purpose: runs in the thread pool, never blocks the event loop
        out = []
        for f in files:
            suffix = Path(f.filename or "").suffix.lower()
            if suffix not in (".pdf", ".docx"):
                raise HTTPException(400, f"Unsupported file type: {f.filename}")
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=config.workspace_dir() / "uploads") as tmp:
                shutil.copyfileobj(f.file, tmp)
            try:
                d = store.register_document(conn(), tmp.name, filename=os.path.basename(f.filename or tmp.name))
            finally:
                os.unlink(tmp.name)
            out.append(d)
            audit(f"Uploaded {d.get('filename') or f.filename} as {d['family']} {d['version']}", document_id=d["id"], format=d["format"], pages=d.get("pages"))
        return out

    @app.get("/api/specs")
    def list_specs():
        docs = rows(conn(), "SELECT * FROM spec_document ORDER BY family, version, id")
        for d in docs:
            runs = rows(conn(), "SELECT id,parser_version,status,active,stats_json,audit_json,error,created FROM parse_run WHERE document_id=? ORDER BY id DESC", (d["id"],))
            for r in runs:
                r["stats"], r["audit"] = jloads(r.pop("stats_json"), {}), jloads(r.pop("audit_json"), {})
                r["outdated"] = r["parser_version"] != config.PARSER_VERSION
            d["runs"] = runs
        return docs

    @app.patch("/api/specs/{doc_id}")
    def patch_doc(doc_id: int, body: dict = Body(...)):
        fam, ver = body.get("family"), body.get("version")
        if not fam and not ver:
            raise HTTPException(400, "family or version required")
        conn().execute("UPDATE spec_document SET family=COALESCE(?,family), version=COALESCE(?,version) WHERE id=?", (fam, ver, doc_id))
        conn().commit()
        audit(f"Spec #{doc_id} relabelled", document_id=doc_id, family=fam, version=ver)
        return row(conn(), "SELECT * FROM spec_document WHERE id=?", (doc_id,))

    @app.delete("/api/specs/{doc_id}")
    def delete_doc(doc_id: int):
        conn().execute("DELETE FROM spec_document WHERE id=?", (doc_id,))
        conn().commit()
        audit(f"Deleted spec #{doc_id} and its parse runs", "warn", document_id=doc_id)
        return {"ok": True}

    @app.post("/api/specs/{doc_id}/parse")
    def parse(doc_id: int, activate: bool = Query(False)):
        if not row(conn(), "SELECT 1 AS x FROM spec_document WHERE id=?", (doc_id,)):
            raise HTTPException(404, "document not found")
        st = settings()

        def work(c, prog):
            rid = store.run_parse(c, doc_id, st, progress=lambda s, a, b: prog(f"{s} {a}/{b}", a, b), activate=True if activate else None)
            return {"run_id": rid}
        audit(f"Parse requested for spec #{doc_id}", document_id=doc_id, activate=activate)
        return {"job": jobs.submit(conn(), "parse", work, {"document_id": doc_id}, DB)}

    @app.post("/api/runs/{run_id}/activate")
    def activate(run_id: int):
        try:
            store.activate_run(conn(), run_id)
        except KeyError:
            raise HTTPException(404, "parse run not found")
        except ValueError as e:
            raise HTTPException(409, str(e))
        audit(f"Parse run #{run_id} set active", run_id=run_id)
        return {"ok": True}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int):
        r = row(conn(), "SELECT r.*, d.family, d.version, d.filename FROM parse_run r JOIN spec_document d ON d.id=r.document_id WHERE r.id=?", (run_id,))
        if not r:
            raise HTTPException(404)
        r["stats"], r["audit"] = jloads(r.pop("stats_json"), {}), jloads(r.pop("audit_json"), {})
        return r

    @app.get("/api/runs/{run_id}/tree")
    def tree(run_id: int):
        return rows(conn(), "SELECT key,number,title,parent_key,page_from,page_to,attrs_json FROM ir_node WHERE run_id=? AND kind='section' ORDER BY ord", (run_id,))

    @app.get("/api/runs/{run_id}/section")
    def section(run_id: int, key: str):
        s = row(conn(), "SELECT * FROM ir_node WHERE run_id=? AND key=? AND kind='section'", (run_id, key))
        if not s:
            raise HTTPException(404)
        tables = rows(conn(), "SELECT key,number,title,page_from,page_to,attrs_json FROM ir_node WHERE run_id=? AND kind='table' AND parent_key=? ORDER BY ord", (run_id, key))
        for t in tables:
            t.update(jloads(t.pop("attrs_json"), {}))
        reqs = rows(conn(), "SELECT key,text,page_from,attrs_json FROM ir_node WHERE run_id=? AND kind='requirement' AND parent_key=? ORDER BY ord", (run_id, key))
        for r in reqs:
            r.update(jloads(r.pop("attrs_json"), {}))
        refs_ = rows(conn(), "SELECT raw,kind,target_family,target_label FROM ir_ref WHERE run_id=? AND src_key=?", (run_id, key))
        s["attrs"] = jloads(s.pop("attrs_json"), {})
        return {"section": s, "tables": tables, "requirements": reqs, "references": refs_}

    @app.get("/api/runs/{run_id}/issues")
    def issues(run_id: int, severity: str | None = None):
        q, a = "SELECT * FROM parse_issue WHERE run_id=?", [run_id]
        if severity:
            q += " AND severity=?"
            a.append(severity)
        return rows(conn(), q + " ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id", a)

    @app.patch("/api/issues/{issue_id}")
    def patch_issue(issue_id: int, body: dict = Body(...)):
        conn().execute("UPDATE parse_issue SET state=?, note=? WHERE id=?", (body.get("state", "open"), body.get("note"), issue_id))
        conn().commit()
        audit(f"Parse issue #{issue_id} marked {body.get('state', 'open')}", issue_id=issue_id, note=body.get("note"))
        return {"ok": True}

    @app.put("/api/runs/{run_id}/tables/correction")
    def correct_table(run_id: int, body: dict = Body(...)):
        need(body, 'key', 'header', 'rows')
        if (not isinstance(body["key"], str) or not isinstance(body["header"], list)
                or not isinstance(body["rows"], list) or any(not isinstance(r, list) for r in body["rows"])):
            raise HTTPException(422, "key must be a string, header a list and rows a list of lists")
        r = row(conn(), "SELECT d.family,d.version FROM parse_run r JOIN spec_document d ON d.id=r.document_id WHERE r.id=?", (run_id,))
        if not r:
            raise HTTPException(404)
        store.set_correction(conn(), r["family"], r["version"], body["key"], "table_replace", {"header": body["header"], "rows": body["rows"]})
        audit(f"Table correction stored for {body['key']}", run_id=run_id, key=body["key"], rows=len(body["rows"]))
        return {"ok": True, "note": "Stored. Reparse to apply; corrections survive every reparse."}

    @app.get("/api/runs/{a}/diff/{b}")
    def diff(a: int, b: int):
        return store.diff_runs(conn(), a, b)

    @app.get("/api/documents/{doc_id}/file")
    def doc_file(doc_id: int):
        d = row(conn(), "SELECT * FROM spec_document WHERE id=?", (doc_id,))
        if not d:
            raise HTTPException(404)
        return FileResponse(d["path"], media_type="application/pdf" if d["format"] == "pdf" else "application/octet-stream", filename=d["filename"])

    # ------------------------------------------------------------------ spec sets
    @app.get("/api/sets")
    def list_sets():
        out = rows(conn(), "SELECT * FROM spec_set ORDER BY id")
        for s in out:
            s["members"] = refs.set_members(conn(), s["id"])
            s["catalog_items"] = conn().execute("SELECT COUNT(*) FROM catalog_item WHERE set_id=?", (s["id"],)).fetchone()[0]
            s["scenarios"] = conn().execute("SELECT COUNT(*) FROM scenario WHERE set_id=?", (s["id"],)).fetchone()[0]
            s["scenarios_approved"] = conn().execute("SELECT COUNT(*) FROM scenario WHERE set_id=? AND state='approved'", (s["id"],)).fetchone()[0]
            s["scenarios_draft"] = conn().execute("SELECT COUNT(*) FROM scenario WHERE set_id=? AND state='draft'", (s["id"],)).fetchone()[0]
            s["scenarios_executed"] = conn().execute("SELECT COUNT(*) FROM scenario WHERE set_id=? AND origin='exec'", (s["id"],)).fetchone()[0]
            s["links_proposed"] = conn().execute("SELECT COUNT(*) FROM scenario_item si JOIN scenario c ON c.id=si.scenario_id "
                                                 "WHERE c.set_id=? AND si.state='proposed'", (s["id"],)).fetchone()[0]
            s["analyses"] = conn().execute("SELECT COUNT(*) FROM exec_analysis WHERE set_id=?", (s["id"],)).fetchone()[0]
        return out

    @app.post("/api/sets")
    def create_set(body: dict = Body(...)):
        ids = body.get("document_ids", [])
        name = body.get("name")
        if not isinstance(ids, list) or not isinstance(name, str) or not name.strip():
            raise HTTPException(422, "name (string) and document_ids (list) are required")
        c = conn()
        try:   # all or nothing: never leave a half-created set or an open transaction on the shared connection
            cur = c.execute("INSERT INTO spec_set(name) VALUES(?)", (name,))
            for d in ids:
                c.execute("INSERT OR IGNORE INTO spec_set_member(set_id,document_id) VALUES(?,?)", (cur.lastrowid, d))
            c.commit()
        except Exception:
            c.rollback()
            raise HTTPException(409, "could not create the set (duplicate name or unknown document id)")
        audit(f"Spec set '{name}' created", set_id=cur.lastrowid, document_ids=ids)
        return {"id": cur.lastrowid}

    @app.put("/api/sets/{set_id}/members")
    def set_members(set_id: int, body: dict = Body(...)):
        ids = body.get("document_ids")
        if not isinstance(ids, list):
            raise HTTPException(422, "document_ids must be a list")
        c = conn()
        try:   # replace atomically: a failure must never leave the set empty or half replaced
            c.execute("DELETE FROM spec_set_member WHERE set_id=?", (set_id,))
            for d in ids:
                c.execute("INSERT INTO spec_set_member(set_id,document_id) VALUES(?,?) ON CONFLICT DO NOTHING", (set_id, d))
            c.commit()
        except Exception:
            c.rollback()
            raise HTTPException(409, "could not update the set members (unknown document id?)")
        audit(f"Members of set #{set_id} updated", set_id=set_id, document_ids=ids)
        return {"ok": True}

    @app.delete("/api/sets/{set_id}")
    def delete_set(set_id: int):
        # explicit deletes (children first) also cover databases created before the cascading foreign keys existed
        runs = "(SELECT id FROM coverage_run WHERE set_id=?)"
        conn().execute(f"DELETE FROM coverage_result WHERE run_id IN {runs}", (set_id,))
        conn().execute(f"DELETE FROM item_result WHERE run_id IN {runs}", (set_id,))
        conn().execute("DELETE FROM coverage_run WHERE set_id=?", (set_id,))
        conn().execute("DELETE FROM ref_choice WHERE set_id=?", (set_id,))
        conn().execute("DELETE FROM item_override WHERE set_id=?", (set_id,))
        conn().execute("DELETE FROM spec_set WHERE id=?", (set_id,))
        conn().commit()
        audit(f"Spec set #{set_id} deleted with its catalog, scenarios and coverage runs", "warn", set_id=set_id)
        return {"ok": True}

    @app.get("/api/sets/{set_id}/references")
    def references(set_id: int, state: str | None = None):
        try:
            rs = refs.resolve_references(conn(), set_id)
        except ValueError as e:
            raise HTTPException(409, str(e))
        out = [r for r in rs if not state or r["state"] == state]
        return {"summary": refs.summarize(rs), "references": out[:3000]}

    @app.post("/api/sets/{set_id}/references/choose")
    def choose_ref(set_id: int, body: dict = Body(...)):
        need(body, 'src_key', 'raw', 'chosen_key')
        refs.choose_reference(conn(), set_id, body["src_key"], body["raw"], body["chosen_key"])
        audit(f"Reference '{body['raw']}' resolved to {body['chosen_key']}", set_id=set_id, src_key=body["src_key"])
        return {"ok": True}

    # ------------------------------------------------------------------ catalog + scenarios
    @app.post("/api/sets/{set_id}/catalog/build")
    def build_cat(set_id: int):
        def work(c, prog):
            prog("building catalog", 0, 1)
            res = catalog.build_catalog(c, set_id)
            return {**res, "issues": res["issues"][:500]}
        audit(f"Catalog build requested for set #{set_id}", set_id=set_id)
        return {"job": jobs.submit(conn(), "catalog", work, {"set_id": set_id}, DB)}

    @app.get("/api/sets/{set_id}/catalog")
    def get_catalog(set_id: int, kind: str | None = None, command: str | None = None, q: str | None = None,
                    observability: str | None = None, family: str | None = None, version: str | None = None, limit: int = 500, offset: int = 0):
        sql, args = "SELECT * FROM catalog_item WHERE set_id=?", [set_id]
        if family:          # commands and requirements carry the spec family they came from
            sql += " AND json_extract(attrs_json, '$.family')=?"
            args.append(family)
        if version:         # a set may hold several revisions of one family
            sql += " AND json_extract(attrs_json, '$.version')=?"
            args.append(version)
        for col, val in (("kind", kind), ("observability", observability)):
            if val:
                sql += f" AND {col}=?"
                args.append(val)
        if command:
            sql += " AND (key=? OR key LIKE ? OR parent_key=?)"
            args += [command, command + "/%", command]
        if q:
            sql += " AND (label LIKE ? OR key LIKE ?)"
            args += [f"%{q}%", f"%{q}%"]
        total = conn().execute(sql.replace("SELECT *", "SELECT COUNT(*)"), args).fetchone()[0]
        out = rows(conn(), sql + " ORDER BY id LIMIT ? OFFSET ?", [*args, min(limit, 2000), offset])
        for i in out:
            i["attrs"] = jloads(i.pop("attrs_json"), {})
        return {"total": total, "items": out}

    @app.put("/api/sets/{set_id}/catalog/override")
    def override(set_id: int, body: dict = Body(...)):
        need(body, 'key')
        obs, reason = body.get("observability"), body.get("na_reason")
        if obs is not None and not (isinstance(obs, str) and obs in OBSERVABILITY):
            raise HTTPException(422, f"observability must be one of {sorted(OBSERVABILITY)} (or null to clear the override)")
        if reason is not None and not isinstance(reason, str):
            raise HTTPException(422, "na_reason must be a string")
        if not isinstance(body["key"], str):
            raise HTTPException(422, "key must be a string")
        catalog.set_override(conn(), set_id, body["key"], obs, reason)
        audit(f"Catalog override {'cleared' if obs is None and reason is None else 'set'} for {body['key']}", set_id=set_id, observability=obs, na_reason=reason)
        return {"ok": True}

    @app.get("/api/sets/{set_id}/scenarios")
    def list_scenarios(set_id: int, origin: str | None = None, state: str | None = None, q: str | None = None,
                       item: str | None = None, source_id: int | None = None, links: str | None = None, limit: int = 500, offset: int = 0):
        sql, args = "SELECT s.* FROM scenario s WHERE s.set_id=?", [set_id]
        if origin:
            sql += " AND s.origin=?"; args.append(origin)
        if source_id is not None:
            sql += " AND s.source_id=?"; args.append(source_id)
        if links:          # executed scenarios with requirement links in this review state
            sql += " AND s.id IN (SELECT scenario_id FROM scenario_item WHERE state=?)"; args.append(links)
        if state:
            sql += " AND s.state=?"; args.append(state)
        if q:      # an executed scenario is also found by any of its tests or commands
            sql += " AND (s.title LIKE ? OR s.sid LIKE ? OR (s.origin='exec' AND s.predicate_json LIKE ?))"; args += [f"%{q}%"] * 3
        if item:
            sql += " AND s.id IN (SELECT scenario_id FROM scenario_item WHERE item_key=?)"; args.append(item)
        total = conn().execute(sql.replace("SELECT s.*", "SELECT COUNT(*)"), args).fetchone()[0]
        out = rows(conn(), sql + " ORDER BY s.id LIMIT ? OFFSET ?", [*args, min(limit, 2000), offset])
        for s in out:
            s["predicate"] = jloads(s.pop("predicate_json"))
            s["provenance"] = jloads(s.pop("provenance_json"), {})
            lk = conn().execute("SELECT item_key,state FROM scenario_item WHERE scenario_id=?", (s["id"],)).fetchall()
            s["items"] = [r[0] for r in lk]
            s["link_counts"] = {}
            for r in lk:
                if r[1]:
                    s["link_counts"][r[1]] = s["link_counts"].get(r[1], 0) + 1
        return {"total": total, "scenarios": out}

    @app.get("/api/scenarios/{sid}/links")
    def scenario_links(sid: int):
        try:
            return execscen.links(conn(), sid)
        except KeyError:
            raise HTTPException(404)

    @app.put("/api/scenarios/{sid}/links")
    def review_link(sid: int, body: dict = Body(...)):
        need(body, "item_key", "state")
        if not (isinstance(body["item_key"], str) and isinstance(body["state"], str) and body["state"] in LINK_STATES):
            raise HTTPException(422, f"item_key must be a string and state one of {sorted(LINK_STATES)}")
        try:
            execscen.set_link(conn(), sid, body["item_key"], body["state"])
        except KeyError:
            raise HTTPException(404, "no such link")
        except ValueError as e:
            raise HTTPException(422, str(e))
        audit(f"Requirement link {body['item_key']} of scenario #{sid} set to {body['state']}", scenario_id=sid, item_key=body["item_key"], state=body["state"])
        return {"ok": True}

    @app.post("/api/scenarios/links/bulk")
    def review_links_bulk(body: dict = Body(...)):
        """Set every requirement link of the given executed scenarios that is in state `from` (default 'proposed') to `state`."""
        need(body, "ids", "state")
        ids, new_state, frm = body["ids"], body["state"], body.get("from", "proposed")
        if not isinstance(ids, list) or any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
            raise HTTPException(422, "ids must be a list of integers")
        if not (isinstance(new_state, str) and new_state in LINK_STATES and isinstance(frm, str) and frm in LINK_STATES):
            raise HTTPException(422, f"state and from must be one of {sorted(LINK_STATES)}")
        c = conn()
        n = 0
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            n += c.execute(f"UPDATE scenario_item SET state=? WHERE state=? AND scenario_id IN ({','.join('?' * len(part))})", [new_state, frm, *part]).rowcount
        c.commit()
        audit(f"Bulk link review: {n} requirement link(s) {frm} \u2192 {new_state}", scenarios=len(ids), links=n, state=new_state)
        return {"ok": True, "updated": n}

    @app.patch("/api/scenarios/{sid}")
    def patch_scenario(sid: int, body: dict = Body(...)):
        s = row(conn(), "SELECT * FROM scenario WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        if s["origin"] == "exec":
            if set(body) - {"title", "description"}:
                raise HTTPException(422, EXEC_READONLY)
            if any(not isinstance(body[k], str) for k in body):
                raise HTTPException(422, "title and description must be strings")
            prov = {**jloads(s["provenance_json"], {}), "edited": True}         # kept when the command DB is analysed again
            upd = {**body, "provenance_json": json.dumps(prov)}
            conn().execute(f"UPDATE scenario SET {', '.join(k + '=?' for k in upd)} WHERE id=?", [*upd.values(), sid])
            conn().commit()
            return {"ok": True, "state": s["state"]}
        # ---- validate everything first; nothing is written until the whole request is known to be good
        for col in ("title", "description"):
            if col in body and not isinstance(body[col], str):
                raise HTTPException(422, f"{col} must be a string")
        if "state" in body and not (isinstance(body["state"], str) and body["state"] in SCENARIO_STATES):
            raise HTTPException(422, f"state must be one of {sorted(SCENARIO_STATES)}")
        if "observability" in body and not (isinstance(body["observability"], str) and body["observability"] in OBSERVABILITY):
            raise HTTPException(422, f"observability must be one of {sorted(OBSERVABILITY)}")
        pred = jloads(s["predicate_json"])
        if "predicate" in body:
            cmds, fields = scenarios.catalog_context(conn(), s["set_id"])
            errs = scenarios.validate_predicate(body["predicate"], cmds, fields)
            if errs:
                raise HTTPException(422, {"errors": errs})
            # the scenario stays linked to its catalog items, so the edited predicate must still be about their commands
            new_cmds = {st.get("command") for st in body["predicate"]["steps"]}
            for item_key, kind, attrs in conn().execute(
                    "SELECT si.item_key, c.kind, c.attrs_json FROM scenario_item si LEFT JOIN catalog_item c "
                    "ON c.set_id=? AND c.key=si.item_key WHERE si.scenario_id=?", (s["set_id"], sid)).fetchall():
                linked = (item_key if kind == "command" else jloads(attrs, {}).get("command")) if kind else None
                if linked and linked not in new_cmds:
                    raise HTTPException(422, {"errors": [f"the predicate no longer refers to {linked}, which {item_key} is linked to; "
                                                         "create a new scenario for a different command"]})
            pred = body["predicate"]
        new_state = body.get("state", "draft" if ("predicate" in body and s["state"] == "approved") else s["state"])   # an edit invalidates approval
        if body.get("state") == "approved":
            cmds, fields = scenarios.catalog_context(conn(), s["set_id"])
            errs = scenarios.validate_predicate(pred, cmds, fields)
            if errs:
                raise HTTPException(422, {"errors": errs, "message": "Only valid predicates can be approved"})
        # ---- one atomic write
        upd: dict[str, Any] = {"state": new_state}
        if "predicate" in body:
            upd["predicate_json"] = json.dumps(pred)
        for col in ("title", "description", "observability"):
            if col in body:
                upd[col] = body[col]
        c = conn()
        try:
            c.execute(f"UPDATE scenario SET {', '.join(k + '=?' for k in upd)} WHERE id=?", [*upd.values(), sid])
            c.commit()
        except Exception:
            c.rollback()
            raise
        if new_state != s["state"] or "predicate" in body:
            audit(f"Scenario {s['sid']}: {s['state']} \u2192 {new_state}" + (" (predicate edited)" if "predicate" in body else ""), scenario_id=sid, origin=s["origin"])
        return {"ok": True, "state": new_state}

    @app.post("/api/scenarios/bulk")
    def bulk(body: dict = Body(...)):
        need(body, 'ids', 'state')
        ids, new_state = body["ids"], body["state"]
        if not isinstance(ids, list) or any(not isinstance(i, int) or isinstance(i, bool) for i in ids):
            raise HTTPException(422, "ids must be a list of integers")
        if not (isinstance(new_state, str) and new_state in SCENARIO_STATES):
            raise HTTPException(422, f"state must be one of {sorted(SCENARIO_STATES)}")
        updated, refused = [], []
        ctx: dict[int, tuple] = {}
        for sid in ids:
            s = row(conn(), "SELECT id,set_id,origin,predicate_json FROM scenario WHERE id=?", (sid,))
            if not s:
                refused.append({"id": sid, "errors": ["scenario not found"]})
                continue
            if s["origin"] == "exec":
                refused.append({"id": sid, "errors": [EXEC_READONLY]})
                continue
            if new_state == "approved":     # same rule as PATCH: only valid predicates can be approved
                if s["set_id"] not in ctx:
                    ctx[s["set_id"]] = scenarios.catalog_context(conn(), s["set_id"])
                errs = scenarios.validate_predicate(jloads(s["predicate_json"]), *ctx[s["set_id"]])
                if errs:
                    refused.append({"id": sid, "errors": errs})
                    continue
            updated.append(sid)
        c = conn()
        try:   # all accepted updates are applied together or not at all
            c.executemany("UPDATE scenario SET state=? WHERE id=?", [(new_state, i) for i in updated])
            c.commit()
        except Exception:
            c.rollback()
            raise
        audit(f"Bulk review: {len(updated)} scenario(s) set to {new_state}" + (f", {len(refused)} refused" if refused else ""),
              "warn" if refused else "info", state=new_state, updated=len(updated), refused=refused[:5])
        return {"ok": not refused, "updated": updated, "refused": refused}

    @app.post("/api/scenarios/validate")
    def validate(body: dict = Body(...)):
        need(body, 'set_id', 'predicate')
        cmds, fields = scenarios.catalog_context(conn(), body["set_id"])
        return {"errors": scenarios.validate_predicate(body["predicate"], cmds, fields)}

    def _selection(body: dict) -> tuple[list[str], list[str]]:
        out = []
        for name in ("section_keys", "command_keys"):
            v = body.get(name, [])
            if not (isinstance(v, list) and all(isinstance(k, str) for k in v)):
                raise HTTPException(422, f"{name} must be a list of strings")
            out.append(v)
        if not (out[0] or out[1]):
            raise HTTPException(422, "select at least one command or section")
        return out[0], out[1]

    @app.get("/api/sets/{set_id}/sections")
    def sections(set_id: int, q: str | None = None, limit: int = 400, family: str | None = None, offset: int = 0, version: str | None = None):
        try:
            return crossgen.list_sections(conn(), set_id, q, min(max(limit, 1), 2000), family or None, max(offset, 0), version or None)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/sets/{set_id}/scenarios/cross/plan")
    def cross_plan(set_id: int, body: dict = Body(...)):
        secs, cmds = _selection(body)
        try:
            return {"units": crossgen.plan(conn(), set_id, secs, cmds)}
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.get("/api/cross/angles")
    def cross_angles():
        """The kinds of scenario each pass over a section looks for, in order; `passes` N runs the first N."""
        return {"default": crossgen.DEFAULT_PASSES, "angles": [{"id": a["id"], "title": a["title"], "focus": a["focus"]} for a in crossgen.ANGLES]}

    @app.post("/api/sets/{set_id}/scenarios/cross")
    def cross_generate(set_id: int, body: dict = Body(...)):
        secs, cmds = _selection(body)
        focus, mx, passes = body.get("focus", ""), body.get("max_per_section", 6), body.get("passes", crossgen.DEFAULT_PASSES)
        if not isinstance(focus, str):
            raise HTTPException(422, "focus must be a string")
        if not isinstance(mx, int) or isinstance(mx, bool) or not 1 <= mx <= 20:
            raise HTTPException(422, "max_per_section must be an integer from 1 to 20")
        if not isinstance(passes, int) or isinstance(passes, bool) or not 1 <= passes <= len(crossgen.ANGLES):
            raise HTTPException(422, f"passes must be an integer from 1 to {len(crossgen.ANGLES)}")
        try:
            crossgen.build_units(conn(), set_id, secs, cmds)       # reject a bad selection now, not inside the job
        except ValueError as e:
            raise HTTPException(422, str(e))
        st = settings()

        def work(c, prog):
            return generate_cross_scenarios(c, set_id, LLMService(c, st), secs, cmds, focus, mx, progress=lambda a, b: prog("cross-command scenarios", a, b), passes=passes)
        audit(f"Cross-command scenario generation requested for set #{set_id}", set_id=set_id, sections=len(secs), commands=len(cmds), passes=passes, focus=focus or None)
        return {"job": jobs.submit(conn(), "cross-scenarios", work, {"set_id": set_id}, DB)}

    # ------------------------------------------------------------------ executed scenarios (derived from a command DB)
    def _exec_source(set_id: int, body: dict) -> int:
        need(body, "source_id")
        src = body["source_id"]
        if not isinstance(src, int) or isinstance(src, bool):
            raise HTTPException(422, "source_id must be an integer")
        s = row(conn(), "SELECT status FROM cmd_source WHERE id=?", (src,))
        if not s:
            raise HTTPException(404, "command DB not found")
        if s["status"] != "imported":
            raise HTTPException(409, "import the command DB first")
        if not row(conn(), "SELECT 1 AS x FROM catalog_item WHERE set_id=? LIMIT 1", (set_id,)):
            raise HTTPException(409, "build the catalog of this spec set first")
        return src

    @app.get("/api/sets/{set_id}/exec")
    def exec_analyses(set_id: int, source_id: int | None = None):
        """The last analysis of each command DB against this set (or of one)."""
        sql, args = "SELECT source_id FROM exec_analysis WHERE set_id=?", [set_id]
        if source_id is not None:
            sql += " AND source_id=?"; args.append(source_id)
        return [{"source_id": r["source_id"], **(execscen.analysis(conn(), set_id, r["source_id"]) or {})} for r in rows(conn(), sql, args)]

    @app.post("/api/sets/{set_id}/exec/analyze")
    def exec_analyze(set_id: int, body: dict = Body(...)):
        src = _exec_source(set_id, body)

        def work(c, prog):
            return execscen.analyze_execution(c, set_id, src, progress=lambda a, b: prog("analysing executed commands", a, b))
        audit(f"Execution analysis requested (set #{set_id}, command DB #{src})", set_id=set_id, source_id=src)
        return {"job": jobs.submit(conn(), "exec-analyze", work, {"set_id": set_id, "source_id": src}, DB)}

    @app.post("/api/sets/{set_id}/exec/describe")
    def exec_describe(set_id: int, body: dict = Body(...)):
        src = _exec_source(set_id, body)
        ids, redo = body.get("scenario_ids"), body.get("redo", False)
        if ids is not None and (not isinstance(ids, list) or not ids or any(not isinstance(i, int) or isinstance(i, bool) for i in ids)):
            raise HTTPException(422, "scenario_ids must be a non-empty list of integers (leave it out for all scenarios)")
        if not isinstance(redo, bool):
            raise HTTPException(422, "redo must be true or false")
        if not row(conn(), "SELECT 1 AS x FROM scenario WHERE set_id=? AND origin='exec' AND source_id=? LIMIT 1", (set_id, src)):
            raise HTTPException(409, "analyse the command DB first")
        st = settings()

        def work(c, prog):
            return execscen.describe_scenarios(c, set_id, src, LLMService(c, st), ids, redo, progress=lambda a, b: prog("describing executed scenarios", a, b))
        audit(f"AI description of executed scenarios requested (set #{set_id}, command DB #{src})", set_id=set_id, source_id=src,
              scenarios=len(ids) if ids else "all undescribed" if not redo else "all")
        return {"job": jobs.submit(conn(), "exec-describe", work, {"set_id": set_id, "source_id": src}, DB)}

    # ------------------------------------------------------------------ command DBs
    @app.get("/api/cmddb/suggest")
    def suggest(path: str):
        try:
            return cmddb.suggest_mapping(path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))

    @app.post("/api/cmddb")
    def add_source(body: dict = Body(...)):
        need(body, 'path')
        try:
            sid = cmddb.register_source(conn(), body["path"], body.get("mapping"), body.get("name"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e))
        audit(f"Command database registered: {body['path']}", source_id=sid)
        return {"id": sid}

    @app.get("/api/cmddb")
    def list_sources():
        out = rows(conn(), "SELECT id,name,path,sha256,row_count,decoded_count,imported_at,status,mapping_json,unknown_json FROM cmd_source ORDER BY id DESC")
        for s in out:
            s["mapping"], s["unknown"] = jloads(s.pop("mapping_json")), jloads(s.pop("unknown_json"), {})
        return out

    @app.put("/api/cmddb/{sid}/mapping")
    def put_mapping(sid: int, body: dict = Body(...)):
        conn().execute("UPDATE cmd_source SET mapping_json=?, status='new' WHERE id=?", (json.dumps(body), sid))
        conn().commit()
        audit(f"Column mapping updated for command database #{sid}", source_id=sid)
        return {"ok": True}

    @app.post("/api/cmddb/{sid}/import")
    def import_db(sid: int):
        st = settings()

        def work(c, prog):
            return cmddb.import_source(c, sid, st.get("mi_layout"), progress=lambda n: prog(f"decoded {n} rows", n, 0))
        audit(f"Import requested for command database #{sid}", source_id=sid)
        return {"job": jobs.submit(conn(), "import-cmddb", work, {"source_id": sid}, DB)}

    @app.get("/api/cmddb/{sid}/commands")
    def browse(sid: int, opcode: int | None = None, queue: str | None = None, interface: str | None = None, test: str | None = None,
               limit: int = 200, offset: int = 0):
        sql, args = "SELECT d.id,d.src_row_id,d.test_id,d.seq,d.interface,d.queue,d.cs,d.opcode,d.nsid,g.dwords_json FROM decoded_command d JOIN cmd_signature g ON g.source_id=d.source_id AND g.signature=d.signature WHERE d.source_id=?", [sid]
        for col, val in (("d.opcode", opcode), ("d.queue", queue), ("d.interface", interface), ("d.test_id", test)):
            if val is not None:
                sql += f" AND {col}=?"
                args.append(val)
        out = rows(conn(), sql + " ORDER BY d.id LIMIT ? OFFSET ?", [*args, min(limit, 1000), offset])
        for r in out:
            dw = jloads(r.pop("dwords_json"))
            if r["interface"] == "pcie":   # commands are de-duplicated on a signature that ignores CID and pointers: don't show one row's values for another
                dw = {k: v for k, v in dw.items() if k not in ("4", "5", "6", "7", "8", "9")}
                if "0" in dw:
                    dw["0"] &= 0xFFFF
            r["dwords"] = dw
        return out

    # ------------------------------------------------------------------ coverage + reports
    @app.post("/api/coverage/run")
    def run_cov(body: dict = Body(...)):
        need(body, 'set_id', 'source_id')
        def work(c, prog):
            rid = coverage.run_coverage(c, body["set_id"], body["source_id"], progress=lambda a, b: prog("matching scenarios", a, b))
            return {"run_id": rid}
        audit(f"Coverage run requested (set #{body['set_id']}, command DB #{body['source_id']})", set_id=body["set_id"], source_id=body["source_id"])
        return {"job": jobs.submit(conn(), "coverage", work, body, DB)}

    @app.get("/api/coverage/runs")
    def cov_runs(set_id: int | None = None):
        sql, args = "SELECT id,set_id,source_id,created,summary_json FROM coverage_run", []
        if set_id:
            sql += " WHERE set_id=?"; args.append(set_id)
        out = rows(conn(), sql + " ORDER BY id DESC LIMIT 100", args)
        for r in out:
            s = jloads(r.pop("summary_json"), {})
            r["overall"] = s.get("overall")
        return out

    @app.get("/api/coverage/{run_id}")
    def cov_summary(run_id: int):
        r = row(conn(), "SELECT * FROM coverage_run WHERE id=?", (run_id,))
        if not r:
            raise HTTPException(404)
        return {"run": {k: r[k] for k in ("id", "set_id", "source_id", "created")}, "meta": jloads(r["meta_json"], {}), "summary": jloads(r["summary_json"], {})}

    @app.get("/api/coverage/{run_id}/items")
    def cov_items(run_id: int, state: str | None = None, kind: str | None = None, command: str | None = None, q: str | None = None,
                  limit: int = 500, offset: int = 0):
        # reads the run's own snapshot (item_result), so historical runs are unaffected by later catalog rebuilds
        sql = "FROM item_result i WHERE i.run_id=?"
        args: list[Any] = [run_id]
        if state:
            sql += " AND i.state=?"; args.append(state)
        if kind:
            sql += " AND i.kind=?"; args.append(kind)
        if command:
            sql += " AND (i.item_key=? OR i.item_key LIKE ? OR i.parent_key=?)"; args += [command, command + "/%", command]
        if q:
            sql += " AND (i.label LIKE ? OR i.item_key LIKE ?)"; args += [f"%{q}%"] * 2
        total = conn().execute("SELECT COUNT(*) " + sql, args).fetchone()[0]
        out = rows(conn(), "SELECT i.item_key AS key,i.kind,i.parent_key,i.label,i.observability,i.na_reason,i.attrs_json,i.state,i.covered,i.total,i.detail_json "
                           + sql + " ORDER BY i.rowid LIMIT ? OFFSET ?", [*args, min(limit, 2000), offset])
        for i in out:
            a = jloads(i.pop("attrs_json"), {})
            i["detail"] = jloads(i.pop("detail_json"), {})
            i["attrs"] = {k: a[k] for k in ("command", "dw", "hi", "lo", "vlo", "vhi", "family", "text", "strength") if k in a}
        return {"total": total, "items": out}

    @app.get("/api/coverage/{run_id}/evidence")
    def cov_evidence(run_id: int, item: str):
        # scenarios and their links are read from the run's own snapshot, not from the live (editable) tables
        it = row(conn(), "SELECT detail_json FROM item_result WHERE run_id=? AND item_key=?", (run_id, item))
        ids = (jloads(it["detail_json"], {}) if it else {}).get("scenario_ids", [])
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        out = rows(conn(), f"""SELECT scenario_id AS id,sid,title,review_state AS state,observability,state AS result,match_count,evidence_json
                               FROM coverage_result WHERE run_id=? AND scenario_id IN ({q}) ORDER BY scenario_id""", [run_id, *ids])
        for s in out:
            s["evidence"] = jloads(s.pop("evidence_json"), [])
        return out

    @app.get("/api/coverage/{a}/diff/{b}")
    def cov_diff(a: int, b: int):
        return coverage.diff_runs(conn(), a, b)

    @app.get("/api/coverage/{run_id}/export")
    def cov_export(run_id: int, format: str = "xlsx", which: str = "gaps"):
        try:
            if format == "csv":
                data, mime, name = reports.to_csv(reports.build_dataset(conn(), run_id), which), "text/csv", f"specforge-{which}-run{run_id}.csv"
            else:
                data, mime, name = reports.export(conn(), run_id, format)
        except (KeyError, ValueError) as e:
            raise HTTPException(404, str(e))
        return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs(state: str | None = None):
        if state == "active":      # every queued/running job, however old (recent() is capped)
            return jobs.active(conn())
        return jobs.recent(conn())

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        j = jobs.get(conn(), job_id)
        if not j:
            raise HTTPException(404)
        j["result"] = jloads(j.pop("result_json"), None)
        return j

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        jobs.cancel(job_id, DB)
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: int):
        async def gen():
            last = None
            while True:
                j = await asyncio.to_thread(lambda: jobs.get(get_conn(), job_id))
                if j is None:
                    yield "event: error\ndata: {}\n\n"
                    return
                snap = (j["state"], j["progress"], j["total"], j["message"])
                if snap != last:
                    last = snap
                    j["result"] = jloads(j.pop("result_json"), None)
                    yield f"data: {json.dumps(j, default=str)}\n\n"
                if j["state"] in ("done", "failed", "cancelled"):
                    return
                await asyncio.sleep(0.4)
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ------------------------------------------------------------------ activity log
    def _split(v: str | None) -> list[str] | None:
        return [x for x in v.split(",") if x] if v else None

    @app.get("/api/logs")
    def get_logs(after_id: int | None = None, before_id: int | None = None, limit: int = 300, level: str | None = None,
                 source: str | None = None, job_id: int | None = None, q: str | None = None):
        eventlog.drain(1.0)       # include events queued a moment ago
        srcs = _split(source)
        # the watermark is read BEFORE the history: an event committed in between is then delivered by the live stream instead of being skipped
        newest = conn().execute("SELECT COALESCE(MAX(id),0) FROM event_log").fetchone()[0]
        items = eventlog.query(conn(), after_id=after_id, before_id=before_id, limit=limit, level=level, sources=srcs, job_id=job_id, q=q)
        has_older = False
        if after_id is None and items:       # older events that match the SAME filters, not merely any older row
            has_older = bool(eventlog.query(conn(), before_id=items[0]["id"], limit=1, level=level, sources=srcs, job_id=job_id, q=q))
        return {"items": items, "newest_id": newest, "has_older": has_older, "sources": list(eventlog.SOURCES)}

    @app.get("/api/logs/stream")
    async def stream_logs(after_id: int | None = None, level: str | None = None, source: str | None = None, job_id: int | None = None):
        srcs = _split(source)

        async def gen():
            last = after_id if after_id is not None else (await asyncio.to_thread(lambda: get_conn().execute("SELECT COALESCE(MAX(id),0) FROM event_log").fetchone()[0]))
            idle = 0
            while True:
                # read the newest id BEFORE querying: an event inserted in between is picked up next time instead of skipped
                newest = await asyncio.to_thread(lambda: get_conn().execute("SELECT COALESCE(MAX(id),0) FROM event_log").fetchone()[0])
                items = await asyncio.to_thread(lambda: eventlog.query(get_conn(), after_id=last, limit=500, level=level, sources=srcs, job_id=job_id))
                if items:
                    last = items[-1]["id"]
                    idle = 0
                    yield f"data: {json.dumps(items, default=str)}\n\n"
                else:
                    last = max(last, newest)         # rows hidden by the filters: step past them so the next poll stays cheap
                    idle += 1
                    if idle % 40 == 0:
                        yield ": keep-alive\n\n"
                await asyncio.sleep(0.4)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.delete("/api/logs")
    def clear_logs():
        eventlog.drain(1.0)
        n = conn().execute("DELETE FROM event_log").rowcount
        conn().commit()
        eventlog.emit("info", "system", f"Activity log cleared ({n} events)")
        return {"ok": True, "deleted": n}

    # ------------------------------------------------------------------ static UI
    for cand in (Path(__file__).resolve().parents[1] / "web", Path(__file__).resolve().parents[3] / "frontend" / "dist"):
        if (cand / "index.html").exists():
            app.mount("/assets", StaticFiles(directory=cand / "assets"), name="assets") if (cand / "assets").exists() else None

            @app.get("/{path:path}", include_in_schema=False)
            def spa(path: str, _root=cand):
                f = safe_join(_root, path)
                if f is not None:
                    return FileResponse(f)
                return FileResponse(_root / "index.html")
            break
    return app


app = create_app()
