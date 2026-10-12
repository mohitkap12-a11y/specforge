import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ArrowRight, Bot, Check, FileUp, Layers, ShieldCheck, Sparkles } from "lucide-react";
import { api } from "../api";
import { Badge, ErrorNote, Legend, Loading, PageHeader, Pct, StateBar } from "../components/ui";
import { rememberSet } from "../hooks";
import type { CovRun, LogEvent, SpecSet } from "../types";

/** Covered-% per run as a small bar chart (oldest -> newest). Bars are labelled for screen readers. */
function Trend({ runs }: { runs: CovRun[] }) {
  const pts = [...runs].reverse().slice(-12);
  if (pts.length < 2) return null;
  return (
    <div className="row" style={{ alignItems: "flex-end", height: 44, gap: 4 }} role="img"
      aria-label={`Covered percentage over the last ${pts.length} runs: ${pts.map((r) => (r.overall?.pct_covered == null ? "n/a" : r.overall.pct_covered.toFixed(0) + "%")).join(", ")}`}>
      {pts.map((r) => {
        const v = r.overall?.pct_covered;   // null when a run has no observable items: show "unavailable", not 0%
        return (
          <div key={r.id} title={`Run #${r.id}: ${v == null ? "n/a" : `${v.toFixed(1)}%`}`}
            style={{ width: 14, height: v == null ? 2 : `${Math.max(3, v) * 0.44}px`, background: v == null ? "var(--border-strong)" : "var(--link)", borderRadius: 2 }} />
        );
      })}
    </div>
  );
}

function SetTile({ set }: { set: SpecSet }) {
  const runs = useQuery({ queryKey: ["covruns", set.id], queryFn: () => api.covRuns(set.id) });
  const latest = runs.data?.[0];
  const summary = useQuery({ queryKey: ["covsum", latest?.id], enabled: !!latest, queryFn: () => api.covSummary(latest!.id) });
  const gaps = useQuery({ queryKey: ["covitems", latest?.id, "top-gaps"], enabled: !!latest, queryFn: () => api.covItems(latest!.id, { state: "not_covered", kind: "command", limit: 6 }) });
  const o = summary.data?.summary.overall;
  return (
    <div className="card">
      <div className="card-head"><h2 className="grow" style={{ margin: 0 }}>{set.name}</h2><span className="muted small">{set.catalog_items} catalog items · {set.scenarios} scenarios</span></div>
      <Loading q={runs} />
      {latest && <ErrorNote error={summary.error ?? gaps.error} />}
      {runs.data && !latest && <div className="muted">No coverage run yet. <Link to="/coverage" onClick={() => rememberSet(set.id)}>Run coverage →</Link></div>}
      {o && latest && (
        <>
          <div className="row"><div className="big"><Pct v={o.pct_covered} /></div><span className="muted small">covered · <Pct v={o.pct_incl_stimulus} /> incl. stimulus · run #{latest.id}</span><span className="grow" /><Trend runs={runs.data ?? []} /></div>
          <div style={{ margin: "10px 0 0" }}><StateBar c={o} /><Legend c={o} /></div>
          {gaps.data && gaps.data.items.length > 0 && (
            <>
              <h3>Uncovered commands</h3>
              <div className="small">{gaps.data.items.map((i) => <div key={i.key}>{i.label}</div>)}</div>
              {gaps.data.total > gaps.data.items.length && <Link className="small" to="/coverage" onClick={() => rememberSet(set.id)}>{gaps.data.total - gaps.data.items.length} more →</Link>}
            </>
          )}
        </>
      )}
    </div>
  );
}

const dotFor = (l: string) => (l === "error" ? "bad" : l === "warn" ? "warn" : l === "debug" ? "" : "info");
const clock = (ts: string) => new Date(ts).toLocaleTimeString([], { hour12: false });

