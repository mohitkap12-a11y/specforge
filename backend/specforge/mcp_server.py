"""The SpecForge MCP server: read-only specification lookups for coding agents (OpenCode, Roo Code, Claude Code ...).

Served by `specforge serve` at /mcp (Streamable HTTP, stateless, JSON responses) and /mcp/sse (the older HTTP+SSE transport, for
clients that predate Streamable HTTP), and by `specforge mcp` over stdio. The tools are thin wrappers over specquery.SpecLibrary;
this module adds what a shared endpoint needs: the admin on/off switch, a per-address rate limit, and usage figures for the
admin MCP page (calls per tool, errors, misses, slow calls, the addresses calling it). No sign-in: the endpoint is open to
everyone who can reach the server, and only reads the parsed specifications (the one thing it starts is a version comparison
that has not been computed yet, through the job queue).
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import threading
import time
from collections import deque
from typing import Any, Callable

from . import __version__, eventlog
from .specquery import NotFound, QueryError, SpecLibrary, summarize_args

SLOW_MS = 2000
RECENT = 200
TOOL_THREADS = 8            # MCP tools run on their own threads: a burst of agent calls cannot take the web UI's threads
MAX_CLIENTS = 5000          # client addresses remembered; idle ones (a day) are forgotten first
LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}

INSTRUCTIONS = """SpecForge serves the NVM Express specifications parsed on this server (Base, NVM / ZNS / KV command sets, NVMe-MI,
transports, ECNs ... every version that was uploaded). Answers are Markdown that cite the specification, version, section or
figure and page; quote those citations when you rely on an answer.

Pick the most specific tool:
- get_command_field: one Command Dword or field of a command, with the value tables it refers to
  (e.g. command="Identify", dword="10" -> CNS / CNTID and the CNS values).
- get_command / list_commands: a command's opcode, section and Command Dword tables.
- get_structure: a data structure, log page or descriptor, optionally one byte offset or field
  (e.g. name="Identify Controller", field="MDTS").
- diff / list_changes: what changed between two versions (default: latest vs the version before it).
- search_spec: anything else; then get_section / get_table on the hits. resolve_reference follows "Figure 312" or
  "section 5.2.1 of the NVM Command Set Specification".
