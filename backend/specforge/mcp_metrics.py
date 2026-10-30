"""Call history of the MCP endpoint (table mcp_call) and the figures the admin Metrics, Misses & errors and Call log tabs show.

Every tool call is kept with what is needed to find out later why it went wrong: the full arguments, the error text, a reason
(what kind of miss or error it was), a fingerprint that groups the same failure, the client address, the agent (name and version
from the MCP initialize handshake, else its User-Agent), the transport, and the size of the answer. Rate-limited and refused
requests are kept too (tool ''), so the protection figures survive a restart.

Rows are written by one background thread (CallLog), never on the request's own thread: recording a call must not slow it down
or fail it. Old rows are pruned after `history_days` (MCP settings, default 90).
"""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import queue
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable, Iterable

from .core.config import db_path
from .core.db import connect

OUTCOMES = ("ok", "miss", "error")
REFUSALS = ("rate_limited", "refused")
ISSUE_STATES = ("open", "investigating", "fixed", "ignored")
SLOW_MS = 2000
SESSION_GAP = 600              # seconds without a call from the same client and agent that end an agent session
MAX_QUEUE = 20000
MAX_ROWS = 2_000_000           # hard cap besides the age limit
PRUNE_EVERY = 2000
MAX_ARGS = 8000                # characters of argument JSON kept per call
MAX_ERROR = 2000
# arguments that change how much of an answer is shown, not what was asked: left out of a miss's fingerprint
PRESENTATION_ARGS = {"page", "limit", "depth", "context", "include_tables", "detail", "kinds"}

COLUMNS = ("ts", "tool", "outcome", "ms", "args_json", "error", "reason", "fingerprint", "ip", "agent", "agent_version", "user_agent",
           "transport", "spec", "version", "answer_chars", "page", "paged")

# (pattern on the NotFound / QueryError text, reason). The first match wins.
REASONS = [
    (r"no specification has been parsed", "library empty"),
    (r"no parsed specification matches", "unknown specification"),
    (r"matches several specifications|name the specification", "specification ambiguous"),
    (r"version .* is not parsed|has version|no parsed version of .* is older|only one version", "version not parsed"),
    (r"has no section|no section of", "section not found"),
    (r"no row of figure", "table row not found"),
    (r"has no figure|no figure of", "figure not found"),
    (r"opcode .* is used by several", "command ambiguous"),
    (r"no command named", "command not found"),
    (r"no field|command dword tables|has no .* table in", "command field not found"),
    (r"no data structure", "structure not found"),
    (r"no section, figure or specification reference", "reference not understood"),
    (r"that changed between", "item did not change"),
    (r"does not occur|has no docling markdown|is not one of them", "text not found in document"),
    (r"still being indexed", "index still building"),
    (r"searching converted documents is turned off", "library search off"),
    (r"only \d+ page\(s\)", "page out of range"),
    (r"give a query", "empty query"),
    (r"could not search", "search query rejected"),
]


def reason_of(tool: str, outcome: str, error: str | None) -> str | None:
    """What kind of miss or error a call was, for grouping and filtering ('section not found', 'internal: OperationalError' ...)."""
    if outcome == "ok":
        return None
    if outcome in REFUSALS:
        return outcome.replace("_", " ")
    if not error:
        return "no search hits" if tool == "search_spec" else "nothing found"
    low = error.lower()
    if outcome == "error" and (m := re.match(r"([A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Timeout)\w*): ", error)):
        return f"internal: {m.group(1)}"
    for pat, why in REASONS:
        if re.search(pat, low):
            return why
    return "invalid request" if outcome == "error" else "nothing found"


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().lower()


def fingerprint(tool: str, outcome: str, reason: str | None, args: dict | None, error: str | None) -> str | None:
    """Groups calls that failed the same way: a miss by the tool and what was asked (case and spacing ignored, paging left out);
    an error by the tool, its reason and its message with numbers and quoted values masked."""
    if outcome == "ok":
        return None
    if outcome == "miss":
        key = json.dumps({k: _norm(v) for k, v in sorted((args or {}).items()) if v not in (None, "") and k not in PRESENTATION_ARGS})
    else:
        msg = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", error or "")
        key = re.sub(r"\d+", "#", msg)[:300]
    return hashlib.sha1(f"{tool}|{outcome}|{reason}|{key}".encode()).hexdigest()[:16]


