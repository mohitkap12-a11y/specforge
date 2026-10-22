import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { ArrowDown, Bot, ChevronDown, ChevronRight, Cog, Copy, Download, Layers, Pause, Play, Trash2, X } from "lucide-react";
import { api, logStreamUrl } from "../api";
import { Badge, Empty, ErrorNote, Loading, PageHeader, Segmented } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useDebounced } from "../hooks";
import { bufferPaused, trimRows } from "../util";
import type { Job, LogEvent, LogLevel } from "../types";

const MAX_ROWS = 4000;       // kept while following the live tail
const HARD_MAX_ROWS = 20000; // ceiling while the user is reading history
const LENSES = {
  all: { label: "All", sources: "" },
  process: { label: "Processing", sources: "job,parser,catalog,scenarios,import,exec,coverage" },
  ai: { label: "AI agents", sources: "agent,llm" },
  app: { label: "App", sources: "api,system" },
} as const;
type Lens = keyof typeof LENSES;
const LEVEL_OPTS: { value: LogLevel; label: string }[] = [{ value: "debug", label: "Debug" }, { value: "info", label: "Info" }, { value: "warn", label: "Warnings" }, { value: "error", label: "Errors" }];

const clock = (ts: string) => {
  const d = new Date(ts);
  return `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
};
const SKIP = new Set(["prompt", "response", "traceback"]);

function Detail({ e }: { e: LogEvent }) {
  const toast = useToast();
  const d = e.data;
  const copy = () => navigator.clipboard?.writeText(JSON.stringify(e, null, 2)).then(() => toast.push({ title: "Copied event JSON", tone: "ok" }), () => {});
  const entries = Object.entries(d).filter(([k]) => !SKIP.has(k));
  return (
    <div className="logdetail">
      <div className="row small" style={{ marginBottom: 8 }}>
        <span className="muted">#{e.id} · {new Date(e.ts).toLocaleString()}</span><span className="grow" />
        <button className="sm" onClick={copy}><Copy size={12} /> Copy JSON</button>
      </div>
      {entries.length > 0 && (
        <dl className="kv" style={{ margin: 0 }}>
          {entries.map(([k, v]) => (
            <Fragment key={k}>
              <dt>{k}</dt>
              <dd>{v !== null && typeof v === "object" ? <pre style={{ margin: 0, maxHeight: 220 }}>{JSON.stringify(v, null, 1)}</pre> : String(v)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
      {Array.isArray(d.prompt) && (
        <>
          <h3>Prompt sent to the model</h3>
          {d.prompt.map((m: { role: string; content: string }, i: number) => (
            <div key={i} className="promptmsg"><header>{m.role}</header><pre>{m.content}</pre></div>
          ))}
        </>
      )}
      {typeof d.response === "string" && <><h3>Model reply</h3><div className="promptmsg"><header>assistant</header><pre>{d.response}</pre></div></>}
      {typeof d.traceback === "string" && <><h3>Traceback</h3><pre>{d.traceback}</pre></>}
      {d.prompt === undefined && e.source === "llm" && e.message.includes("request") && <p className="muted small">Prompt capture is off. Turn it on in Settings → Logging to see prompts and replies.</p>}
    </div>
  );
};

const Row = memo(function Row({ e, open, onToggle, onJob }: { e: LogEvent; open: boolean; onToggle: (id: number) => void; onJob: (id: number) => void }) {
  const has = Object.keys(e.data).length > 0;
  return (
    <div className={`logrow lv-${e.level} ${has ? "has" : ""} ${open ? "open" : ""}`} onClick={() => has && onToggle(e.id)} role={has ? "button" : undefined} aria-expanded={has ? open : undefined}>
      <span className="ts">{clock(e.ts)}</span>
      <span className={`dot ${e.level === "error" ? "bad" : e.level === "warn" ? "warn" : e.level === "info" ? "info" : ""}`} title={e.level} style={{ alignSelf: "center", justifySelf: "center" }} />
      <span className={`src s-${e.source}`}>{e.source}</span>
      <span className="msg">{has && (open ? <ChevronDown size={12} style={{ verticalAlign: "-1px", marginRight: 4 }} /> : <ChevronRight size={12} style={{ verticalAlign: "-1px", marginRight: 4 }} />)}{e.message}</span>
      {e.job_id !== null ? <button className="jobtag" title={`Show only job #${e.job_id}`} onClick={(ev) => { ev.stopPropagation(); onJob(e.job_id!); }}>job {e.job_id}</button> : <span />}
      {open && <Detail e={e} />}
    </div>
  );
});

