import { Fragment, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Copy, Download, RefreshCw, Search, X } from "lucide-react";
import { api, mcpCsvUrl } from "../api";
import type { McpCallRow, McpFilters, McpIssue, McpIssueDetail, McpIssueStatus, McpKpis, McpMetrics, McpRange } from "../types";
import { useToast } from "../components/Toasts";
import { Loading, Segmented } from "../components/ui";
import { CellBar, DayBars, LatencyLines, OutcomeBars, OutcomeMix, WeekHeatmap } from "../components/McpCharts";
import {
  ago, approxTokens, callText, delta, fmtChars, fmtDuration, fmtInt, fmtMs, fmtPct, OUTCOME_LABEL, OUTCOME_TONE, STATUS_TONE, when,
} from "../mcpFormat";

export type AdminTab = "metrics" | "issues" | "calls";
const RANGES: McpRange[] = ["1h", "24h", "7d", "30d", "90d"];
const FILTER_KEYS = ["tool", "agent", "ip", "outcome", "transport", "spec", "reason", "fingerprint", "q"] as const;
const STATUSES: McpIssueStatus[] = ["open", "investigating", "fixed", "ignored"];

/** The filter shared by the three admin tabs, kept in the URL so a view can be bookmarked or sent to a colleague. */
type Patch = Partial<Record<keyof McpFilters | "issue" | "status", string | boolean | undefined>>;

export function useMcpFilters(): [McpFilters, (patch: Patch, tab?: AdminTab) => void] {
  const [params, setParams] = useSearchParams();
  const range = (RANGES as string[]).includes(params.get("range") ?? "") ? (params.get("range") as McpRange) : "7d";
  const f: McpFilters = { range, slow: params.get("slow") === "1" };
  for (const k of FILTER_KEYS) {
    const v = params.get(k);
    if (v) f[k] = v;
  }
  const set = (patch: Patch, tab?: AdminTab) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "" || v === false) n.delete(k);
      else n.set(k, v === true ? "1" : String(v));
    }
    if (tab) n.set("tab", tab);
    setParams(n);
  };
  return [f, set];
}