function RecentActivity() {
  const q = useQuery({ queryKey: ["logs-recent"], queryFn: () => api.logs({ limit: 8, level: "info" }), refetchInterval: 4000 });
  const items: LogEvent[] = [...(q.data?.items ?? [])].reverse();
  return (
    <div className="card">
      <div className="card-head"><h2 className="grow" style={{ margin: 0 }}>Recent activity</h2><Link className="small" to="/activity">Open live log →</Link></div>
      <Loading q={q} />
      {q.data && items.length === 0 && <div className="muted">Nothing has happened yet.</div>}
      <div className="stack" style={{ gap: 6 }}>
        {items.map((e) => (
          <div key={e.id} className="row" style={{ flexWrap: "nowrap", alignItems: "baseline" }}>
            <span className="mono muted" style={{ flex: "none" }}>{clock(e.ts)}</span>
            <span className={`dot ${dotFor(e.level)}`} style={{ flex: "none", alignSelf: "center" }} />
            <span className={`src s-${e.source} mono`} style={{ flex: "none", padding: "0 6px", borderRadius: 4, fontSize: 11 }}>{e.source}</span>
            <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={e.message}>{e.message}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AiCheck() {
  const [go, setGo] = useState(false);
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const provider: string = settings.data?.tasks?.generate?.provider ?? "ollama";
  const model: string = settings.data?.tasks?.generate?.model ?? "";
  const q = useQuery({ queryKey: ["models", provider, "overview"], queryFn: () => api.providerModels(provider), enabled: go, staleTime: 0 });
  return (
    <div className="card">
      <div className="card-head"><Bot size={16} /><h2 style={{ margin: 0 }}>AI scenario drafting</h2></div>
      <p className="muted">Optional. A local model drafts scenarios from normative statements; nothing counts until you approve it.</p>
      <div className="row small" style={{ marginBottom: 10 }}>
        <span className="muted">Provider</span><strong>{provider}</strong>
        <span className="muted">Model</span>{model ? <strong>{model}</strong> : <Badge value="warning" label="not selected" />}
      </div>
      <div className="row">
        <button onClick={() => (go ? q.refetch() : setGo(true))} disabled={q.isFetching}>{q.isFetching ? <span className="spinner" /> : null}Check connection</button>
        <Link to="/settings#ai" className="small">Configure →</Link>
      </div>
      {q.data?.ok && <div className="toast ok">Connected · {q.data.models.length} model(s) available.</div>}
      {q.data && !q.data.ok && <div className="toast bad">{q.data.error}</div>}
      <ErrorNote error={q.error} />
    </div>
  );
}

export default function OverviewPage() {
  const nav = useNavigate();
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const sets = useQuery({ queryKey: ["sets"], queryFn: api.sets });
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const covRuns = useQuery({ queryKey: ["covruns", "all"], queryFn: () => api.covRuns() });
  const stale = (specs.data ?? []).filter((d) => d.runs.some((r) => r.active && r.outdated));

  if (specs.isLoading || sets.isLoading) return <><PageHeader title="Overview" /><Loading q={specs} /></>;
  if (specs.error) return <><PageHeader title="Overview" /><ErrorNote error={specs.error} /></>;     // never mistake a failed load for an empty workspace
  const docs = specs.data ?? [];
  const setList = sets.data ?? [];
  const steps = [
    { icon: FileUp, title: "Upload specifications", body: "Add the NVMe Base and NVM Command Set (and optionally NVMe-MI) PDFs or DOCX files.", done: docs.length > 0, to: "/specs", cta: "Upload specs", detail: `${docs.length} uploaded` },
    { icon: Sparkles, title: "Parse them", body: "Extract sections, tables, bit fields and normative statements, with an audit of what was found.", done: docs.some((d) => d.runs.some((r) => r.status === "done")), to: "/specs", cta: "Parse", detail: `${docs.filter((d) => d.runs.some((r) => r.status === "done")).length} of ${docs.length} parsed` },
    { icon: Layers, title: "Create a spec set", body: "Group the spec versions that reference each other and resolve cross-references.", done: setList.length > 0, to: "/sets", cta: "Create set", detail: `${setList.length} set(s)` },
    { icon: Check, title: "Build the catalog", body: "List every command, field, value and reserved bit. This is the coverage denominator.", done: setList.some((s) => s.catalog_items > 0), to: "/sets", cta: "Build catalog", detail: `${setList.reduce((n, s) => n + s.catalog_items, 0)} items` },
    { icon: Bot, title: "Review scenarios", body: "Template scenarios are generated for you. Optionally draft more with a local model, then approve.", done: setList.some((s) => s.scenarios > 0), to: "/scenarios", cta: "Open scenarios", detail: `${setList.reduce((n, s) => n + s.scenarios, 0)} scenarios` },
    { icon: FileUp, title: "Import a command database", body: "Point at the SQLite dump of submitted commands and check the column mapping.", done: (sources.data ?? []).some((s) => s.status === "imported"), to: "/cmddb", cta: "Add database", detail: `${(sources.data ?? []).length} source(s)` },
    { icon: ShieldCheck, title: "Run coverage", body: "Match approved scenarios against the commands and see what is covered, partial or missing.", done: (covRuns.data ?? []).length > 0, to: "/coverage", cta: "Run coverage", detail: `${(covRuns.data ?? []).length} run(s)` },
  ];
  const firstOpen = steps.findIndex((s) => !s.done);
  const doneCount = steps.filter((s) => s.done).length;

  if (docs.length === 0) {
    return (
      <>
        <section className="hero">
          <span className="badge violet"><Sparkles size={12} /> Local-first · works offline</span>
          <h1>Know exactly what your NVMe tests cover.</h1>
          <p>SpecForge reads the NVMe specifications, builds the list of everything that can be tested, and matches it against the commands your test runs actually sent. Evidence for every verdict, and AI only drafts, never decides.</p>
          <div className="row">
            <button className="primary" style={{ height: 40, padding: "0 18px" }} onClick={() => nav("/specs")}>Upload your first spec <ArrowRight size={16} /></button>
            <Link to="/settings#ai" className="small">Set up AI first (optional)</Link>
          </div>
        </section>
        <h2 style={{ margin: "0 0 12px" }}>How it works</h2>
        <div className="steps">
          {steps.map((s, i) => (
            <div key={s.title} className={`step ${i === 0 ? "next" : ""}`}>
              <div className="num">{i + 1}</div>
              <div><h3>{s.title}</h3><p>{s.body}</p></div>
            </div>
          ))}
        </div>
        <div className="grid" style={{ marginTop: 24, gridTemplateColumns: "repeat(auto-fit, minmax(min(280px, 100%), 1fr))" }}>
          <AiCheck />
          <div className="card">
            <div className="card-head"><h2 style={{ margin: 0 }}>See everything it does</h2></div>
            <p className="muted">Every parse, AI call and decision is recorded. Open the Activity view to watch jobs and agents live, including the exact prompts and replies.</p>
            <Link to="/activity" className="small">Open activity log →</Link>
          </div>
        </div>
      </>
    );
  }

  return (
    <>
      <PageHeader title="Overview" description="Where things stand and what to do next."
        actions={<><Link to="/activity"><button>Activity</button></Link><Link to="/specs"><button className="primary">Upload spec</button></Link></>} />
      <ErrorNote error={specs.error ?? sets.error} />
      {stale.length > 0 && <div className="toast warn">Parser updated: reparse available for {stale.map((d) => `${d.family} ${d.version}`).join(", ")}. See <Link to="/specs">Specs</Link>.</div>}

      <div className="card">
        <div className="card-head">
          <h2 style={{ margin: 0 }}>{doneCount === steps.length ? "Setup complete" : "Getting started"}</h2>
          <div className="checklist-progress grow">
            <div className="progress" style={{ width: 160 }}><span style={{ width: `${(100 * doneCount) / steps.length}%`, background: doneCount === steps.length ? "var(--ok)" : undefined }} /></div>
            <span className="muted small">{doneCount} of {steps.length}</span>
          </div>
        </div>
        {doneCount < steps.length ? (
          <div className="steps">
            {steps.map((s, i) => (
              <div key={s.title} className={`step ${s.done ? "done" : i === firstOpen ? "next" : ""}`}>
                <div className="num">{s.done ? <Check size={15} /> : i + 1}</div>
                <div>
                  <h3>{s.title}</h3>
                  <p>{s.done ? s.detail : s.body}</p>
                  {!s.done && <Link to={s.to}><button className={i === firstOpen ? "primary sm" : "sm"}>{s.cta} <ArrowRight size={13} /></button></Link>}
                </div>
              </div>
            ))}
          </div>
        ) : <div className="muted small">Every step is done. Re-run coverage after approving more scenarios or importing new command data.</div>}
      </div>

      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(150px, 100%), 1fr))", marginBottom: 16 }}>
        <div className="card stat"><div className="label">Specs parsed</div><div className="big">{docs.filter((d) => d.runs.some((r) => r.status === "done")).length}<span className="muted" style={{ fontSize: 16 }}> / {docs.length}</span></div></div>
        <div className="card stat"><div className="label">Catalog items</div><div className="big">{setList.reduce((n, s) => n + s.catalog_items, 0)}</div></div>
        <div className="card stat"><div className="label">Scenarios</div><div className="big">{setList.reduce((n, s) => n + s.scenarios, 0)}</div></div>
        <div className="card stat"><div className="label">Coverage runs</div><div className="big">{(covRuns.data ?? []).length}</div></div>
      </div>

      {setList.map((s) => <SetTile key={s.id} set={s} />)}
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(340px, 100%), 1fr))" }}>
        <RecentActivity />
        <AiCheck />
      </div>
    </>
  );
}