function LogView({ params, setParams }: { params: URLSearchParams; setParams: (p: URLSearchParams) => void }) {
  const toast = useToast();
  const lens = (params.get("lens") as Lens) in LENSES ? (params.get("lens") as Lens) : "all";
  const level = (["debug", "info", "warn", "error"].includes(params.get("level") ?? "") ? params.get("level") : "info") as LogLevel;
  const job = params.get("job") ? Number(params.get("job")) : undefined;
  const [search, setSearch] = useState("");
  const q = useDebounced(search, 300);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [hasOlder, setHasOlder] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [live, setLive] = useState(true);
  const [connected, setConnected] = useState(false);
  const [pending, setPending] = useState(0);
  const [openIds, setOpenIds] = useState<Set<number>>(new Set());
  const [stick, setStick] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  const pendingRef = useRef<LogEvent[]>([]);
  const gapRef = useRef(false);                  // the pause buffer overflowed: events were dropped, so resuming reloads instead of appending
  const stickRef = useRef(true);                  // mirrors `stick` so the live-append callback can read it without re-subscribing
  const liveRef = useRef(live);
  liveRef.current = live;
  const qRef = useRef(q);
  qRef.current = q;
  const sources = LENSES[lens].sources;
  const view = useRef(0);                         // bumped whenever the visible list is reset; late responses for an older view are dropped
  const [olderBusy, setOlderBusy] = useState(false);
  const [reload, setReload] = useState(0);          // bumped to restart the initial load when its response was made stale (e.g. by clearing the log)

  const matchesQuery = (e: LogEvent) => !qRef.current || `${e.message} ${JSON.stringify(e.data)}`.toLowerCase().includes(qRef.current.toLowerCase());
  const append = useCallback((batch: LogEvent[]) => {
    setEvents((cur) => {
      const seen = new Set(cur.slice(-500).map((x) => x.id));
      const fresh = batch.filter((x) => !seen.has(x.id));
      if (!fresh.length) return cur;
      return trimRows([...cur, ...fresh], stickRef.current, MAX_ROWS, HARD_MAX_ROWS);
    });
  }, []);

  useEffect(() => {
    let es: EventSource | null = null;
    let cancelled = false;
    view.current += 1; setOlderBusy(false);
    setLoading(true); setError(null); setEvents([]); pendingRef.current = []; gapRef.current = false; setPending(0); setOpenIds(new Set()); setConnected(false);
    const token = view.current;                    // clearing the log bumps this: a response that started before it must not repopulate the list
    api.logs({ limit: 400, level, source: sources || undefined, job_id: job, q: q || undefined }).then((r) => {
      if (cancelled) return;
      if (token !== view.current) { setReload((n) => n + 1); return; }    // stale: reload instead of leaving the view empty and without a live stream
      setEvents(r.items); setHasOlder(r.has_older); setLoading(false); setStick(true);
      es = new EventSource(logStreamUrl({ after_id: r.newest_id, level, source: sources || undefined, job_id: job }));
      es.onopen = () => setConnected(true);
      es.onerror = () => setConnected(false);
      es.onmessage = (m) => {
        const batch = (JSON.parse(m.data) as LogEvent[]).filter(matchesQuery);
        if (!batch.length) return;
        if (liveRef.current) append(batch);
        else {
          const b = bufferPaused(pendingRef.current, batch, HARD_MAX_ROWS);
          pendingRef.current = b.buf;
          if (b.overflowed) gapRef.current = true;
          setPending(pendingRef.current.length);
        }
      };
    }).catch((e) => { if (!cancelled) { setError(e); setLoading(false); } });
    return () => { cancelled = true; es?.close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [level, sources, job, q, reload]);

  useEffect(() => { if (events.length >= MAX_ROWS) setHasOlder(true); }, [events.length]);   // rows trimmed from the top still exist in the database
  useEffect(() => {   // resuming flushes what arrived while paused
    if (!live || !pendingRef.current.length) return;
    if (gapRef.current) { gapRef.current = false; pendingRef.current = []; setPending(0); setReload((n) => n + 1); return; }   // events were dropped while paused: refetch
    append(pendingRef.current); pendingRef.current = []; setPending(0);
  }, [live, append]);

  useLayoutEffect(() => { if (stick && box.current) box.current.scrollTop = box.current.scrollHeight; }, [events, stick, loading]);
  stickRef.current = stick;
  const onScroll = () => { const el = box.current; if (el) setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 48); };

  const loadOlder = async () => {
    const first = events[0];
    if (!first || olderBusy || events.length >= HARD_MAX_ROWS) return;                 // one request at a time: a double click must not prepend the same page twice
    const token = view.current;
    const el = box.current;
    const before = el?.scrollHeight ?? 0;
    const top = el?.scrollTop ?? 0;
    setOlderBusy(true);
    try {
      const r = await api.logs({ limit: 300, before_id: first.id, level, source: sources || undefined, job_id: job, q: q || undefined });
      if (token !== view.current) return;            // filters changed or the log was cleared meanwhile: this page belongs to another view
      setStick(false);
      setEvents((cur) => { const have = new Set(cur.map((x) => x.id)); return [...r.items.filter((x) => !have.has(x.id)), ...cur]; });
      setHasOlder(r.has_older);
      requestAnimationFrame(() => { if (el) el.scrollTop = top + el.scrollHeight - before; });   // keep the viewport on the same rows
    } catch (e) { if (token === view.current) setError(e); }
    finally { if (token === view.current) setOlderBusy(false); }
  };

  const update = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) v === null || v === "" ? n.delete(k) : n.set(k, v);
    setParams(n);
  };
  const toggle = useCallback((id: number) => setOpenIds((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; }), []);
  const onJob = useCallback((id: number) => { const n = new URLSearchParams(params); n.set("job", String(id)); setParams(n); }, [params, setParams]);

  const download = () => {
    const blob = new Blob([events.map((e) => JSON.stringify(e)).join("\n")], { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `specforge-activity-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.ndjson`; a.click();
    URL.revokeObjectURL(a.href);
  };
  const clear = async () => {
    if (!confirm("Clear the whole activity log? This cannot be undone.")) return;
    try {
      const r = await api.clearLogs();
      view.current += 1; setOlderBusy(false);          // discard any in-flight "older" page
      pendingRef.current = []; gapRef.current = false; setPending(0);          // events buffered while paused were deleted too: resuming must not bring them back
      setEvents([]); setHasOlder(false); setOpenIds(new Set());
      toast.push({ title: `Cleared ${r.deleted} events`, tone: "ok" });
    } catch (e) { setError(e); }
  };
  const counts = useMemo(() => ({ warn: events.filter((e) => e.level === "warn").length, error: events.filter((e) => e.level === "error").length }), [events]);

  return (
    <div className="card flush">
      <div className="logbar">
        <div className="segmented" role="group" aria-label="View">
          {(Object.keys(LENSES) as Lens[]).map((k) => (
            <button key={k} className={k === lens ? "on" : ""} aria-pressed={k === lens} onClick={() => update({ lens: k === "all" ? null : k })}>
              {k === "ai" && <Bot size={13} />}{k === "process" && <Cog size={13} />}{k === "all" && <Layers size={13} />}{LENSES[k].label}
            </button>
          ))}
        </div>
        <Segmented<LogLevel> label="Minimum level" value={level} options={LEVEL_OPTS} onChange={(v) => update({ level: v === "info" ? null : v })} />
        <input placeholder="Filter text…" aria-label="Filter text" value={search} onChange={(e) => setSearch(e.target.value)} style={{ width: 180 }} />
        {job !== undefined && <button className="chip on" onClick={() => update({ job: null })} title="Remove job filter">Job #{job} <X size={12} /></button>}
        <span className="grow" />
        <span className="row small muted" style={{ gap: 6 }}><span className={`dot ${connected && live ? "ok pulse" : connected ? "warn" : "bad"}`} />{!connected ? "Reconnecting…" : live ? "Live" : "Paused"}</span>
        <button className="sm" onClick={() => setLive((v) => !v)} aria-pressed={!live}>{live ? <><Pause size={13} /> Pause</> : <><Play size={13} /> Resume{pending ? ` (${pending})` : ""}</>}</button>
        <button className="sm" onClick={download} disabled={!events.length}><Download size={13} /> Export</button>
        <button className="sm danger" onClick={clear}><Trash2 size={13} /></button>
      </div>
      {lens === "ai" && <div className="small muted" style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>The scenario agent narrates each requirement it reads and every draft it accepts or rejects. <strong>llm</strong> rows are the individual model calls. Click a row to see the exact prompt and reply.</div>}
      {level === "debug" && <div className="small muted" style={{ padding: "8px 14px", borderBottom: "1px solid var(--border)" }}>Debug events (cache hits, progress heartbeats) are only recorded when the minimum level in Settings → Logging is “debug”.</div>}
      <ErrorNote error={error} />
      <div className="logbox" ref={box} onScroll={onScroll} role="log" aria-live="off" aria-label="Activity log">
        {loading && <div className="row muted" style={{ padding: 16 }}><span className="spinner" /> Loading…</div>}
        {!loading && hasOlder && events.length >= HARD_MAX_ROWS && <div className="muted small" style={{ padding: 10, textAlign: "center" }}>Showing {events.length.toLocaleString()} events, the most this view holds. Narrow the filters or export to go further back.</div>}
        {!loading && hasOlder && events.length < HARD_MAX_ROWS && <div style={{ padding: 10, textAlign: "center" }}><button className="sm" onClick={loadOlder} disabled={olderBusy}>{olderBusy && <span className="spinner" />}Load older events</button></div>}
        {!loading && events.length === 0 && <div style={{ padding: 28 }}><Empty title="No events match" icon={Layers}>Events appear here as soon as something runs: parsing a spec, building a catalog, an AI call. Try another view or a lower level.</Empty></div>}
        {events.map((e) => <Row key={e.id} e={e} open={openIds.has(e.id)} onToggle={toggle} onJob={onJob} />)}
        {!stick && events.length > 0 && (
          <div className="newbar"><button className="sm primary" onClick={() => { setStick(true); }}><ArrowDown size={13} /> Jump to latest</button></div>
        )}
      </div>
      <div className="row small muted" style={{ padding: "8px 14px", borderTop: "1px solid var(--border)" }}>
        <span>{events.length} events{events.length >= MAX_ROWS ? " (oldest dropped)" : ""}</span>
        {counts.error > 0 && <Badge value="error" label={`${counts.error} error${counts.error === 1 ? "" : "s"}`} />}
        {counts.warn > 0 && <Badge value="warning" label={`${counts.warn} warning${counts.warn === 1 ? "" : "s"}`} />}
      </div>
    </div>
  );
}

const startOf = (j: Job) => (j.created ? new Date(j.created.replace(" ", "T") + "Z").getTime() : Date.now());
const dur = (ms: number) => { const s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`; };

function RunningNow({ onLogs }: { onLogs: (id: number) => void }) {
  const q = useQuery({ queryKey: ["jobs", "running"], queryFn: api.jobs, refetchInterval: 1500 });
  const [, tick] = useState(0);
  useEffect(() => { const h = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(h); }, []);
  const running = (q.data ?? []).filter((j) => j.state === "running" || j.state === "queued");
  if (!running.length) return null;
  return (
    <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(320px, 100%), 1fr))", marginBottom: 16 }}>
      {running.map((j) => (
        <div key={j.id} className="runcard">
          <div className="row"><span className="spinner" /><strong className="grow">{j.type}</strong><span className="muted small">#{j.id} · {dur(Date.now() - startOf(j))}</span></div>
          <div className="small" style={{ minHeight: 18 }}>{j.state === "queued" ? "Queued…" : j.message || "Working…"}</div>
          <div className={`progress ${j.total > 0 ? "" : "indeterminate"}`}><span style={{ width: j.total > 0 ? `${Math.min(100, (100 * j.progress) / j.total)}%` : undefined }} /></div>
          <div className="row"><button className="sm" onClick={() => onLogs(j.id)}>Filter logs to this job</button><span className="grow" /><button className="sm danger" onClick={() => api.cancelJob(j.id)}>Cancel</button></div>
        </div>
      ))}
    </div>
  );
}

function JobsTable({ onLogs }: { onLogs: (id: number) => void }) {
  const q = useQuery({ queryKey: ["jobs", "history"], queryFn: api.jobs, refetchInterval: (query) => ((query.state.data as Job[] | undefined)?.some((j) => j.state === "running" || j.state === "queued") ? 1500 : 8000) });
  const [open, setOpen] = useState<number | null>(null);
  const list = q.data ?? [];
  return (
    <div className="card flush">
      <Loading q={q} />
      {q.data && list.length === 0 && <div style={{ padding: 28 }}><Empty title="No jobs yet" icon={Layers}>Parsing, catalog builds, imports, AI drafting and coverage runs show up here.</Empty></div>}
      {list.length > 0 && (
        <div className="scroll">
          <table className="t">
            <thead><tr><th>#</th><th>Job</th><th>State</th><th>Progress</th><th>Started</th><th /></tr></thead>
            <tbody>{list.map((j) => (
              <Fragment key={j.id}>
                <tr className="click" onClick={() => setOpen(open === j.id ? null : j.id)}>
                  <td>{j.id}</td><td>{j.type}</td><td><Badge value={j.state} label={j.state} /></td>
                  <td>{j.total > 0 ? `${j.progress}/${j.total}` : j.progress || ""} <span className="muted small">{j.message ?? ""}</span></td>
                  <td className="small muted">{j.created ?? ""}</td>
                  <td onClick={(e) => e.stopPropagation()} className="row" style={{ flexWrap: "nowrap" }}>
                    <button className="sm" onClick={() => onLogs(j.id)}>Logs</button>
                    {(j.state === "running" || j.state === "queued") && <button className="sm danger" onClick={() => api.cancelJob(j.id).then(() => q.refetch())}>Cancel</button>}
                  </td>
                </tr>
                {open === j.id && (
                  <tr><td colSpan={6}>
                    {j.error && <pre>{j.error}</pre>}
                    {j.result ? <pre>{JSON.stringify(j.result, null, 1)}</pre> : !j.error && <span className="muted">No result recorded.</span>}
                  </td></tr>
                )}
              </Fragment>
            ))}</tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function ActivityPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "jobs" ? "jobs" : "logs";
  const toLogs = (id: number) => { const n = new URLSearchParams(); n.set("job", String(id)); setParams(n); };
  const setTab = (t: "logs" | "jobs") => { const n = new URLSearchParams(params); t === "jobs" ? n.set("tab", "jobs") : n.delete("tab"); setParams(n); };
  return (
    <>
      <PageHeader title="Activity" description="Everything the system and its AI agents are doing, as it happens: jobs, parsing, model calls, decisions and your own actions." />
      <RunningNow onLogs={toLogs} />
      <div className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "logs"} className={tab === "logs" ? "active" : ""} onClick={() => setTab("logs")}>Live log</button>
        <button role="tab" aria-selected={tab === "jobs"} className={tab === "jobs" ? "active" : ""} onClick={() => setTab("jobs")}>Jobs</button>
      </div>
      {tab === "logs" ? <LogView params={params} setParams={setParams} /> : <JobsTable onLogs={toLogs} />}
    </>
  );
}