function FilterBar({ f, set, refresh, extra }: { f: McpFilters; set: ReturnType<typeof useMcpFilters>[1]; refresh: () => void; extra?: React.ReactNode }) {
  const chips = FILTER_KEYS.filter((k) => f[k]);
  return (
    <div className="mcpa-filters">
      <Segmented label="Time range" value={f.range} onChange={(v) => set({ range: v })} options={RANGES.map((r) => ({ value: r, label: r }))} />
      {chips.map((k) => (
        <button key={k} className="chip on" onClick={() => set({ [k]: undefined })} title="Remove this filter">
          {k}: {k === "fingerprint" ? String(f[k]).slice(0, 8) : String(f[k])} <X size={12} />
        </button>
      ))}
      {f.slow && <button className="chip on" onClick={() => set({ slow: undefined })}>slow only <X size={12} /></button>}
      <span className="grow" />
      {extra}
      <button className="sm" onClick={refresh} title="Refresh"><RefreshCw size={14} /></button>
      <a className="btn-link" href={mcpCsvUrl(f)} download><Download size={14} /> Export CSV</a>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------------------------- Metrics

function Kpi({ label, value, d, hint }: { label: string; value: string; d?: ReturnType<typeof delta>; hint?: string }) {
  return (
    <div className="card mcpa-kpi">
      <div className="muted small">{label}</div>
      <div className="mcpa-kpi-v">{value}</div>
      <div className={`small mcpa-${d?.tone ?? "flat"}`}>{d ? `${d.text} vs previous` : hint ?? " "}</div>
    </div>
  );
}

function Kpis({ k, p }: { k: McpKpis; p: McpKpis }) {
  return (
    <div className="mcpa-kpis">
      <Kpi label="Tool calls" value={fmtInt(k.calls)} d={delta(k.calls, p.calls)} />
      <Kpi label="Answered" value={fmtPct(k.answered_pct)} d={delta(k.answered_pct, p.answered_pct, { points: true })} />
      <Kpi label="Miss rate" value={fmtPct(k.miss_pct)} d={delta(k.miss_pct, p.miss_pct, { points: true, lowerIsBetter: true })} />
      <Kpi label="Error rate" value={fmtPct(k.error_pct)} d={delta(k.error_pct, p.error_pct, { points: true, lowerIsBetter: true })} />
      <Kpi label="p95 latency" value={fmtMs(k.p95_ms)} d={delta(k.p95_ms, p.p95_ms, { lowerIsBetter: true })} />
      <Kpi label="Clients · agents" value={`${k.clients} · ${k.agents}`} hint={`${fmtInt(k.sessions ?? 0)} agent sessions`} />
      <Kpi label="Recovered after a miss / error" value={fmtPct(k.recovered_pct)} hint="agent still got an answer in the session" />
      <Kpi label="Rate limited · refused" value={`${fmtInt(k.rate_limited)} · ${fmtInt(k.refused)}`} d={delta(k.rate_limited + k.refused, p.rate_limited + p.refused, { lowerIsBetter: true })} />
    </div>
  );
}

function MetricsBody({ m, set }: { m: McpMetrics; set: ReturnType<typeof useMcpFilters>[1] }) {
  const k = m.kpis;
  const maxTool = Math.max(1, ...m.tools.map((t) => t.calls));
  const maxAgent = Math.max(1, ...m.agents.map((t) => t.calls));
  const maxSpec = Math.max(1, ...m.specs.map((t) => t.calls));
  const totalT = m.transports.reduce((a, t) => a + t.calls, 0) || 1;
  const failures = m.reasons.reduce((a, r) => a + r.calls, 0) || 1;
  return (
    <>
      <Kpis k={k} p={m.previous} />
      <div className="mcpa-row r21">
        <div className="card">
          <div className="card-head"><h2 className="grow">Tool calls over time</h2>
            <div className="mcpa-legend"><span><i className="mcpc-ok" />Answered</span><span><i className="mcpc-miss" />Miss (nothing found)</span><span><i className="mcpc-error" />Error</span></div></div>
          {k.calls === 0 ? <p className="muted">No tool calls in this range.</p> : <OutcomeBars series={m.series} bucket={m.bucket_s} />}
        </div>
        <div className="card">
          <div className="card-head"><h2 className="grow">Why calls failed</h2><span className="muted small">click to see them</span></div>
          {m.reasons.length === 0 ? <p className="muted small">No misses or errors in this range.</p> : (
            <table className="t mcpa-tight"><tbody>{m.reasons.map((r) => (
              <tr key={r.outcome + r.reason} className="click" onClick={() => set({ reason: r.reason, outcome: r.outcome }, "issues")}>
                <td><span className={`badge ${OUTCOME_TONE[r.outcome]}`}>{OUTCOME_LABEL[r.outcome]}</span> {r.reason}</td>
                <td className="num">{fmtInt(r.calls)}</td><td className="num muted">{fmtPct((100 * r.calls) / failures)}</td></tr>
            ))}</tbody></table>
          )}
        </div>
      </div>
      <div className="mcpa-row r11">
        <div className="card">
          <div className="card-head"><h2 className="grow">Latency</h2>
            <div className="mcpa-legend"><span><i className="mcpc-l1f" />p50</span><span><i className="mcpc-l2f" />p95</span><span><i className="mcpc-thr" />slow (2 s)</span></div></div>
          {k.calls === 0 ? <p className="muted">No calls.</p> : <LatencyLines series={m.series} bucket={m.bucket_s} />}
          <p className="small muted">p50 {fmtMs(k.p50_ms)} · p95 {fmtMs(k.p95_ms)} · p99 {fmtMs(k.p99_ms)} · {fmtInt(k.slow)} slow call(s) <button className="sm ghost" onClick={() => set({ slow: true }, "calls")}>show</button></p>
        </div>
        <div className="card">
          <div className="card-head"><h2 className="grow">When agents use it</h2><span className="muted small">calls per hour, your local time</span></div>
          <WeekHeatmap grid={m.heatmap} />
        </div>
      </div>
      <div className="card flush">
        <div className="card-head mcpa-pad"><h2 className="grow">Tools</h2><span className="muted small">click a tool to filter every chart by it</span></div>
        <div className="scroll"><table className="t">
          <thead><tr><th>Tool</th><th>Calls</th><th>Outcome mix</th><th className="num">Miss</th><th className="num">Error</th><th className="num">p50</th><th className="num">p95</th><th className="num">Max</th><th className="num">Avg answer</th><th className="num">Paged</th><th className="num">Slow</th><th>Last call</th><th /></tr></thead>
          <tbody>{m.tools.map((t) => {
            const flag = t.error_pct >= 2.5 ? ["bad", "errors"] : t.miss_pct >= 10 ? ["warn", "high misses"] : (t.p95_ms ?? 0) >= 2000 ? ["warn", "slow"] : ["ok", "healthy"];
            return (
              <tr key={t.tool} className="click" onClick={() => set({ tool: t.tool })}>
                <td className="mono">{t.tool}</td>
                <td><div className="mcpa-barcell"><span>{fmtInt(t.calls)}</span><CellBar v={t.calls} max={maxTool} /></div></td>
                <td style={{ width: 110 }}><OutcomeMix ok={t.ok} miss={t.misses} error={t.errors} /></td>
                <td className="num">{fmtPct(t.miss_pct)}</td><td className="num">{fmtPct(t.error_pct)}</td>
                <td className="num">{fmtMs(t.p50_ms)}</td><td className="num">{fmtMs(t.p95_ms)}</td><td className="num">{fmtMs(t.max_ms)}</td>
                <td className="num" title={t.avg_chars ? `about ${fmtInt(approxTokens(t.avg_chars))} tokens` : undefined}>{fmtChars(t.avg_chars)} ch</td>
                <td className="num">{fmtInt(t.paged)}</td><td className="num">{fmtInt(t.slow)}</td><td className="small muted">{ago(t.last)}</td>
                <td><span className={`badge ${flag[0]}`}>{flag[1]}</span></td>
              </tr>
            );
          })}</tbody>
        </table></div>
      </div>
      <div className="mcpa-row r111">
        <div className="card">
          <div className="card-head"><h2 className="grow">Agents</h2><span className="muted small">from the MCP handshake</span></div>
          <table className="t mcpa-tight"><thead><tr><th>Agent</th><th className="num">Calls</th><th className="num">Miss</th><th className="num">Error</th><th style={{ width: "30%" }} /></tr></thead>
            <tbody>{m.agents.map((a) => (
              <tr key={a.agent + a.version} className="click" onClick={() => set({ agent: a.agent })}>
                <td className="nowrap">{a.agent} <span className="muted small">{a.version}</span></td><td className="num">{fmtInt(a.calls)}</td>
                <td className="num">{fmtPct(a.miss_pct)}</td><td className="num">{fmtPct(a.error_pct)}</td><td><CellBar v={a.calls} max={maxAgent} /></td></tr>
            ))}</tbody></table>
          <h3>Transport</h3>
          <div className="mcpc-mix tall">{m.transports.map((t, i) => <span key={t.transport} className={`mcpc-s${(i % 3) + 1}`} style={{ flexGrow: t.calls / totalT }} title={`${t.transport}: ${t.calls}`} />)}</div>
          <div className="mcpa-legend" style={{ marginTop: 6 }}>{m.transports.map((t, i) => <span key={t.transport}><i className={`mcpc-s${(i % 3) + 1}`} />{t.transport} {fmtPct((100 * t.calls) / totalT)}</span>)}</div>
        </div>
        <div className="card">
          <div className="card-head"><h2 className="grow">Specifications asked for</h2></div>
          <table className="t mcpa-tight"><thead><tr><th>Spec</th><th className="num">Calls</th><th className="num">Miss</th><th style={{ width: "30%" }} /></tr></thead>
            <tbody>{m.specs.map((s) => (
              <tr key={s.spec + s.version} className="click" onClick={() => s.spec !== "(any / latest)" && set({ spec: s.spec })}>
                <td>{s.spec} <span className="muted small">{s.version}</span></td><td className="num">{fmtInt(s.calls)}</td><td className="num">{fmtPct(s.miss_pct)}</td>
                <td><CellBar v={s.calls} max={maxSpec} /></td></tr>
            ))}</tbody></table>
        </div>
        <div className="card">
          <div className="card-head"><h2 className="grow">Answers &amp; sessions</h2></div>
          <table className="t mcpa-tight"><tbody>
            <tr><td>Average answer</td><td className="num">{fmtChars(k.avg_chars)} chars · ~{fmtChars(approxTokens(k.avg_chars))} tokens</td></tr>
            <tr><td>p95 answer</td><td className="num">{fmtChars(k.p95_chars)} chars</td></tr>
            <tr><td>Served in total</td><td className="num">~{fmtChars(approxTokens(k.chars_total))} tokens</td></tr>
            <tr><td>Answers cut into pages</td><td className="num">{fmtPct(k.paged_pct)}</td></tr>
            <tr><td>Agent sessions</td><td className="num">{fmtInt(k.sessions)}</td></tr>
            <tr><td>Calls per session</td><td className="num">median {fmtInt(k.calls_per_session_p50)} · p95 {fmtInt(k.calls_per_session_p95)}</td></tr>
            <tr><td>Session length</td><td className="num">median {fmtDuration(k.session_s_p50)}</td></tr>
          </tbody></table>
          <p className="muted small">A session: calls from one address and agent with no pause over 10 minutes.</p>
        </div>
      </div>
      <div className="mcpa-row r11">
        <div className="card flush">
          <div className="card-head mcpa-pad"><h2 className="grow">Clients</h2><span className="muted small">by network address; the endpoint has no sign-in</span></div>
          <div className="scroll" style={{ maxHeight: 380 }}><table className="t">
            <thead><tr><th>Address</th><th>Agent</th><th className="num">Calls</th><th className="num">Miss</th><th className="num">Error</th><th className="num">429s</th><th>Last seen</th></tr></thead>
            <tbody>{m.clients.map((c) => (
              <tr key={c.ip} className="click" onClick={() => set({ ip: c.ip })}>
                <td className="mono">{c.ip}</td><td className="small nowrap">{c.agents.join(", ")}</td><td className="num">{fmtInt(c.calls)}</td>
                <td className="num">{fmtPct(c.miss_pct)}</td><td className="num">{fmtPct(c.error_pct)}</td>
                <td className="num">{c.rate_limited ? <span className="badge warn">{c.rate_limited}</span> : 0}</td>
                <td className="nowrap">{c.active ? <span className="badge ok">active</span> : <span className="badge">idle</span>} <span className="small muted">{ago(c.last)}</span></td></tr>
            ))}</tbody>
          </table></div>
        </div>
        <div className="card flush">
          <div className="card-head mcpa-pad"><h2 className="grow">Common call chains</h2><span className="muted small">consecutive calls in one session</span></div>
          {m.chains.length === 0 ? <p className="muted small mcpa-pad">Not enough calls yet.</p> : (
            <div className="scroll"><table className="t">
              <thead><tr><th>Chain</th><th className="num">Times</th><th className="num">Last call answered</th></tr></thead>
              <tbody>{m.chains.map((c) => (
                <tr key={c.chain.join(">")}><td className="mono small">{c.chain.join(" → ")} {c.retry && <span className="badge warn">retry loop</span>}</td>
                  <td className="num">{fmtInt(c.count)}</td><td className="num">{c.answered_pct < 70 ? <span className="badge warn">{fmtPct(c.answered_pct)}</span> : fmtPct(c.answered_pct)}</td></tr>
              ))}</tbody>
            </table></div>
          )}
          <p className="muted small mcpa-pad">The same tool several times in a row usually means the agent is not finding what it needs on the first try.</p>
        </div>
      </div>
    </>
  );
}

export function MetricsTab() {
  const [f, set] = useMcpFilters();
  const q = useQuery({ queryKey: ["mcp-metrics", f], queryFn: () => api.mcpMetrics(f), refetchInterval: 30_000 });
  return (
    <>
      <FilterBar f={f} set={set} refresh={() => q.refetch()} extra={q.dataUpdatedAt ? <span className="muted small">updated {ago(q.dataUpdatedAt / 1000)}</span> : null} />
      {q.data ? <MetricsBody m={q.data} set={set} /> : <Loading q={q} />}
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------- Misses & errors

/** Markdown summary of one group, to paste into a bug report or a parser ticket. */
export function issueReport(d: McpIssueDetail): string {
  const lines = [
    `### MCP ${d.outcome}: ${d.tool} — ${d.reason ?? "unknown reason"}`,
    `- ${d.count} occurrence(s), ${new Date(d.first * 1000).toISOString()} → ${new Date(d.last * 1000).toISOString()}`,
    `- agents: ${d.agents.map((a) => `${a.agent} (${a.count})`).join(", ")}`,
    `- clients: ${d.clients.map((c) => `${c.ip} (${c.count})`).join(", ")}`,
    `- agent got an answer later in the session: ${d.recovered} of ${d.followed} followed`,
    `- status: ${d.status}${d.note ? ` — ${d.note}` : ""}`,
    "", "Calls as asked:", ...d.variants.slice(0, 10).map((v) => `- \`${callText(d.tool, v.args)}\` × ${v.count}`),
  ];
  if (d.errors.length) lines.push("", "Messages:", ...d.errors.slice(0, 5).map((e) => "```\n" + e.error + "\n```"));
  return lines.join("\n");
}

function IssueDetail({ fp, range }: { fp: string; range: McpRange }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [, set] = useMcpFilters();
  const q = useQuery({ queryKey: ["mcp-issue", fp, range], queryFn: () => api.mcpIssue(fp, range) });
  const [note, setNote] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (b: { status?: McpIssueStatus; note?: string }) => api.mcpTriage(fp, b),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["mcp-issues"] }); qc.invalidateQueries({ queryKey: ["mcp-issue", fp] }); setNote(null); toast.push({ title: "Saved", tone: "ok" }); },
    onError: (e) => toast.push({ title: "Not saved", description: (e as Error).message, tone: "bad" }),
  });
  const d = q.data;
  if (!d) return <Loading q={q} />;
  return (
    <div className="mcpa-detail">
      <div className="mcpa-detail-grid">
        <div>
          <h3>Triage</h3>
          <div className="row">
            <select value={d.status} onChange={(e) => save.mutate({ status: e.target.value as McpIssueStatus })} aria-label="Status">
              {STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            {d.updated && <span className="muted small">by {d.updated_by} · {d.updated} UTC</span>}
          </div>
          <textarea className="mcpa-note" rows={3} placeholder="What went wrong, what to fix (parser, spec library, tool description, agent rule)…"
            value={note ?? d.note ?? ""} onChange={(e) => setNote(e.target.value)} />
          <div className="row">
            <button className="primary sm" disabled={note === null || save.isPending} onClick={() => save.mutate({ note: note ?? "" })}>Save note</button>
            <button className="sm" onClick={() => navigator.clipboard.writeText(issueReport(d)).then(() => toast.push({ title: "Report copied as Markdown", tone: "ok" }))}><Copy size={13} /> Copy report</button>
            <button className="sm" onClick={() => set({ fingerprint: fp, outcome: undefined, reason: undefined }, "calls")}>All {d.count} calls</button>
            <a className="btn-link sm" href={mcpCsvUrl({ range, fingerprint: fp })} download><Download size={13} /> CSV</a>
          </div>
          <h3>Per day</h3>
          <DayBars days={d.per_day} />
          <p className="muted small">{d.count} occurrence(s) · first {when(d.first)} · last {when(d.last)} · p50 {fmtMs(d.p50_ms)} · the agent got an answer later in {d.recovered} of {d.followed} followed sessions</p>
        </div>
        <div>
          <h3>Asked as</h3>
          <table className="t mcpa-tight"><tbody>{d.variants.map((v, i) => <tr key={i}><td className="mono small">{callText(d.tool, v.args)}</td><td className="num">×{v.count}</td></tr>)}</tbody></table>
          {d.errors.length > 0 && <><h3>Messages returned</h3>{d.errors.map((e, i) => <pre key={i} className="mcpa-pre">{e.error}<span className="muted"> ×{e.count}</span></pre>)}</>}
          <div className="mcpa-detail-grid tight">
            <div><h3>Agents</h3>{d.agents.map((a) => <div key={a.agent} className="small">{a.agent} <span className="muted">×{a.count}</span></div>)}</div>
            <div><h3>Clients</h3>{d.clients.map((c) => <div key={c.ip} className="small mono">{c.ip} <span className="muted">×{c.count}</span></div>)}</div>
          </div>
        </div>
      </div>
      <h3>What the agent did next</h3>
      <div className="scroll"><table className="t">
        <thead><tr><th>When</th><th>Client · agent</th><th>Next calls in the same session</th></tr></thead>
        <tbody>{d.next.map(({ call, next }) => (
          <tr key={call.id}>
            <td className="small muted">{when(call.ts)}</td>
            <td className="small"><span className="mono">{call.ip}</span> · {[call.agent, call.agent_version].filter(Boolean).join(" ") || "unknown"}</td>
            <td>{next.length === 0 ? <span className="muted small">nothing more within 10 min (gave up or answered from memory)</span> : next.map((n) => (
              <div key={n.id} className="small"><span className={`badge ${OUTCOME_TONE[n.outcome]}`}>{OUTCOME_LABEL[n.outcome]}</span> <span className="mono">{callText(n.tool, n.args)}</span> <span className="muted">+{Math.round(n.ts - call.ts)} s</span></div>
            ))}</td>
          </tr>
        ))}</tbody>
      </table></div>
    </div>
  );
}

