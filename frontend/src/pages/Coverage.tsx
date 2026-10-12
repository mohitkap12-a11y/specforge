import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Legend, Loading, Pager, Pct, STATE_ORDER, StateBar, stateLabel } from "../components/ui";
import { SetPicker, useSelectedSet } from "../components/SetPicker";
import { useJobs } from "../components/jobs";
import { useDebounced, useStoredState } from "../hooks";
import type { CovItem } from "../types";

const LIMIT = 100;

function Evidence({ run, item, onClose }: { run: number; item: CovItem; onClose: () => void }) {
  const q = useQuery({ queryKey: ["evidence", run, item.key], queryFn: () => api.covEvidence(run, item.key) });
  return (
    <aside className="drawer" aria-label="Evidence">
      <div className="row"><h2 className="grow">{item.label}</h2><button onClick={onClose}>Close</button></div>
      <div className="row"><Badge value={item.state} /><span className="badge">{item.kind}</span><span className="badge">{item.observability}</span></div>
      <p className="mono small">{item.key}</p>
      {item.na_reason && <p className="muted">N/A reason: {item.na_reason}</p>}
      {item.total > 1 && <p>{item.covered} of {item.total} sub-items covered</p>}
      <Loading q={q} />
      {q.data?.length === 0 && <p className="muted">No scenarios are linked to this item in this run.</p>}
      {q.data?.map((s) => (
        <div key={s.id} className="card">
          <div className="row"><strong className="grow">{s.title}</strong><Badge value={s.result} label={s.result} /></div>
          <div className="small muted">{s.sid} · review: {s.state} · {s.match_count} matching command{s.match_count === 1 ? "" : "s"}</div>
          {s.evidence.length > 0 && (
            <div className="scroll" style={{ maxHeight: 220 }}>
              <table className="t"><thead><tr><th>Test</th><th>Row</th><th>Detail</th></tr></thead>
                <tbody>{s.evidence.slice(0, 50).map((e: any, i: number) => (
                  <tr key={i}><td>{e.test_id ?? e.test ?? "–"}</td><td>{e.src_row_id ?? e.row ?? "–"}</td>
                    <td className="mono small">{typeof e === "object" ? JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k]) => !["test_id", "test", "src_row_id", "row"].includes(k)))) : String(e)}</td></tr>
                ))}</tbody></table>
            </div>
          )}
        </div>
      ))}
    </aside>
  );
}