def agent_from_user_agent(ua: str | None) -> tuple[str | None, str | None]:
    """'claude-code/2.1.3 (cli)' -> ('claude-code', '2.1.3'); a bare runtime (node, undici, python-httpx) is not an agent name."""
    if not ua:
        return None, None
    m = re.match(r"\s*([A-Za-z][\w.\-]*)(?:/([\w.\-+]+))?", ua)
    if not m:
        return None, None
    name = m.group(1)
    if name.lower() in {"node", "undici", "node-fetch", "python-httpx", "python-requests", "aiohttp", "mozilla", "curl", "go-http-client"}:
        return f"unknown ({name.lower()})", None
    return name, m.group(2)


def answer_meta(answer: Any) -> tuple[int | None, int]:
    """(characters of the answer, 1 when it was cut into pages and more remain)."""
    if answer is None:
        return None, 0
    s = str(answer)
    return len(s), int(bool(re.search(r"Call again with page=\d+ for the rest", s[-300:])))


def make_row(tool: str, outcome: str, ms: float, args: dict | None, error: str | None, *, ip: str, agent: str | None = None,
             agent_version: str | None = None, user_agent: str | None = None, transport: str | None = None, answer: Any = None,
             ts: float | None = None) -> dict:
    args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    reason = reason_of(tool, outcome, error)
    chars, paged = answer_meta(answer)
    page = args.get("page")
    try:
        args_json = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        args_json = json.dumps({"_repr": repr(args)})
    return {"ts": ts if ts is not None else time.time(), "tool": tool, "outcome": outcome, "ms": round(ms, 1),
            "args_json": args_json[:MAX_ARGS], "error": (error or None) and error[:MAX_ERROR], "reason": reason,
            "fingerprint": fingerprint(tool, outcome, reason, args, error), "ip": ip, "agent": agent, "agent_version": agent_version,
            "user_agent": (user_agent or None) and user_agent[:200], "transport": transport,
            "spec": str(args["spec"])[:120] if args.get("spec") else None, "version": str(args["version"])[:40] if args.get("version") else None,
            "answer_chars": chars, "page": page if isinstance(page, int) else None, "paged": paged}


