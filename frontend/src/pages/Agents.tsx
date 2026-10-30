import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, PlugZap, RefreshCw } from "lucide-react";
import { api } from "../api";
import type { McpCall, McpSettings, McpStatus } from "../types";
import { useAuth } from "../components/auth";
import { useToast } from "../components/Toasts";
import { Field, Loading, PageHeader, Segmented, Switch } from "../components/ui";

type Client = "opencode" | "roo" | "claude" | "other";

/** Ready-to-paste configuration for each agent the team uses; `url` is the Streamable HTTP endpoint. */
export function clientConfig(client: Client, url: string, sseUrl: string): { where: string; code: string; note?: string }[] {
  const json = (o: unknown) => JSON.stringify(o, null, 2);
  switch (client) {
    case "opencode":
      return [{ where: "opencode.json in your project, or ~/.config/opencode/opencode.json for every project",
        code: json({ $schema: "https://opencode.ai/config.json", mcp: { specforge: { type: "remote", url, enabled: true } } }) }];
    case "roo":
      return [{ where: "Roo Code → MCP Servers → Edit Global MCP (mcp_settings.json), or .roo/mcp.json in your project",
        code: json({ mcpServers: { specforge: { type: "streamable-http", url, disabled: false } } }),
        note: "Older Roo Code versions without streamable-http: use \"type\": \"sse\" with the SSE URL below." }];
    case "claude":
      return [{ where: "Terminal (every project for your user)", code: `claude mcp add --transport http --scope user specforge ${url}` },
        { where: "or .mcp.json in your project (shared with the team through git)", code: json({ mcpServers: { specforge: { type: "http", url } } }) }];
    default:
      return [{ where: "Any MCP client with Streamable HTTP", code: url },
        { where: "Clients that only speak the older HTTP+SSE transport", code: sseUrl }];
  }
}

const AGENT_RULE = `When you need details from the NVMe / NVMe-MI specifications (command fields, CDW layouts, data structures, log pages,
status values, or what changed between versions), use the specforge MCP tools instead of answering from memory.
Prefer get_command_field / get_structure / diff over search_spec, and cite the figure or section and the spec version you used.`;

function CopyBlock({ code }: { code: string }) {
  const [done, setDone] = useState(false);
  return (
    <div style={{ position: "relative" }}>
      <pre style={{ paddingRight: 44 }}>{code}</pre>
      <button className="sm ghost" aria-label="Copy" title="Copy" style={{ position: "absolute", top: 4, right: 4 }}
        onClick={() => navigator.clipboard.writeText(code).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); })}>
        {done ? <Check size={14} /> : <Copy size={14} />}
      </button>
    </div>
  );
}

function Guide() {
  const info = useQuery({ queryKey: ["mcp-info"], queryFn: api.mcpInfo });
  const [client, setClient] = useState<Client>("opencode");
  const probe = useMutation({ mutationFn: api.mcpProbe });
  const d = info.data;
  if (!d) return <Loading q={info} />;
  return (
    <>
      {!d.enabled && <div className="card" role="alert"><strong>The MCP endpoint is turned off</strong> by an administrator; agents get an error until it is turned on again.</div>}
      <div className="card">
        <h2>1. The endpoint</h2>
        <p className="muted">No account or API key: every agent on the network uses the same address. It only reads the parsed specifications.</p>
        <Field label="Streamable HTTP (OpenCode, Roo Code, Claude Code)"><CopyBlock code={d.url} /></Field>
        <Field label="HTTP+SSE (older clients only)"><CopyBlock code={d.sse_url} /></Field>
        <div className="row">
          <button onClick={() => probe.mutate()} disabled={probe.isPending}><PlugZap size={15} /> Test the endpoint</button>
          {probe.isSuccess && <span className="badge ok">Answering: {probe.data} tools</span>}
          {probe.isError && <span className="badge bad">{String((probe.error as Error).message)}</span>}
          <span className="muted small">At most {d.rate_limit_per_min || "unlimited"} requests per minute from one machine.</span>
        </div>
      </div>
      <div className="card">
        <h2>2. Add it to your agent</h2>
        <Segmented label="Agent" value={client} onChange={setClient}
          options={[{ value: "opencode", label: "OpenCode" }, { value: "roo", label: "Roo Code" }, { value: "claude", label: "Claude Code" }, { value: "other", label: "Other" }]} />
        <div style={{ marginTop: 12 }}>
          {clientConfig(client, d.url, d.sse_url).map((c) => (
            <Field key={c.where} label={c.where} hint={c.note}><CopyBlock code={c.code} /></Field>
          ))}
        </div>
        <p className="muted small">Restart the agent (or reload its MCP servers) after editing the file; the <code>specforge</code> server should list {d.tools.length} tools.</p>
      </div>
      <div className="card">
        <h2>3. Tell the agent when to use it (optional)</h2>
        <p className="muted">Add this to the project's agent rules (AGENTS.md, CLAUDE.md or .roo/rules) so it asks the specs instead of guessing:</p>
        <CopyBlock code={AGENT_RULE} />
        <p className="muted small">Try asking your agent: <em>"What is in Command Dword 10 of the Identify command in NVMe Base 2.4?"</em> or <em>"What changed in Identify between the previous version and 2.4?"</em></p>
      </div>
      <div className="card">
        <h2>Tools</h2>
        <div className="scroll"><table className="t">
          <thead><tr><th>Tool</th><th>What it answers</th><th>Parameters</th></tr></thead>
          <tbody>{d.tools.map((t) => (
            <tr key={t.name}><td className="mono">{t.name}</td><td className="small">{t.description}</td>
              <td className="small mono">{t.params.map((p) => (t.required.includes(p) ? `${p}*` : p)).join(", ")}</td></tr>
          ))}</tbody>
        </table></div>
        <p className="muted small">* required. Resources: <code>specforge://specs</code> and <code>specforge://&lt;spec&gt;@&lt;version&gt;/sec:&lt;n&gt;</code> or <code>/fig:&lt;n&gt;</code>.</p>
      </div>
      <div className="card">
        <h2>Specifications served ({d.specs.length})</h2>
        {d.specs.length === 0 ? <p className="muted">None parsed yet: parse specifications on the Specs page and they are served at once.</p> :
          <p className="small">{d.specs.map((s) => `${s.family} ${s.version}`).join(" · ")}</p>}
        <p className="muted small">Without a version, agents get the latest parsed version (the answer says which). Comparisons agents ask for are computed once on this server and kept.</p>
      </div>
    </>
  );
}

