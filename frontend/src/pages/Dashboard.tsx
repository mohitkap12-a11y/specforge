import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Legend, Loading, Pct, StateBar } from "../components/ui";
import type { CovRun, SpecSet } from "../types";

/** Covered-% per run as a small bar chart (oldest -> newest). Bars are labelled for screen readers. */
function Trend({ runs }: { runs: CovRun[] }) {
  const pts = [...runs].reverse().slice(-12);
  if (pts.length < 2) return null;
  return (
    <div className="row" style={{ alignItems: "flex-end", height: 44, gap: 4 }} role="img"
      aria-label={`Covered percentage over the last ${pts.length} runs: ${pts.map((r) => (r.overall?.pct_covered ?? 0).toFixed(0) + "%").join(", ")}`}>
      {pts.map((r) => (
        <div key={r.id} title={`Run #${r.id}: ${r.overall?.pct_covered?.toFixed(1) ?? "–"}%`}
          style={{ width: 14, height: `${Math.max(3, r.overall?.pct_covered ?? 0) * 0.44}px`, background: "var(--accent)", borderRadius: 2 }} />
      ))}
    </div>
  );
}

function SetTile({ set }: { set: SetSpec }) {
  const runs = useQuery({ queryKey: ["covruns", set.id], queryFn: () => api.covRuns(set.id) });
  const latest = runs.data?.[0];
  const summary = useQuery({ queryKey: ["covsum", latest?.id], enabled: !!latest, queryFn: () => api.covSummary(latest!.id) });
  const gaps = useQuery({ queryKey: ["covitems", latest?.id, "top-gaps"], enabled: !!latest, queryFn: () => api.covItems(latest!.id, { state: "not_covered", kind: "command", limit: 6 }) });
  const o = summary.data?.summary.overall;
  return (
    <div className="card">
      <div className="row"><h2 className="grow">{set.name}</h2><span className="muted small">{set.catalog_items} catalog items · {set.scenarios} scenarios</span></div>
      <Loading q={runs} />
      {runs.data && !latest && <div className="muted">No coverage run yet. <Link to="/coverage">Run coverage</Link></div>}
      {o && latest && (
        <>
          <div className="row"><div className="big"><Pct v={o.pct_covered} /></div><span className="muted small">covered · <Pct v={o.pct_incl_stimulus} /> incl. stimulus · run #{latest.id}</span><span className="grow" /><Trend runs={runs.data ?? []} /></div>
          <div style={{ margin: "8px 0" }}><StateBar c={o} /><Legend c={o} /></div>
          {gaps.data && gaps.data.items.length > 0 && (
            <>
              <h3>Uncovered commands</h3>
              <div className="small">{gaps.data.items.map((i) => <div key={i.key}>{i.label}</div>)}</div>
              {gaps.data.total > gaps.data.items.length && <Link className="small" to="/coverage">{gaps.data.total - gaps.data.items.length} more…</Link>}
            </>
          )}
        </>
      )}
    </div>
  );
}
type SetSpec = SpecSet;

export default function DashboardPage() {
  const sets = useQuery({ queryKey: ["sets"], queryFn: api.sets });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: api.jobs });
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const stale = (specs.data ?? []).filter((d) => d.runs.some((r) => r.active && r.outdated));
  return (
    <>
      <h1>Dashboard</h1>
      <ErrorNote error={sets.error ?? jobs.error} />
      {stale.length > 0 && <div className="toast bad">Parser updated: reparse available for {stale.map((d) => `${d.family} ${d.version}`).join(", ")}. See the <Link to="/specs">Specs</Link> page.</div>}
      <Loading q={sets} />
      {sets.data?.length === 0 && <Empty>Nothing here yet. Start on the <Link to="/specs">Specs</Link> page: upload and parse specs, then create a set.</Empty>}
      {sets.data?.map((s) => <SetTile key={s.id} set={s} />)}
      {jobs.data && jobs.data.length > 0 && (
        <div className="card">
          <h2>Recent jobs</h2>
          <table className="t"><tbody>{jobs.data.slice(0, 6).map((j) => (
            <tr key={j.id}><td>#{j.id}</td><td>{j.type}</td><td><Badge value={j.state} label={j.state} /></td><td className="small">{j.state === "failed" ? (j.error ?? "").split("\n")[0] : j.message ?? ""}</td></tr>
          ))}</tbody></table>
          <Link className="small" to="/jobs">All jobs</Link>
        </div>
      )}
    </>
  );
}