function RunView({ runId, prevId }: { runId: number; prevId: number | null }) {
  const sum = useQuery({ queryKey: ["covsum", runId], queryFn: () => api.covSummary(runId) });
  const [state, setState] = useState("");
  const [kind, setKind] = useState("");
  const [command, setCommand] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const [item, setItem] = useState<CovItem | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  useEffect(() => { setOffset(0); }, [runId, state, kind, command, dq]);
  useEffect(() => { setItem(null); setShowDiff(false); }, [runId]);
  const items = useQuery({ queryKey: ["covitems", runId, state, kind, command, dq, offset], queryFn: () => api.covItems(runId, { state, kind, command, q: dq, offset, limit: LIMIT }) });
  const diff = useQuery({ queryKey: ["covdiff", prevId, runId], enabled: showDiff && prevId !== null, queryFn: () => api.covDiff(prevId!, runId) });
  if (sum.isLoading || sum.error || !sum.data) return <Loading q={sum} />;
  const s = sum.data.summary;
  const o = s.overall;
  const cmds = Object.entries(s.by_command).sort((a, b) => (a[1].pct_covered ?? -1) - (b[1].pct_covered ?? -1));
  return (
    <>
      <div className="grid">
        <div className="card"><div className="muted small">Covered</div><div className="big"><Pct v={o.pct_covered} /></div><div className="small muted">of {o.denominator} observable items</div></div>
        <div className="card"><div className="muted small">Incl. stimulus-only</div><div className="big"><Pct v={o.pct_incl_stimulus} /></div></div>
        <div className="card"><div className="muted small">Gaps</div><div className="big">{o.not_covered + o.partial + o.no_scenario + o.needs_review}</div>
          <div className="small muted">{o.no_scenario} without scenario · {o.needs_review} awaiting review</div></div>
        <div className="card"><div className="muted small">Scenario errors</div><div className="big">{s.scenario_errors}</div></div>
      </div>
      <div className="card"><StateBar c={o} /><Legend c={o} /></div>

      <div className="card">
        <h2>By kind</h2>
        <table className="t"><thead><tr><th>Kind</th><th style={{ width: "40%" }}>Coverage</th><th>Covered</th><th>Items</th></tr></thead>
          <tbody>{Object.entries(s.by_kind).map(([k, c]) => (
            <tr key={k} className="click" onClick={() => setKind(kind === k ? "" : k)}><td>{k}{kind === k && " ✓"}</td><td><StateBar c={c} /></td><td><Pct v={c.pct_covered} /></td><td>{c.total}</td></tr>
          ))}</tbody></table>
      </div>

      <div className="card">
        <h2>By command <span className="muted small">(least covered first; click to filter)</span></h2>
        <div className="scroll" style={{ maxHeight: 340 }}>
          <table className="t"><thead><tr><th>Command</th><th style={{ width: "40%" }}>Coverage</th><th>Covered</th><th>Items</th></tr></thead>
            <tbody>{cmds.map(([k, c]) => (
              <tr key={k} className={`click ${command === k ? "sel" : ""}`} onClick={() => setCommand(command === k ? "" : k)}>
                <td className="small">{k.split("/").pop()}</td><td><StateBar c={c} /></td><td><Pct v={c.pct_covered} /></td><td>{c.total}</td></tr>
            ))}</tbody></table>
        </div>
      </div>

      {s.unknown_commands.length > 0 && (
        <div className="card">
          <h2>Commands in the DB that are not in the catalog</h2>
          <table className="t"><thead><tr><th>Interface</th><th>Queue</th><th>Opcode</th><th>Commands</th></tr></thead>
            <tbody>{s.unknown_commands.slice(0, 20).map((u, i) => <tr key={i}><td>{u.interface}</td><td>{u.queue}</td><td className="mono">0x{u.opcode.toString(16).padStart(2, "0")}</td><td>{u.commands}</td></tr>)}</tbody></table>
        </div>
      )}

      <div className="card">
        <div className="row" style={{ marginBottom: 8 }}>
          <h2 className="grow">Items</h2>
          {prevId !== null && <button onClick={() => setShowDiff((d) => !d)}>{showDiff ? "Hide" : "Compare with"} run #{prevId}</button>}
        </div>
        {showDiff && <><Loading q={diff} />{diff.data && (
          <div className="toast ok">vs run #{prevId}: {diff.data.gained.length} gained, {diff.data.lost.length} lost
            {diff.data.lost.length > 0 && <div className="small">Lost: {diff.data.lost.slice(0, 10).join(", ")}{diff.data.lost.length > 10 && " …"}</div>}</div>)}</>}
        <div className="row" style={{ marginBottom: 8 }}>
          <select value={state} onChange={(e) => setState(e.target.value)} aria-label="State">
            <option value="">All states</option>{STATE_ORDER.map((st) => <option key={st} value={st}>{stateLabel(st)}</option>)}
          </select>
          <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind">
            <option value="">All kinds</option>{Object.keys(s.by_kind).map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          {command && <button onClick={() => setCommand("")}>Command: {command.split("/").pop()} ×</button>}
          <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <Loading q={items} />
        {items.data && (
          <>
            <div className="scroll">
              <table className="t"><thead><tr><th>State</th><th>Kind</th><th>Item</th><th>Observability</th><th>Hit</th></tr></thead>
                <tbody>{items.data.items.map((it) => (
                  <tr key={it.key} className="click" onClick={() => setItem(it)}>
                    <td><Badge value={it.state} /></td><td>{it.kind}</td><td>{it.label}<div className="mono small muted">{it.key.split("/").slice(1).join("/")}</div></td>
                    <td className="small">{it.observability}</td><td>{it.total > 1 ? `${it.covered}/${it.total}` : ""}</td>
                  </tr>
                ))}</tbody></table>
            </div>
            <Pager total={items.data.total} offset={offset} limit={LIMIT} onChange={setOffset} />
          </>
        )}
      </div>
      {item && <Evidence run={runId} item={item} onClose={() => setItem(null)} />}
    </>
  );
}

export default function CoveragePage() {
  const sel = useSelectedSet();
  const { track } = useJobs();
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const runs = useQuery({ queryKey: ["covruns", sel.setId], enabled: sel.setId !== null, queryFn: () => api.covRuns(sel.setId!) });
  const [srcId, setSrcId] = useStoredState<number | null>("specforge.source", null);
  const [runSel, setRunSel] = useState<number | null>(null);
  const imported = (sources.data ?? []).filter((s) => s.status === "imported" || s.decoded_count > 0);
  const source = imported.find((s) => s.id === srcId) ?? imported[0];
  const list = runs.data ?? [];
  const runId = list.some((r) => r.id === runSel) ? runSel : list[0]?.id ?? null;
  const prevId = list.find((r) => runId !== null && r.id < runId)?.id ?? null;
  const run = useMutation({
    mutationFn: () => api.runCoverage(sel.setId!, source!.id),
    onSuccess: (r) => track(r.job, "Coverage run", (j) => { const id = j.result?.run_id; if (typeof id === "number") setRunSel(id); }),
  });
  return (
    <>
      <h1>Coverage</h1>
      <div className="card row">
        <SetPicker sel={sel} />
        <label>Command DB{" "}
          <select value={source?.id ?? ""} onChange={(e) => setSrcId(Number(e.target.value))} disabled={!imported.length}>
            {imported.map((s) => <option key={s.id} value={s.id}>{s.name || s.path} ({s.decoded_count} commands)</option>)}
          </select>
        </label>
        <button className="primary" disabled={sel.setId === null || !source || run.isPending} onClick={() => run.mutate()}>Run coverage</button>
        {list.length > 0 && (
          <label>Run{" "}
            <select value={runId ?? ""} onChange={(e) => setRunSel(Number(e.target.value))}>
              {list.map((r) => <option key={r.id} value={r.id}>#{r.id} · {r.created} · {r.overall?.pct_covered != null ? `${r.overall.pct_covered.toFixed(1)}%` : "–"}</option>)}
            </select>
          </label>
        )}
      </div>
      <ErrorNote error={sel.error ?? sources.error ?? runs.error ?? run.error} />
      {sources.data && !imported.length && (
        <Empty>No imported command DB yet. Add one on the <Link to="/cmddb">Command DBs</Link> page.</Empty>
      )}
      {sel.setId !== null && runs.data && list.length === 0 && imported.length > 0 && <Empty>No coverage runs for this set yet. Click “Run coverage”.</Empty>}
      {runId !== null && <RunView runId={runId} prevId={prevId} />}
    </>
  );
}