const ago = (ts?: number | null) => {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function Calls({ title, items, empty }: { title: string; items: McpCall[]; empty: string }) {
  return (
    <div className="card flush">
      <div style={{ padding: "12px 16px 0" }}><h2>{title}</h2></div>
      {items.length === 0 ? <p className="muted small" style={{ padding: "0 16px 12px" }}>{empty}</p> : (
        <div className="scroll" style={{ maxHeight: 320 }}><table className="t">
          <thead><tr><th>When</th><th>Tool</th><th>Arguments</th><th>Result</th><th style={{ textAlign: "right" }}>ms</th><th>From</th></tr></thead>
          <tbody>{items.map((c, i) => (
            <tr key={`${c.ts}-${i}`}><td className="small muted">{ago(c.ts)}</td><td className="mono small">{c.tool}</td><td className="mono small">{c.args}</td>
              <td className="small"><span className={`badge ${c.outcome === "ok" ? "ok" : c.outcome === "miss" ? "warn" : "bad"}`}>{c.outcome}</span>{c.error && <span className="muted"> {c.error}</span>}</td>
              <td className="small" style={{ textAlign: "right" }}>{c.ms}</td><td className="mono small">{c.ip}</td></tr>
          ))}</tbody>
        </table></div>
      )}
    </div>
  );
}

function SettingsCard({ s }: { s: McpSettings }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [rate, setRate] = useState(String(s.rate_limit_per_min));
  const [wait, setWait] = useState(String(s.compare_wait_s));
  const [url, setUrl] = useState(s.public_url ?? "");
  const save = useMutation({
    mutationFn: api.mcpSettings,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["mcp-status"] }); qc.invalidateQueries({ queryKey: ["mcp-info"] }); toast.push({ title: "MCP settings saved", tone: "ok" }); },
    onError: (e) => toast.push({ title: "Not saved", description: (e as Error).message, tone: "bad" }),
  });
  return (
    <div className="card">
      <h2>Settings</h2>
      <div className="row" style={{ marginBottom: 10 }}>
        <Switch checked={s.enabled} label="MCP endpoint on" onChange={(v) => save.mutate({ enabled: v })} />
        <span>{s.enabled ? "Endpoint on: agents can connect" : "Endpoint off: agents get an error"}</span>
      </div>
      <div className="row" style={{ marginBottom: 10 }}>
        <Switch checked={s.log_calls} label="Log every call" onChange={(v) => save.mutate({ log_calls: v })} />
        <span>One Activity-log line per call (errors are always logged)</span>
      </div>
      <div className="row" style={{ marginBottom: 4 }}>
        <Switch checked={s.library_search ?? true} label="Search converted documents with the index" onChange={(v) => save.mutate({ library_search: v })} />
        <span>{(s.library_search ?? true) ? "Converted documents searched with the index" : "Converted documents not searched: agents read them with read_raw only"}</span>
      </div>
      <details className="small" style={{ margin: "0 0 12px 46px" }}>
        <summary>What this switch does</summary>
        <div className="muted" style={{ marginTop: 6 }}>
          <p><b>Which documents it affects.</b> Only documents converted on the To Markdown page that are <i>not</i> parsed specifications. Parsed specifications are always searched through their own structured index; this switch does not touch them, nor get_command, get_structure and the other lookups.</p>
          <p><b>On (default).</b> search_spec also searches every converted document: its section text, tables and individual table rows. Each hit names the section number or line range and the exact read_raw call that opens just that part (for example one row of Identify Controller, about 400 tokens, instead of the whole section, about 45k tokens). With spec set to a file name, search_spec searches that one file.</p>
          <p><b>Off.</b> search_spec answers from parsed specifications only and says that converted documents are not searched; naming a converted file in search_spec returns a hint to use read_raw. Agents can still read converted documents with read_raw: the whole file page by page, around a phrase, one section, or a line range. This is how the server behaved before the index existed, so you can ask an agent the same question both ways and compare the answers, tokens used and number of calls.</p>
          <p><b>Indexing continues either way.</b> New conversions are indexed, deleted ones are removed and Rebuild works whether the switch is on or off, so turning it back on takes effect within 2 seconds, with nothing to rebuild.</p>
        </div>
      </details>
      <div className="grid">
        <Field label="Requests per minute per machine" hint="0 = no limit"><input type="number" min={0} value={rate} onChange={(e) => setRate(e.target.value)} /></Field>
        <Field label="Wait for a new comparison (s)" hint="then the agent is told to retry"><input type="number" min={1} max={600} value={wait} onChange={(e) => setWait(e.target.value)} /></Field>
        <Field label="Public URL" hint="what the guide shows, e.g. http://specforge.lab:8000 (empty: the address you opened)"><input value={url} placeholder="http://host:port" onChange={(e) => setUrl(e.target.value)} /></Field>
      </div>
      <button className="primary" disabled={save.isPending}
        onClick={() => save.mutate({ rate_limit_per_min: Number(rate), compare_wait_s: Number(wait), public_url: url })}>Save</button>
    </div>
  );
}