class CallLog:
    """Queues call rows and writes them from one background thread (like eventlog). `db`: the workspace database (None: the
    current workspace's, resolved when written). `keep_days`: read on every pruning pass."""

    def __init__(self, db: str | None = None, keep_days: Callable[[], float] = lambda: 90) -> None:
        self.db = db
        self.keep_days = keep_days
        self.q: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.accepted = 0
        self.finished = 0
        self.inserted = 0

    def add(self, row: dict) -> None:
        """Never blocks and never raises: the call it records has already been answered."""
        try:
            with self.lock:
                self.accepted += 1
                if self.q.qsize() >= MAX_QUEUE:          # a stuck database: drop the oldest rather than grow without bound
                    try:
                        self.q.get_nowait()
                        self.finished += 1
                    except queue.Empty:
                        pass
                self.q.put(row)
                if self.thread is None or not self.thread.is_alive():
                    self.thread = threading.Thread(target=self._run, name="sf-mcp-calls", daemon=True)
                    self.thread.start()
        except Exception:  # noqa: BLE001
            pass

    def drain(self, timeout: float = 5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.finished >= self.accepted:
                    return True
            time.sleep(0.02)
        return False

    def _run(self) -> None:
        conn: sqlite3.Connection | None = None
        while True:
            try:
                batch = [self.q.get(timeout=1.0)]
            except queue.Empty:
                continue
            time.sleep(0.05)                               # let a burst accumulate into one transaction
            while len(batch) < 500:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            for attempt in range(20):
                try:
                    conn = conn or connect(self.db or str(db_path()))
                    conn.executemany(f"INSERT INTO mcp_call({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))})",
                                     [tuple(r.get(c) for c in COLUMNS) for r in batch])
                    conn.commit()
                    self.inserted += len(batch)
                    if self.inserted >= PRUNE_EVERY:
                        self.inserted = 0
                        prune(conn, self.keep_days())
                    break
                except sqlite3.OperationalError as e:
                    with contextlib.suppress(Exception):
                        conn and conn.close()
                    conn = None
                    if "locked" not in str(e) and "busy" not in str(e):
                        break
                    time.sleep(min(0.2 * (attempt + 1), 2))
                except Exception:  # noqa: BLE001  e.g. the workspace was removed (tests): give the rows up
                    conn = None
                    break
            with self.lock:
                self.finished += len(batch)


def prune(conn: sqlite3.Connection, keep_days: float) -> None:
    try:
        conn.execute("DELETE FROM mcp_call WHERE ts < ?", (time.time() - max(1.0, float(keep_days)) * 86400,))
        conn.execute("DELETE FROM mcp_call WHERE id <= (SELECT MAX(id) FROM mcp_call) - ?", (MAX_ROWS,))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()


# ----------------------------------------------------------------------------------------------------------------- queries

def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


def _where(since: float | None, until: float | None, f: dict) -> tuple[str, list]:
    sql, args = " WHERE 1=1", []
    if since is not None:
        sql += " AND ts >= ?"; args.append(since)
    if until is not None:
        sql += " AND ts < ?"; args.append(until)
    for key, col in (("tool", "tool"), ("agent", "agent"), ("ip", "ip"), ("transport", "transport"), ("fingerprint", "fingerprint"),
                     ("reason", "reason"), ("spec", "spec")):
        vals = [v for v in (f.get(key) or []) if v]
        if vals:
            sql += f" AND COALESCE({col},'') IN ({','.join('?' * len(vals))})"; args += vals
    outs = [o for o in (f.get("outcome") or []) if o in OUTCOMES + REFUSALS]
    if outs:
        sql += f" AND outcome IN ({','.join('?' * len(outs))})"; args += outs
    if f.get("q"):
        sql += " AND (args_json LIKE ? OR error LIKE ? OR tool LIKE ?)"; args += [f"%{f['q']}%"] * 3
    if f.get("slow"):
        sql += " AND ms >= ?"; args.append(SLOW_MS)
    return sql, args


def _bucket_size(span: float) -> int:
    for limit, size in ((2 * 3600, 300), (2 * 86400, 3600), (8 * 86400, 3 * 3600), (32 * 86400, 86400)):
        if span <= limit:
            return size
    return 86400


def _kpis(rows: list[sqlite3.Row]) -> dict:
    calls = [r for r in rows if r["outcome"] in OUTCOMES]
    n = len(calls)
    by = Counter(r["outcome"] for r in rows)
    ms = [r["ms"] for r in calls]
    chars = [r["answer_chars"] for r in calls if r["answer_chars"] is not None]
    return {"calls": n, "ok": by["ok"], "misses": by["miss"], "errors": by["error"], "rate_limited": by["rate_limited"],
            "refused": by["refused"], "answered_pct": round(100 * by["ok"] / n, 1) if n else None,
            "miss_pct": round(100 * by["miss"] / n, 1) if n else None, "error_pct": round(100 * by["error"] / n, 1) if n else None,
            "clients": len({r["ip"] for r in calls}), "agents": len({r["agent"] for r in calls if r["agent"]}),
            "p50_ms": _pct(ms, 50), "p95_ms": _pct(ms, 95), "p99_ms": _pct(ms, 99), "avg_ms": round(sum(ms) / n, 1) if n else None,
            "slow": sum(1 for x in ms if x >= SLOW_MS), "avg_chars": round(sum(chars) / len(chars)) if chars else None,
            "p95_chars": _pct([float(c) for c in chars], 95), "chars_total": sum(chars),
            "paged_pct": round(100 * sum(r["paged"] or 0 for r in calls) / n, 1) if n else None}


def _sessions(calls: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    """Calls (oldest first) cut into agent sessions: one client address and agent, no gap over SESSION_GAP."""
    open_: dict[tuple, list] = {}
    out: list[list] = []
    for r in calls:
        k = (r["ip"], r["agent"] or "")
        s = open_.get(k)
        if s and r["ts"] - s[-1]["ts"] <= SESSION_GAP:
            s.append(r)
        else:
            s = open_[k] = [r]
            out.append(s)
    return out


def _chains(sessions: list[list[sqlite3.Row]]) -> list[dict]:
    """The most common runs of 2 and 3 consecutive tools in a session, with how often the last call of the run was answered."""
    seen: Counter = Counter()
    good: Counter = Counter()
    for s in sessions:
        tools = [r["tool"] for r in s]
        for n in (2, 3):
            for i in range(len(s) - n + 1):
                key = tuple(tools[i:i + n])
                seen[key] += 1
                good[key] += s[i + n - 1]["outcome"] == "ok"
    out = [{"chain": list(k), "count": c, "answered_pct": round(100 * good[k] / c, 1), "retry": len(set(k)) == 1}
           for k, c in seen.most_common(60) if c >= 2]
    out.sort(key=lambda x: (-x["count"], len(x["chain"])))
    return out[:12]


def metrics(conn: sqlite3.Connection, since: float, until: float, filters: dict, tz_minutes: int = 0) -> dict:
    """Everything the Metrics tab draws for one time range and filter, plus the previous range of the same length for deltas."""
    span = max(until - since, 60.0)
    cols = "id, ts, tool, outcome, ms, ip, agent, agent_version, transport, spec, version, answer_chars, paged, reason, fingerprint"
    w, a = _where(since, until, filters)
    rows = conn.execute(f"SELECT {cols} FROM mcp_call{w} ORDER BY ts", a).fetchall()
    pw, pa = _where(since - span, since, filters)
    prev = conn.execute(f"SELECT outcome, ms, ip, agent, answer_chars, paged FROM mcp_call{pw}", pa).fetchall()
    calls = [r for r in rows if r["outcome"] in OUTCOMES]

    size = _bucket_size(span)
    off = tz_minutes * 60
    first = int((since + off) // size) * size - off
    nb = int((until - first) // size) + 1
    buckets = [{"t": first + i * size, "ok": 0, "miss": 0, "error": 0, "refused": 0, "_ms": []} for i in range(nb)]
    heat = [[0] * 24 for _ in range(7)]
    for r in rows:
        b = buckets[min(nb - 1, max(0, int((r["ts"] - first) // size)))]
        if r["outcome"] in OUTCOMES:
            b[r["outcome"]] += 1
            b["_ms"].append(r["ms"])
            lt = time.gmtime(r["ts"] + off)
            heat[lt.tm_wday][lt.tm_hour] += 1
        else:
            b["refused"] += 1
    series = [{**{k: v for k, v in b.items() if k != "_ms"}, "p50_ms": _pct(b["_ms"], 50), "p95_ms": _pct(b["_ms"], 95)} for b in buckets]

    def group(key: Callable[[sqlite3.Row], Any], extra: Callable[[list], dict] | None = None, limit: int = 50) -> list[dict]:
        g: dict[Any, list] = defaultdict(list)
        for r in calls:
            g[key(r)].append(r)
        out = []
        for k, rs in g.items():
            ms = [r["ms"] for r in rs]
            c = Counter(r["outcome"] for r in rs)
            out.append({"key": k, "calls": len(rs), "ok": c["ok"], "misses": c["miss"], "errors": c["error"],
                        "miss_pct": round(100 * c["miss"] / len(rs), 1), "error_pct": round(100 * c["error"] / len(rs), 1),
                        "p50_ms": _pct(ms, 50), "p95_ms": _pct(ms, 95), "max_ms": max(ms), "last": max(r["ts"] for r in rs),
                        **(extra(rs) if extra else {})})
        out.sort(key=lambda x: -x["calls"])
        return out[:limit]

    def tool_extra(rs: list) -> dict:
        chars = [r["answer_chars"] for r in rs if r["answer_chars"] is not None]
        return {"slow": sum(1 for r in rs if r["ms"] >= SLOW_MS), "avg_chars": round(sum(chars) / len(chars)) if chars else None,
                "paged": sum(r["paged"] or 0 for r in rs)}

    def client_extra(rs: list) -> dict:
        return {"first": min(r["ts"] for r in rs), "agents": sorted({_agent_label(r) for r in rs})[:5]}

    tools = group(lambda r: r["tool"], tool_extra)
    for t in tools:
        t["tool"] = t.pop("key")
    agents = group(lambda r: (r["agent"] or "unknown", r["agent_version"] or ""))
    for x in agents:
        x["agent"], x["version"] = x.pop("key")
    clients = group(lambda r: r["ip"], client_extra, limit=200)
    for x in clients:
        x["ip"] = x.pop("key")
    limited = Counter(r["ip"] for r in rows if r["outcome"] == "rate_limited")
    for x in clients:
        x["rate_limited"] = limited.get(x["ip"], 0)
        x["active"] = until - x["last"] < 900 and until >= time.time() - 60
    specs = group(lambda r: (r["spec"] or "(any / latest)", r["version"] or ""), limit=30)
    for x in specs:
        x["spec"], x["version"] = x.pop("key")
    transports = Counter(r["transport"] or "unknown" for r in calls)
    reasons = Counter((r["outcome"], r["reason"]) for r in calls if r["outcome"] != "ok")

    sess = _sessions(calls)
    lens = [len(s) for s in sess]
    durs = [s[-1]["ts"] - s[0]["ts"] for s in sess]
    recovered = _recovery(sess)
    return {"since": since, "until": until, "bucket_s": size, "kpis": {**_kpis(rows), "sessions": len(sess),
            "calls_per_session_p50": _pct([float(x) for x in lens], 50), "calls_per_session_p95": _pct([float(x) for x in lens], 95),
            "session_s_p50": _pct(durs, 50), **recovered}, "previous": _kpis(prev), "series": series, "heatmap": heat,
            "tools": tools, "agents": agents, "clients": clients, "specs": specs,
            "transports": [{"transport": k, "calls": v} for k, v in transports.most_common()],
            "reasons": [{"outcome": o, "reason": r or "", "calls": n} for (o, r), n in reasons.most_common(30)],
            "chains": _chains(sess)}


def _agent_label(r: sqlite3.Row) -> str:
    return " ".join(x for x in (r["agent"] or "unknown", r["agent_version"] or "") if x)


def _recovery(sessions: list[list[sqlite3.Row]]) -> dict:
    """Of the misses and errors, the share after which the agent still got an answer later in the same session."""
    fails = rec = 0
    for s in sessions:
        last_ok = max((i for i, r in enumerate(s) if r["outcome"] == "ok"), default=-1)
        for i, r in enumerate(s):
            if r["outcome"] != "ok":
                fails += 1
                rec += i < last_ok
    return {"recovered_pct": round(100 * rec / fails, 1) if fails else None}


def issues(conn: sqlite3.Connection, since: float, until: float, filters: dict, status: str | None = None, limit: int = 200) -> list[dict]:
    """Misses and errors grouped by fingerprint, most frequent first, with the admin's triage state and note."""
    f = {**filters, "outcome": [o for o in (filters.get("outcome") or ["miss", "error"]) if o in ("miss", "error")] or ["miss", "error"]}
    w, a = _where(since, until, f)
    groups = conn.execute(
        f"SELECT fingerprint, tool, outcome, reason, COUNT(*) n, MIN(ts) first, MAX(ts) last, MAX(id) last_id, COUNT(DISTINCT ip) clients, "
        f"COUNT(DISTINCT COALESCE(agent,'')) agents, AVG(ms) avg_ms FROM mcp_call{w} GROUP BY fingerprint ORDER BY n DESC LIMIT 2000", a).fetchall()
    if not groups:
        return []
    states = {r["fingerprint"]: dict(r) for r in conn.execute("SELECT * FROM mcp_issue")}
    out = []
    for g in groups:
        st = states.get(g["fingerprint"]) or {}
        state = st.get("status") or "open"
        if status and status != "all" and state != status:
            continue
        out.append({**{k: g[k] for k in ("fingerprint", "tool", "outcome", "reason", "first", "last", "clients", "agents")},
                    "count": g["n"], "avg_ms": round(g["avg_ms"] or 0, 1), "status": state, "note": st.get("note"),
                    "updated": st.get("updated"), "updated_by": st.get("updated_by"), "_last_id": g["last_id"]})
        if len(out) >= limit:
            break
    ids = [x.pop("_last_id") for x in out]
    sample = {r["id"]: r for r in conn.execute(f"SELECT id, args_json, error, agent, agent_version FROM mcp_call WHERE id IN ({','.join('?' * len(ids))})", ids)}
    for x, i in zip(out, ids):
        s = sample.get(i)
        x["args"] = json.loads(s["args_json"]) if s and s["args_json"] else {}
        x["error"] = s["error"] if s else None
        x["last_agent"] = " ".join(v for v in (s["agent"], s["agent_version"]) if v) if s else None
    return out


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["args"] = json.loads(d.pop("args_json") or "{}")
    return d


def calls(conn: sqlite3.Connection, filters: dict, since: float | None = None, until: float | None = None, before_id: int | None = None,
          limit: int = 100) -> dict:
    w, a = _where(since, until, filters)
    total = conn.execute(f"SELECT COUNT(*) FROM mcp_call{w}", a).fetchone()[0]
    if before_id is not None:
        w += " AND id < ?"; a = [*a, before_id]
    items = [_row(r) for r in conn.execute(f"SELECT * FROM mcp_call{w} ORDER BY id DESC LIMIT ?", [*a, max(1, min(limit, 500))])]
    return {"total": total, "items": items, "next_before": items[-1]["id"] if len(items) == limit else None}


def issue_detail(conn: sqlite3.Connection, fp: str, since: float, until: float, tz_minutes: int = 0) -> dict | None:
    """One group of misses or errors: when it happened, the argument and message variants, who hit it, the latest calls, and what
    each agent did next in the same session (did it recover, and how)."""
    rows = conn.execute("SELECT * FROM mcp_call WHERE fingerprint = ? AND ts >= ? AND ts < ? ORDER BY ts", (fp, since, until)).fetchall()
    if not rows:
        rows = conn.execute("SELECT * FROM mcp_call WHERE fingerprint = ? ORDER BY ts DESC LIMIT 500", (fp,)).fetchall()[::-1]
        if not rows:
            return None
    st = conn.execute("SELECT * FROM mcp_issue WHERE fingerprint = ?", (fp,)).fetchone()
    days: Counter = Counter(time.strftime("%Y-%m-%d", time.gmtime(r["ts"] + tz_minutes * 60)) for r in rows)
    variants = Counter(r["args_json"] for r in rows)
    errors = Counter(r["error"] for r in rows if r["error"])
    agents = Counter(_agent_label(r) for r in rows)
    clients = Counter(r["ip"] for r in rows)
    latest = rows[-50:][::-1]
    nxt = []
    for r in latest[:20]:
        n = conn.execute("SELECT * FROM mcp_call WHERE ip = ? AND COALESCE(agent,'') = ? AND ts > ? AND ts <= ? AND outcome IN ('ok','miss','error') "
                         "AND id != ? ORDER BY ts, id LIMIT 3", (r["ip"], r["agent"] or "", r["ts"], r["ts"] + SESSION_GAP, r["id"])).fetchall()
        nxt.append({"call": _row(r), "next": [_row(x) for x in n]})
    first = rows[0]
    recovered = sum(1 for x in nxt if any(c["outcome"] == "ok" for c in x["next"]))
    return {"fingerprint": fp, "tool": first["tool"], "outcome": first["outcome"], "reason": first["reason"], "count": len(rows),
            "first": rows[0]["ts"], "last": rows[-1]["ts"], "status": st["status"] if st else "open", "note": st["note"] if st else None,
            "updated": st["updated"] if st else None, "updated_by": st["updated_by"] if st else None,
            "per_day": [{"day": d, "count": c} for d, c in sorted(days.items())],
            "variants": [{"args": json.loads(k or "{}"), "count": c} for k, c in variants.most_common(20)],
            "errors": [{"error": k, "count": c} for k, c in errors.most_common(10)],
            "agents": [{"agent": k, "count": c} for k, c in agents.most_common(10)],
            "clients": [{"ip": k, "count": c} for k, c in clients.most_common(10)],
            "p50_ms": _pct([r["ms"] for r in rows], 50), "recovered": recovered, "followed": len(nxt), "next": nxt}


def set_issue(conn: sqlite3.Connection, fp: str, status: str | None, note: str | None, user: str | None) -> dict:
    cur = conn.execute("SELECT * FROM mcp_issue WHERE fingerprint = ?", (fp,)).fetchone()
    status = status or (cur["status"] if cur else "open")
    note = note if note is not None else (cur["note"] if cur else None)
    conn.execute("INSERT INTO mcp_issue(fingerprint, status, note, updated, updated_by) VALUES(?,?,?,datetime('now'),?) "
                 "ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status, note=excluded.note, updated=excluded.updated, "
                 "updated_by=excluded.updated_by", (fp, status, note, user))
    conn.commit()
    return dict(conn.execute("SELECT * FROM mcp_issue WHERE fingerprint = ?", (fp,)).fetchone())


CSV_COLUMNS = ("id", "time_utc", "tool", "outcome", "reason", "ms", "args", "error", "ip", "agent", "agent_version", "transport", "spec",
               "version", "answer_chars", "page", "paged", "fingerprint", "user_agent")


def csv_lines(conn: sqlite3.Connection, filters: dict, since: float | None, until: float | None, limit: int = 100_000) -> Iterable[str]:
    w, a = _where(since, until, filters)
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(CSV_COLUMNS)
    yield buf.getvalue()
    cur = conn.execute(f"SELECT * FROM mcp_call{w} ORDER BY id DESC LIMIT ?", [*a, limit])
    while chunk := cur.fetchmany(1000):
        buf.seek(0); buf.truncate()
        for r in chunk:
            out.writerow([r["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"])), r["tool"], r["outcome"], r["reason"] or "", r["ms"],
                          r["args_json"] or "", r["error"] or "", r["ip"], r["agent"] or "", r["agent_version"] or "", r["transport"] or "",
                          r["spec"] or "", r["version"] or "", r["answer_chars"] if r["answer_chars"] is not None else "", r["page"] or "",
                          r["paged"] or 0, r["fingerprint"] or "", r["user_agent"] or ""])
        yield buf.getvalue()