export function IssuesTab() {
  const [f, set] = useMcpFilters();
  const [params, setParams] = useSearchParams();
  const status = ([...STATUSES, "all"] as string[]).includes(params.get("status") ?? "") ? (params.get("status") as McpIssueStatus | "all") : "open";
  const open = params.get("issue");
  const setStatus = (s: string) => { const n = new URLSearchParams(params); n.set("status", s); setParams(n); };
  const toggle = (fp: string) => { const n = new URLSearchParams(params); open === fp ? n.delete("issue") : n.set("issue", fp); setParams(n); };
  const q = useQuery({ queryKey: ["mcp-issues", f, status], queryFn: () => api.mcpIssues(f, status), refetchInterval: 60_000 });
  const outcome = f.outcome === "miss" || f.outcome === "error" ? f.outcome : "both";
  const items: McpIssue[] = q.data?.items ?? [];
  const total = items.reduce((a, i) => a + i.count, 0);
  return (
    <>
      <FilterBar f={f} set={set} refresh={() => q.refetch()} />
      <div className="row" style={{ marginBottom: 12 }}>
        <Segmented label="Triage status" value={status} onChange={setStatus}
          options={[...STATUSES.map((s) => ({ value: s, label: s[0].toUpperCase() + s.slice(1) })), { value: "all", label: "All" }]} />
        <Segmented label="Kind" value={outcome} onChange={(v) => set({ outcome: v === "both" ? undefined : v })}
          options={[{ value: "both", label: "Misses & errors" }, { value: "miss", label: "Misses" }, { value: "error", label: "Errors" }]} />
        <span className="muted small">{items.length} group(s) · {fmtInt(total)} call(s). The same failure (same tool and question, or same error) is one group.</span>
      </div>
      {!q.data ? <Loading q={q} /> : items.length === 0 ? <div className="card"><p className="muted">No {status === "all" ? "" : status + " "}misses or errors in this range.</p></div> : (
        <div className="card flush"><div className="scroll"><table className="t">
          <thead><tr><th /><th className="num">Times</th><th>Kind</th><th>Call (latest)</th><th>Reason / message</th><th className="num">Clients</th><th className="num">Agents</th><th>Last</th><th>Status</th></tr></thead>
          <tbody>{items.map((i) => (
            <Fragment key={i.fingerprint}>
              <tr className={`click${open === i.fingerprint ? " sel" : ""}`} onClick={() => toggle(i.fingerprint)}>
                <td>{open === i.fingerprint ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                <td className="num"><b>{fmtInt(i.count)}</b></td>
                <td><span className={`badge ${OUTCOME_TONE[i.outcome]}`}>{OUTCOME_LABEL[i.outcome]}</span></td>
                <td className="mono small mcpa-clip">{callText(i.tool, i.args)}</td>
                <td className="small mcpa-clip"><b>{i.reason}</b>{i.error && <span className="muted"> — {i.error}</span>}</td>
                <td className="num">{i.clients}</td><td className="num">{i.agents}</td><td className="small muted">{ago(i.last)}</td>
                <td><span className={`badge ${STATUS_TONE[i.status]}`}>{i.status}</span>{i.note && <span className="muted small" title={i.note}> ✎</span>}</td>
              </tr>
              {open === i.fingerprint && <tr className="mcpa-open"><td colSpan={9}><IssueDetail fp={i.fingerprint} range={f.range} /></td></tr>}
            </Fragment>
          ))}</tbody>
        </table></div></div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------- Call log

function CallRow({ c, open, onToggle }: { c: McpCallRow; open: boolean; onToggle: () => void }) {
  const [, set] = useMcpFilters();
  return (
    <>
      <tr className={`click${open ? " sel" : ""}`} onClick={onToggle}>
        <td>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
        <td className="small muted" title={new Date(c.ts * 1000).toISOString()}>{when(c.ts)}</td>
        <td><span className={`badge ${OUTCOME_TONE[c.outcome]}`}>{OUTCOME_LABEL[c.outcome]}</span></td>
        <td className="mono small mcpa-clip">{c.tool ? callText(c.tool, c.args) : <span className="muted">(request)</span>}</td>
        <td className="small mcpa-clip">{c.reason ?? ""}</td>
        <td className="num">{fmtMs(c.ms)}</td><td className="num">{fmtChars(c.answer_chars)}</td>
        <td className="small">{[c.agent, c.agent_version].filter(Boolean).join(" ") || "—"}</td><td className="mono small">{c.ip}</td>
      </tr>
      {open && (
        <tr className="mcpa-open"><td colSpan={9}><div className="mcpa-detail">
          <div className="mcpa-detail-grid">
            <div><h3>Arguments</h3><pre className="mcpa-pre">{JSON.stringify(c.args, null, 2)}</pre>
              {c.error && <><h3>Message returned to the agent</h3><pre className="mcpa-pre">{c.error}</pre></>}</div>
            <div><h3>Request</h3><table className="t mcpa-tight"><tbody>
              <tr><td>Time (UTC)</td><td className="mono small">{new Date(c.ts * 1000).toISOString()}</td></tr>
              <tr><td>Transport</td><td>{c.transport ?? "—"}</td></tr>
              <tr><td>User-Agent</td><td className="mono small">{c.user_agent || "—"}</td></tr>
              <tr><td>Spec · version asked</td><td>{c.spec ?? "any"} {c.version ?? ""}</td></tr>
              <tr><td>Answer</td><td>{c.answer_chars === null ? "—" : `${fmtInt(c.answer_chars)} chars (~${fmtInt(approxTokens(c.answer_chars))} tokens)${c.paged ? ", more pages" : ""}${c.page ? `, page ${c.page}` : ""}`}</td></tr>
              <tr><td>Call id</td><td className="mono small">#{c.id}</td></tr>
            </tbody></table>
              <div className="row" style={{ marginTop: 8 }}>
                {c.fingerprint && <button className="sm" onClick={() => set({ fingerprint: undefined, outcome: undefined, issue: c.fingerprint!, status: "all" }, "issues")}>Open its group</button>}
                <button className="sm" onClick={() => set({ ip: c.ip })}>Only this client</button>
                <button className="sm" onClick={() => navigator.clipboard.writeText(callText(c.tool, c.args))}><Copy size={13} /> Copy call</button>
              </div>
            </div>
          </div>
        </div></td></tr>
      )}
    </>
  );
}

export function CallLogTab() {
  const [f, set] = useMcpFilters();
  const [text, setText] = useState(f.q ?? "");
  const [open, setOpen] = useState<number | null>(null);
  const q = useInfiniteQuery({
    queryKey: ["mcp-calls", f],
    queryFn: ({ pageParam }) => api.mcpCalls(f, pageParam),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <FilterBar f={f} set={set} refresh={() => q.refetch()} />
      <div className="row" style={{ marginBottom: 12 }}>
        <Segmented label="Outcome" value={f.outcome ?? "all"} onChange={(v) => set({ outcome: v === "all" ? undefined : v })}
          options={[{ value: "all", label: "All" }, { value: "ok", label: "Answered" }, { value: "miss", label: "Misses" }, { value: "error", label: "Errors" }, { value: "miss,error", label: "Misses + errors" }, { value: "rate_limited,refused", label: "Refused" }]} />
        <form className="row" onSubmit={(e) => { e.preventDefault(); set({ q: text.trim() || undefined }); }}>
          <input placeholder="Text in arguments or message" value={text} onChange={(e) => setText(e.target.value)} style={{ width: 240 }} />
          <button type="submit" className="sm"><Search size={13} /> Search</button>
        </form>
        <label className="row small"><input type="checkbox" checked={!!f.slow} onChange={(e) => set({ slow: e.target.checked })} /> slow (≥ 2 s) only</label>
        <span className="muted small grow" style={{ textAlign: "right" }}>{q.data ? `${fmtInt(q.data.pages[0].total)} call(s)` : ""}</span>
      </div>
      {!q.data ? <Loading q={q} /> : rows.length === 0 ? <div className="card"><p className="muted">No calls match.</p></div> : (
        <div className="card flush"><div className="scroll"><table className="t">
          <thead><tr><th /><th>When</th><th>Outcome</th><th>Call</th><th>Reason</th><th className="num">Time</th><th className="num">Answer</th><th>Agent</th><th>Client</th></tr></thead>
          <tbody>{rows.map((c) => <CallRow key={c.id} c={c} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}</tbody>
        </table></div>
          {q.hasNextPage && <div className="mcpa-pad"><button onClick={() => q.fetchNextPage()} disabled={q.isFetchingNextPage}>{q.isFetchingNextPage ? "Loading…" : "Load older calls"}</button></div>}
        </div>
      )}
    </>
  );
}