function Status() {
  const qc = useQueryClient();
  const toast = useToast();
  const st = useQuery({ queryKey: ["mcp-status"], queryFn: api.mcpStatus, refetchInterval: 5000 });
  const reindex = useMutation({ mutationFn: api.mcpReindex, onSuccess: (r) => toast.push({ title: r.reused ? "Index rebuild already running" : "Rebuilding the search index", tone: "info", to: "/activity?tab=jobs", toLabel: "Jobs" }) });
  const reset = useMutation({ mutationFn: api.mcpResetStats, onSuccess: () => qc.invalidateQueries({ queryKey: ["mcp-status"] }) });
  const d: McpStatus | undefined = st.data;
  if (!d) return <Loading q={st} />;
  const u = d.usage;
  return (
    <>
      <div className="grid" style={{ marginBottom: 16 }}>
        <div className="card"><div className="muted small">Endpoint</div><h2>{!d.settings.enabled ? <span className="badge bad">Off</span> : d.running ? <span className="badge ok">Serving</span> : <span className="badge warn">Starting</span>}</h2><div className="small mono">{d.url}</div></div>
        <div className="card"><div className="muted small">Machines (active in the last 15 min)</div><h2>{u.client_count} ({u.active_clients})</h2></div>
        <div className="card"><div className="muted small">Tool calls</div><h2>{u.calls}</h2><div className="small muted">since {new Date(u.since * 1000).toLocaleString()}</div></div>
        <div className="card"><div className="muted small">Errors · not found · rate limited</div><h2>{u.errors} · {u.misses} · {u.rate_limited}</h2></div>
      </div>
      <SettingsCard key={JSON.stringify(d.settings)} s={d.settings} />
      <div className="card flush">
        <div className="card-head" style={{ padding: "12px 16px 0" }}><h2 className="grow">Calls per tool</h2><button onClick={() => reset.mutate()}>Reset figures</button></div>
        {u.tools.length === 0 ? <p className="muted small" style={{ padding: "0 16px 12px" }}>No calls since the server started.</p> : (
          <div className="scroll"><table className="t">
            <thead><tr><th>Tool</th><th>Calls</th><th>Errors</th><th>Not found</th><th>Slow (≥ 2 s)</th><th>Avg ms</th><th>Max ms</th><th>Last</th></tr></thead>
            <tbody>{u.tools.map((t) => <tr key={t.tool}><td className="mono">{t.tool}</td><td>{t.calls}</td><td>{t.errors}</td><td>{t.misses}</td><td>{t.slow}</td><td>{t.avg_ms}</td><td>{t.max_ms}</td><td className="small muted">{ago(t.last)}</td></tr>)}</tbody>
          </table></div>
        )}
      </div>
      <div className="card flush">
        <div style={{ padding: "12px 16px 0" }}><h2>Machines</h2><p className="muted small">By network address only (no names are recorded).</p></div>
        {u.clients.length === 0 ? <p className="muted small" style={{ padding: "0 16px 12px" }}>No agent has connected since the server started.</p> : (
          <div className="scroll" style={{ maxHeight: 320 }}><table className="t">
            <thead><tr><th>Address</th><th>Status</th><th>Requests</th><th>Tool calls</th><th>Rate limited</th><th>First seen</th><th>Last seen</th></tr></thead>
            <tbody>{u.clients.map((c) => <tr key={c.ip}><td className="mono">{c.ip}</td><td>{c.active ? <span className="badge ok">active</span> : <span className="badge">idle</span>}</td><td>{c.requests}</td><td>{c.calls}</td><td>{c.limited}</td><td className="small muted">{ago(c.first_seen)}</td><td className="small muted">{ago(c.last_seen)}</td></tr>)}</tbody>
          </table></div>
        )}
      </div>
      <Calls title="Not found (what agents looked for and did not get)" items={u.recent_misses} empty="No misses." />
      <Calls title="Errors" items={u.recent_errors} empty="No errors." />
      <Calls title="Slow calls" items={u.recent_slow} empty="No call took 2 s or more." />
      <Calls title="Recent calls" items={u.recent} empty="No calls yet." />
      <div className="card flush">
        <div className="card-head" style={{ padding: "12px 16px 0" }}>
          <h2 className="grow">Search index</h2>
          <button onClick={() => reindex.mutate()} disabled={reindex.isPending || d.index_job !== null}><RefreshCw size={14} /> {d.index_job !== null ? "Rebuilding…" : "Rebuild"}</button>
        </div>
        <p className="muted small" style={{ padding: "0 16px" }}>One entry per section, figure and table row of the version each spec serves, and of each converted document that is not parsed (section text in pieces, tables and table rows from its Markdown). Built at startup and on first use; rebuild only if search results look stale. {d.comparisons_by_mcp} comparison(s) computed for agents so far.</p>
        {d.documents.length === 0 ? <p className="muted small" style={{ padding: "0 16px 12px" }}>No parsed specifications.</p> : (
          <div className="scroll"><table className="t">
            <thead><tr><th>Specification</th><th>Version</th><th>Parse run</th><th>Index</th><th>Entries</th><th>Built</th></tr></thead>
            <tbody>{d.documents.map((x) => <tr key={x.run_id}><td className="mono">{x.family}</td><td>{x.version}</td><td>#{x.run_id}</td>
              <td><span className={`badge ${x.index === "current" ? "ok" : "warn"}`}>{x.index === "current" ? "ready" : x.index === "missing" ? "built on first use" : "outdated"}</span></td>
              <td>{x.entries}</td><td className="small muted">{x.built ?? "—"}{x.seconds != null ? ` (${x.seconds} s)` : ""}</td></tr>)}</tbody>
          </table></div>
        )}
        {(d.library ?? []).length > 0 && (
          <div className="scroll"><table className="t">
            <thead><tr><th>Converted document (not parsed)</th><th>Index</th><th>Entries</th><th>Built</th></tr></thead>
            <tbody>{d.library.map((x) => <tr key={x.dir}><td className="mono">{x.filename}</td>
              <td><span className={`badge ${x.index === "current" ? "ok" : "warn"}`}>{x.index === "current" ? "ready" : x.index === "missing" ? "built on first use" : "outdated"}</span></td>
              <td>{x.entries}</td><td className="small muted">{x.built ?? "—"}{x.seconds != null ? ` (${x.seconds} s)` : ""}</td></tr>)}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}

/** The MCP endpoint: setup guide for everyone, usage and settings for admins. */
export default function AgentsPage() {
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();
  const tab = isAdmin && params.get("tab") === "status" ? "status" : "guide";
  const setTab = (t: "guide" | "status") => { const n = new URLSearchParams(params); t === "status" ? n.set("tab", "status") : n.delete("tab"); setParams(n); };
  return (
    <>
      <PageHeader title="MCP for agents"
        description="Your coding agent can look up the parsed NVMe specifications on this server: command fields, data structures, sections, figures, and what changed between versions, with figure and page citations." />
      {isAdmin && (
        <div className="tabs" role="tablist">
          <button role="tab" aria-selected={tab === "guide"} className={tab === "guide" ? "active" : ""} onClick={() => setTab("guide")}>Setup guide</button>
          <button role="tab" aria-selected={tab === "status"} className={tab === "status" ? "active" : ""} onClick={() => setTab("status")}>Status (admin)</button>
        </div>
      )}
      {tab === "guide" ? <Guide /> : <Status />}
    </>
  );
}