- read_raw: the converted Markdown of a document, for text the structured tools miss.
A specification is named by id (nvme-base), short name (base, mi, nvm, zns) or title; without a version the latest parsed
version is used and the answer says which. Long answers are paged: pass page=2, 3 ... as the answer instructs."""

client_ip: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_client_ip", default=None)


class Usage:
    """Usage figures since the server started (in memory), for the admin MCP page."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._clear()

    def _clear(self) -> None:
        self.started = time.time()
        self.tools: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        self.recent: deque[dict] = deque(maxlen=RECENT)
        self.misses: deque[dict] = deque(maxlen=RECENT)
        self.slow: deque[dict] = deque(maxlen=RECENT)
        self.errors: deque[dict] = deque(maxlen=RECENT)
        self.rate_limited = 0
        self.refused_disabled = 0

    def reset(self) -> None:
        with self.lock:
            self._clear()

    def seen(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            if ip not in self.clients and len(self.clients) >= MAX_CLIENTS:
                idle = [k for k, c in self.clients.items() if now - c.get("last_seen", 0) > 86400]
                for k in idle or sorted(self.clients, key=lambda k: self.clients[k].get("last_seen", 0))[:MAX_CLIENTS // 10]:
                    self.clients.pop(k, None)
            c = self.clients.setdefault(ip, {"ip": ip, "first_seen": now, "requests": 0, "calls": 0, "limited": 0})
            c["requests"] += 1
            c["last_seen"] = now

    def limited(self, ip: str) -> None:
        with self.lock:
            self.rate_limited += 1
            if ip in self.clients:
                self.clients[ip]["limited"] += 1

    def record(self, tool: str, args: dict, ms: float, outcome: str, error: str | None = None) -> dict:
        ip = client_ip.get() or "local"
        ev = {"ts": time.time(), "tool": tool, "args": summarize_args(args), "ms": round(ms, 1), "outcome": outcome, "ip": ip,
              **({"error": error[:300]} if error else {})}
        with self.lock:
            t = self.tools.setdefault(tool, {"tool": tool, "calls": 0, "errors": 0, "misses": 0, "slow": 0, "total_ms": 0.0, "max_ms": 0.0})
            t["calls"] += 1
            t["total_ms"] += ms
            t["max_ms"] = max(t["max_ms"], ms)
            t["last"] = ev["ts"]
            if outcome == "error":
                t["errors"] += 1
                self.errors.appendleft(ev)
            elif outcome == "miss":
                t["misses"] += 1
                self.misses.appendleft(ev)
            if ms >= SLOW_MS:
                t["slow"] += 1
                self.slow.appendleft(ev)
            self.recent.appendleft(ev)
            if ip in self.clients:
                self.clients[ip]["calls"] += 1
        return ev

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            tools = [{**t, "avg_ms": round(t["total_ms"] / t["calls"], 1) if t["calls"] else 0.0, "total_ms": round(t["total_ms"], 1),
                      "max_ms": round(t["max_ms"], 1)} for t in self.tools.values()]
            now = time.time()
            clients = sorted(({**c, "active": now - c.get("last_seen", 0) < 900} for c in self.clients.values()),
                             key=lambda c: -c.get("last_seen", 0))
            return {"since": self.started, "tools": sorted(tools, key=lambda t: -t["calls"]), "clients": clients[:500],
                    "client_count": len(clients), "active_clients": sum(1 for c in clients if c["active"]),
                    "calls": sum(t["calls"] for t in tools), "errors": sum(t["errors"] for t in tools), "misses": sum(t["misses"] for t in tools),
                    "rate_limited": self.rate_limited, "refused_disabled": self.refused_disabled,
                    "recent": list(self.recent)[:50], "recent_misses": list(self.misses)[:50], "recent_slow": list(self.slow)[:50],
                    "recent_errors": list(self.errors)[:50]}


class RateLimiter:
    """Sliding one-minute window per client address."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits: dict[str, deque[float]] = {}

    def allow(self, key: str, per_minute: int) -> bool:
        if per_minute <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= per_minute:
                return False
            q.append(now)
            if len(self.hits) > 10_000:          # forget idle addresses
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 60][:5000]:
                    self.hits.pop(k, None)
            return True


def build_server(lib: SpecLibrary, usage: Usage | None = None, log_calls: Callable[[], bool] = lambda: True):
    """The MCP server with its tools and resources; transports are attached by the caller (HTTP in the API app, or stdio)."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    import anyio

    usage = usage or Usage()
    limiter: list[anyio.CapacityLimiter] = []

    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        """fn on the MCP tool threads (not the thread pool the web UI's routes share)."""
        if not limiter:
            limiter.append(anyio.CapacityLimiter(TOOL_THREADS))
        return await anyio.to_thread.run_sync(functools.partial(fn, *args), limiter=limiter[0])

    async def wait_comparison(spec: str, from_version: str | None, to_version: str | None) -> str | None:
        """Start (or join) the comparison a diff needs and wait for it without holding a thread: None when it is ready,
        else what to tell the agent."""
        d_from, d_to = await run(lib._pair, spec, from_version, to_version)
        state, n = await run(lib.start_comparison, d_from, d_to)
        deadline = time.monotonic() + lib.compare_wait_s
        while state == "running":
            if await run(lib.poll_comparison, n, d_from, d_to) is not None:
                return None
            if time.monotonic() >= deadline:
                return lib._computing(d_from, d_to)
            await anyio.sleep(0.5)
        return None

    server = MCPServer(name="specforge", title="SpecForge specifications", version=__version__, instructions=INSTRUCTIONS, log_level="WARNING",
                       description="Read-only lookups in the NVMe / NVMe-MI specifications parsed by SpecForge")
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def tool(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register fn as a tool that records its use, turns lookup failures into tool errors the agent can read, and treats
        'nothing found' as a miss."""
        is_async = inspect.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def wrapped(**kwargs: Any) -> str:
            t0 = time.monotonic()
            outcome, err = "ok", None
            try:
                res = await fn(**kwargs) if is_async else await run(functools.partial(fn, **kwargs))
                if isinstance(res, tuple):          # (markdown, hit count) from search
                    res, n = res
                    outcome = "ok" if n else "miss"
                return res
            except NotFound as e:
                outcome, err = "miss", str(e)
                raise ToolError(str(e)) from None
            except QueryError as e:
                outcome, err = "error", str(e)
                raise ToolError(str(e)) from None
            except Exception as e:  # noqa: BLE001
                outcome, err = "error", f"{type(e).__name__}: {e}"
                eventlog.emit("error", "mcp", f"MCP tool {fn.__name__} failed: {err[:300]}", tool=fn.__name__, args=summarize_args(kwargs))
                raise ToolError(f"internal error in {fn.__name__}: {e}") from None
            finally:
                ms = (time.monotonic() - t0) * 1000
                ev = usage.record(fn.__name__, kwargs, ms, outcome, err)
                if log_calls() or outcome == "error":
                    eventlog.emit("warn" if outcome == "error" else "info", "mcp",
                                  f"MCP {fn.__name__} {ev['args']} → {outcome} in {ms:.0f} ms", **{k: v for k, v in ev.items() if k != "ts"})
        server.add_tool(wrapped, name=fn.__name__, annotations=read_only, structured_output=False)
        return fn

    @tool
    def list_specs() -> str:
        """List every parsed specification and version on this server (Base, command sets, NVMe-MI, transports, ECNs ...), with
        the names to pass as `spec` / `version` to the other tools."""
        return lib.list_specs()

    @tool
    def search_spec(query: str, spec: str | None = None, version: str | None = None, kinds: str | None = None, limit: int = 25):
        """Keyword search over sections, figures (tables) and table rows. NVMe abbreviations are expanded (CDW10, CNS, NSID,
        LBAF ...). Without `spec`, searches the latest version of every specification; version="all" searches every version.
        kinds: comma list of section, table, row. Returns hits with section / figure numbers to open with get_section or get_table."""
        return lib.search(query, spec, version, kinds, limit)

    @tool
    def get_section(section: str, spec: str | None = None, version: str | None = None, depth: int = 0, include_tables: bool = True,
                    page: int = 1) -> str:
        """One section by number ("5.2.1") or title, with its text, its tables and the list of its sub-sections.
        depth=1.. also includes sub-sections that many levels down."""
        return lib.section(spec, version, section, depth, include_tables, page)

    @tool
    def get_table(figure: str, spec: str | None = None, version: str | None = None, page: int = 1) -> str:
        """One figure / table by number ("Figure 103", "103") or caption, as a Markdown table with its section and page."""
        return lib.table(spec, version, figure, page)

    @tool
    def list_commands(spec: str | None = None, version: str | None = None) -> str:
        """Every command of a specification: name, opcode (Admin / I/O / MI), section and its Command Dword tables."""
        return lib.list_commands(spec, version)

    @tool
    def get_command(command: str, spec: str | None = None, version: str | None = None, detail: str = "full", page: int = 1) -> str:
        """A command by name ("Identify", "Get Log Page") or opcode ("06h", "admin 06h"): opcode, section text, every Command Dword
        table, its other tables, and where other specifications extend it. Without `spec`, looks in every specification.
        detail="brief" lists the tables instead of printing them."""
        return lib.command(command, spec, version, detail, page)

    @tool
    def get_command_field(command: str, dword: str | None = None, field: str | None = None, spec: str | None = None,
                          version: str | None = None, page: int = 1) -> str:
        """One Command Dword ("10", "CDW10", "dword 11", "NMD0") and/or one field ("CNS", "NUMDL", "Save") of a command, with the
        tables the field descriptions refer to (e.g. the CNS values for Identify CDW10) and what the command's text says about the field."""
        return lib.command_field(command, dword, field, spec, version, page)

    @tool
    def get_structure(name: str, spec: str | None = None, version: str | None = None, field: str | None = None, offset: str | None = None,
                      page: int = 1) -> str:
        """A data structure, log page or descriptor by name ("Identify Controller", "SMART / Health Information") or figure,
        optionally narrowed to the row holding a byte `offset` (decimal, or hex like "4Dh") or a `field` ("MDTS")."""
        return lib.structure(name, spec, version, field, offset, page)

    @tool
    def resolve_reference(text: str, spec: str | None = None, version: str | None = None, page: int = 1) -> str:
        """Follow a reference as written in the spec text: "Figure 312", "section 5.2.1", "refer to section 3.1 of the NVM Express
        NVM Command Set Specification". `spec` / `version` say which document the text comes from."""
        return lib.reference(text, spec, version, page)

    @tool
    async def list_changes(spec: str, from_version: str | None = None, to_version: str | None = None, impact: str | None = None,
                     kind: str | None = None, query: str | None = None, page: int = 1) -> str:
        """What changed between two versions of a specification (default: the latest version vs the version before it): commands
        and data structures added / modified / removed, then every change ranked by impact (breaking, new, changed, clarification,
        editorial). Filter with impact, kind (section | table) or query. Computed on the server the first time, then kept."""
        if msg := await wait_comparison(spec, from_version, to_version):
            return msg
        return await run(lib.changes, spec, from_version, to_version, impact, kind, query, page)

    @tool
    async def diff(spec: str, item: str, from_version: str | None = None, to_version: str | None = None, page: int = 1) -> str:
        """The exact changes to one item between two versions (default: latest vs previous): a command ("Identify"), a data structure,
        a section ("5.2") or a figure ("Figure 103"); sentences added / removed / reworded with their shall / should level, table rows
        added / removed / changed with old and new values."""
        if msg := await wait_comparison(spec, from_version, to_version):
            return msg
        return await run(lib.diff, spec, item, from_version, to_version, page)

    @tool
    def read_raw(spec: str, version: str | None = None, around: str | None = None, context: int = 40, page: int = 1) -> str:
        """The converted (Docling) Markdown of a document, for text the structured tools do not cover. With `around`, only the lines
        around each occurrence of that phrase (`context` lines either side); otherwise the whole document, paged.
        `spec` may also be the file name of a document that was converted but not parsed."""
        return lib.raw(spec, version, around, context, page)

    @server.resource("specforge://specs", name="specs", title="Parsed specifications", mime_type="text/markdown",
                     description="Every parsed specification and version on this server")
    def specs_resource() -> str:
        return lib.list_specs()

    @server.resource("specforge://{spec}/{node}", name="spec-node", title="Section or figure", mime_type="text/markdown",
                     description="A section (sec:5.2.1) or figure (fig:103) of one version: specforge://nvme-base@2.4/sec:5.2")
    def node_resource(spec: str, node: str) -> str:
        try:
            return lib.resource(spec, node)
        except QueryError as e:
            raise ValueError(str(e)) from None

    server.usage = usage
    return server


class McpEndpoint:
    """ASGI app for every path under /mcp: the admin switch, the rate limit and the client list, in front of the Streamable HTTP
    transport (/mcp) and the HTTP+SSE transport (/mcp/sse, /mcp/messages/)."""

    def __init__(self, lib: SpecLibrary, settings: Callable[[], dict], ip_of: Callable[[Any], str], usage: Usage | None = None,
                 local_only: bool = False):
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.routing import Mount, Router

        self.lib, self.settings, self.ip_of = lib, settings, ip_of
        self.local_only = local_only        # sign-in off: the server listens on localhost only, so must every request's Host / Origin
        self.usage = usage or Usage()
        self.limiter = RateLimiter()
        self._cfg: tuple[float, dict] = (0.0, {})
        self.server = build_server(lib, self.usage, log_calls=lambda: bool(self.cfg().get("log_calls", True)))
        open_ = TransportSecuritySettings(enable_dns_rebinding_protection=False)    # reached by the server's intranet name
        self.http_app = self.server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                                        transport_security=open_, host="0.0.0.0")
        self.sse_router = Router(routes=[Mount("/mcp", app=self.server.sse_app(sse_path="/sse", message_path="/messages/",
                                                                              transport_security=open_, host="0.0.0.0"))])
        self.running = False

    def cfg(self) -> dict:
        """The 'mcp' settings, re-read at most every 2 s (every request passes here)."""
        at, c = self._cfg
        if time.monotonic() - at > 2:
            try:
                c = self.settings().get("mcp") or {}
                self.lib.compare_wait_s = float(c.get("compare_wait_s", 60) or 60)
            except Exception:  # noqa: BLE001  a busy database must not take the endpoint down
                pass
            self._cfg = (time.monotonic(), c)
        return c

    def refresh(self) -> None:
        self._cfg = (0.0, {})

    @staticmethod
    def _loopback(req) -> bool:
        from urllib.parse import urlsplit

        host = urlsplit("//" + (req.headers.get("host") or "")).hostname
        origin = req.headers.get("origin")
        return host in LOOPBACK_NAMES and (not origin or urlsplit(origin).hostname in LOOPBACK_NAMES)

    @contextlib.asynccontextmanager
    async def lifespan(self):
        async with self.server.session_manager.run():
            self.running = True
            try:
                yield
            finally:
                self.running = False

    async def _reply(self, send, status: int, message: str) -> None:
        body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": message}}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"),
                                                                                   (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        from starlette.requests import Request

        path = scope.get("path", "")
        req = Request(scope)
        if self.local_only and not self._loopback(req):
            # a web page whose host name was rebound to 127.0.0.1 must not reach a local, sign-in-free server
            return await self._reply(send, 403, "Refused: without sign-in this endpoint only answers requests addressed to localhost.")
        ip = self.ip_of(req)
        cfg = self.cfg()
        if not cfg.get("enabled", True):
            self.usage.refused_disabled += 1
            return await self._reply(send, 503, "The SpecForge MCP endpoint is turned off by an administrator.")
        if scope["method"] == "POST" and not self.limiter.allow(ip, int(cfg.get("rate_limit_per_min", 120) or 0)):
            self.usage.limited(ip)
            return await self._reply(send, 429, f"Rate limit: at most {cfg.get('rate_limit_per_min')} MCP requests per minute per "
                                                "address. Wait a moment and retry.")
        self.usage.seen(ip)
        tok = client_ip.set(ip)
        try:
            if path in ("/mcp", "/mcp/"):
                if not self.running:
                    return await self._reply(send, 503, "The MCP endpoint is starting; retry in a moment.")
                return await self.http_app(scope | {"path": "/mcp"}, receive, send)
            return await self.sse_router(scope, receive, send)
        finally:
            client_ip.reset(tok)


def run_stdio(db_file: str | None = None) -> None:
    """`specforge mcp`: the same tools over stdio, against a local workspace."""
    eventlog.configure({"logging": {"min_level": "warn"}})
    build_server(SpecLibrary(db_file), log_calls=lambda: False).run("stdio")
