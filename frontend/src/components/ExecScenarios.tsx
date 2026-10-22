import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Database, PlayCircle } from "lucide-react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading, Pager } from "./ui";
import { useJobs } from "./jobs";
import { useDebounced, useStoredState } from "../hooks";
import type { ExecAnalysis, ExecPredicate, ExecStep, Scenario, ScenarioLink } from "../types";

const LIMIT = 100;
export const SOURCE_KEY = "specforge.source";       // shared with the Coverage page

/** Field values as the spec writes them: 01h, 3FFh. */
export const fmtVal = (v: number) => `${v.toString(16).toUpperCase().padStart(2, "0")}h`;
const where = (st: ExecStep) => ({ admin: "Admin", io: "I/O", mi: "MI" } as Record<string, string>)[st.queue] ?? st.queue;
const KIND_LABEL: Record<string, string> = { command: "Commands", field: "Fields sent", value: "Values", boundary: "Boundaries (min / max)", reserved: "Reserved bits set", mi_tunnel: "Admin over NVMe-MI", requirement: "Requirements" };

/** Open the Scenarios page on the executed scenarios of this command DB (guarded: storage can be unavailable). */
export function rememberSource(id: number) {
  try { localStorage.setItem(SOURCE_KEY, JSON.stringify(id)); localStorage.setItem("specforge.scenarios.tab", JSON.stringify("executed")); } catch { /* ignore */ }
}

/** The selected command DB (imported ones only), remembered across pages. */
export function useSelectedSource() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [id, setId] = useStoredState<number | null>(SOURCE_KEY, null);
  const imported = (sources.data ?? []).filter((s) => s.status === "imported");
  const source = imported.find((s) => s.id === id) ?? imported[0];
  return { sources, imported, source, setId };
}

function StepRow({ st, i }: { st: ExecStep; i: number }) {
  return (
    <tr>
      <td className="muted">{i + 1}</td>
      <td>
        {st.command ? <strong>{st.name}</strong> : <Badge value="bad" label={st.name} />}
        <div className="small muted">{where(st)} {fmtVal(st.opcode)}{st.tunnel ? " · over NVMe-MI" : ""}</div>
      </td>
      <td>{st.count > 1 ? `×${st.count}` : ""}</td>
      <td className="small">
        {st.fields.map((f) => (
          <div key={f.key} title={`${f.dw}[${f.hi}:${f.lo}]`}>
            <span className="mono">{f.name}={fmtVal(f.value)}</span>{f.meaning && <span className="muted"> · {f.meaning}</span>}
            {f.unlisted && <> <Badge value="warning" label="value not in the spec table" /></>}
            {f.reserved_value && <> <Badge value="warning" label="reserved value" /></>}
          </div>
        ))}
        {st.ranges.map((r) => (
          <div key={r.key} className="mono" title={`${r.dw}[${r.hi}:${r.lo}]`}>
            {r.name}={r.min === r.max ? fmtVal(r.min) : `${fmtVal(r.min)}..${fmtVal(r.max)}`}
            {r.min !== r.max && <span className="muted"> ({Array.isArray(r.values) ? r.values.length : r.values} values)</span>}
          </div>
        ))}
        {st.reserved_set.map((r) => <div key={r.key} className="mono" style={{ color: "var(--warn)" }}>reserved {r.dw}[{r.hi}:{r.lo}]={fmtVal(r.value)}</div>)}
      </td>
    </tr>
  );
}

function RequirementLinks({ s, links }: { s: Scenario; links: ScenarioLink[] }) {
  const qc = useQueryClient();
  const review = useMutation({
    mutationFn: ({ key, state }: { key: string; state: "proposed" | "approved" | "rejected" }) => api.reviewLink(s.id, key, state),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["scenario-links", s.id] }); qc.invalidateQueries({ queryKey: ["scenarios"] }); qc.invalidateQueries({ queryKey: ["sets"] }); },
  });
  const reqs = links.filter((l) => l.state !== "observed");
  return (
    <>
      <h3>Requirements exercised ({reqs.filter((l) => l.state === "approved").length} approved{reqs.some((l) => l.state === "proposed") ? `, ${reqs.filter((l) => l.state === "proposed").length} to review` : ""})</h3>
      {reqs.length === 0 && <p className="muted small">A log of submitted commands cannot show which spec statements a test checks. “Describe with AI” proposes them from the requirements of the commands this flow used; they count toward coverage once you approve them.</p>}
      <ErrorNote error={review.error} />
      {reqs.map((l) => (
        <div key={l.key} className="req" style={{ opacity: l.state === "rejected" ? 0.6 : 1 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <Badge value={l.state} label={l.state} />
            <span className="row" style={{ gap: 4 }}>
              {l.state !== "approved" && <button className="sm primary" disabled={review.isPending} onClick={() => review.mutate({ key: l.key, state: "approved" })}>Approve</button>}
              {l.state !== "rejected" && <button className="sm danger" disabled={review.isPending} onClick={() => review.mutate({ key: l.key, state: "rejected" })}>Reject</button>}
              {l.state !== "proposed" && <button className="sm" disabled={review.isPending} onClick={() => review.mutate({ key: l.key, state: "proposed" })}>Undo</button>}
            </span>
          </div>
          <div>{l.text ?? l.label ?? l.key}</div>
          {l.detail.why && <div className="small muted">Why: {l.detail.why}</div>}
          <div className="mono small muted">{l.key}{l.observability === "unobservable" ? " · N/A in the catalog: does not count" : ""}</div>
        </div>
      ))}
    </>
  );
}

function Drawer({ s, setId, onClose }: { s: Scenario; setId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const { track } = useJobs();
  const p = s.predicate as ExecPredicate;
  const [title, setTitle] = useState(s.title);
  const [desc, setDesc] = useState(s.description ?? "");
  useEffect(() => { setTitle(s.title); setDesc(s.description ?? ""); }, [s.id, s.title, s.description]);
  const links = useQuery({ queryKey: ["scenario-links", s.id], queryFn: () => api.scenarioLinks(s.id) });
  const save = useMutation({ mutationFn: () => api.patchScenario(s.id, { title, description: desc }), onSuccess: () => qc.invalidateQueries({ queryKey: ["scenarios"] }) });
  const describe = useMutation({
    mutationFn: () => api.execDescribe(setId, s.source_id!, [s.id], true),
    onSuccess: (r) => track(r.job, "Describe executed scenario", () => qc.invalidateQueries({ queryKey: ["scenario-links", s.id] })),
  });
  const observed = (links.data ?? []).filter((l) => l.state === "observed");
  const byKind = new Map<string, ScenarioLink[]>();
  observed.forEach((l) => byKind.set(l.kind ?? "other", [...(byKind.get(l.kind ?? "other") ?? []), l]));
  const ai = s.provenance.ai as { model?: string; provider?: string } | undefined;
  const dirty = title !== s.title || desc !== (s.description ?? "");
  return (
    <aside className="drawer" aria-label="Executed scenario">
      <div className="row"><h2 className="grow" style={{ margin: 0 }}>Executed scenario</h2><button onClick={onClose}>Close</button></div>
      <div className="row small" style={{ margin: "8px 0" }}>
        <Badge value="observed" label="executed" />
        <span className="badge">{p.test_count} test{p.test_count === 1 ? "" : "s"}</span>
        <span className="badge">{p.commands} commands</span>
        <span className="badge">{p.steps.length}{p.truncated ? "+" : ""} steps</span>
        <span className="mono muted">{s.sid}</span>
      </div>
      <label>Title<input style={{ width: "100%" }} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>Description<textarea rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
      <div className="small muted">{ai ? `Described by ${ai.provider}/${ai.model}.` : s.provenance.edited ? "Edited by a reviewer." : "Generated from the log."} Your edits are kept when the command DB is analysed again.</div>
      <ErrorNote error={save.error ?? describe.error} />
      <div className="row" style={{ margin: "10px 0" }}>
        <button className="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save</button>
        <button disabled={describe.isPending || s.source_id === null} onClick={() => describe.mutate()}><Bot size={14} /> {ai ? "Describe again with AI" : "Describe with AI"}</button>
      </div>

      <h3>Tests that ran this flow</h3>
      <div className="small mono">{p.tests.slice(0, 40).join(", ")}{p.test_count > 40 && <span className="muted"> … +{p.test_count - 40} more</span>}</div>

      <h3>Steps</h3>
      <p className="small muted">Consecutive commands with the same values in their enumerated fields are one step; numeric fields (addresses, counts) are shown as the range used.</p>
      <div className="scroll" style={{ maxHeight: 420 }}>
        <table className="t"><thead><tr><th>#</th><th>Command</th><th>Times</th><th>Fields sent</th></tr></thead>
          <tbody>{p.steps.map((st, i) => <StepRow key={i} st={st} i={i} />)}</tbody></table>
      </div>
      {p.truncated && <div className="small muted">The log continues beyond the steps shown.</div>}

      <Loading q={links} />
      {links.data && <RequirementLinks s={s} links={links.data} />}
      {links.data && (
        <>
          <h3>Catalog items exercised ({observed.length})</h3>
          {[...byKind.entries()].map(([k, ls]) => (
            <details key={k}>
              <summary>{KIND_LABEL[k] ?? k} ({ls.length})</summary>
              <div className="small">{ls.map((l) => <div key={l.key}>{l.label ?? l.key} <span className="muted">· {l.detail.commands ?? 0} command(s)</span></div>)}</div>
            </details>
          ))}
        </>
      )}
    </aside>
  );
}

function AnalysisCard({ a }: { a: ExecAnalysis }) {
  return (
    <div className="card">
      <div className="card-head"><h2 className="grow" style={{ margin: 0 }}>What the tests executed</h2><span className="muted small">analysed {a.created}</span></div>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(130px, 100%), 1fr))" }}>
        <div className="stat"><div className="label">Tests</div><div className="big">{a.tests}</div></div>
        <div className="stat"><div className="label">Commands</div><div className="big">{a.commands}</div></div>
        <div className="stat"><div className="label">Distinct flows</div><div className="big">{a.scenarios}</div></div>
        <div className="stat"><div className="label">Not in catalog</div><div className="big">{a.unknown_commands}</div></div>
      </div>
      <table className="t" style={{ marginTop: 10 }}><thead><tr><th>Catalog items</th><th>Exercised</th><th style={{ width: "45%" }} /></tr></thead>
        <tbody>{Object.entries(a.observed).filter(([k]) => k !== "field").map(([k, v]) => (     /* analyses saved before 'field' was dropped */
          <tr key={k}><td>{KIND_LABEL[k] ?? k}</td><td>{v.observed} / {v.total}</td>
            <td><div className="progress"><span style={{ width: `${v.total ? (100 * v.observed) / v.total : 0}%` }} /></div></td></tr>
        ))}</tbody></table>
      {a.unknown.length > 0 && (
        <details style={{ marginTop: 8 }}>
          <summary className="small">Opcodes in the log that are not in the catalog ({a.unknown.length})</summary>
          <table className="t"><thead><tr><th>Interface</th><th>Queue</th><th>Opcode</th><th>Commands</th></tr></thead>
            <tbody>{a.unknown.map((u, i) => <tr key={i}><td>{u.interface}</td><td>{u.queue}</td><td className="mono">{fmtVal(u.opcode)}</td><td>{u.commands}</td></tr>)}</tbody></table>
        </details>
      )}
      <div className="small muted" style={{ marginTop: 8 }}>Run coverage to turn this into covered / not covered per catalog item. <Link to="/coverage">Go to Coverage →</Link></div>
    </div>
  );
}

export default function ExecScenarios({ setId }: { setId: number }) {
  const qc = useQueryClient();
  const { track } = useJobs();
  const { sources, imported, source, setId: setSource } = useSelectedSource();
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [linkFilter, setLinkFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => { setOffset(0); setChecked(new Set()); }, [setId, source?.id, dq, linkFilter]);

  const analysis = useQuery({ queryKey: ["exec-analysis", setId, source?.id], enabled: !!source, queryFn: () => api.execAnalyses(setId, source!.id) });
  const list = useQuery({
    queryKey: ["scenarios", setId, "exec", source?.id, dq, linkFilter, offset],
    enabled: !!source,
    queryFn: () => api.scenarios(setId, { origin: "exec", source_id: source!.id, q: dq, links: linkFilter, offset, limit: LIMIT }),
  });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["exec-analysis"] }); qc.invalidateQueries({ queryKey: ["scenarios"] }); };
  const analyze = useMutation({ mutationFn: () => api.execAnalyze(setId, source!.id), onSuccess: (r) => track(r.job, "Analyse command DB", refresh) });
  const describe = useMutation({
    mutationFn: (ids?: number[]) => api.execDescribe(setId, source!.id, ids, !!ids),
    onSuccess: (r) => track(r.job, "Describe executed scenarios", refresh),
  });
  const bulk = useMutation({
    mutationFn: (state: "approved" | "rejected") => api.reviewLinksBulk([...checked], state),
    onSuccess: () => { setChecked(new Set()); qc.invalidateQueries({ queryKey: ["scenarios"] }); qc.invalidateQueries({ queryKey: ["scenario-links"] }); qc.invalidateQueries({ queryKey: ["sets"] }); },
  });
  const a = analysis.data?.[0];
  const rows = list.data?.scenarios ?? [];
  const current = rows.find((s) => s.id === open);
  const toggle = (id: number) => setChecked((c) => { const n = new Set(c); n.has(id) ? n.delete(id) : n.add(id); return n; });

  if (sources.data && !imported.length) {
    return <Empty icon={Database} title="No command database imported" action={<Link to="/cmddb"><button className="primary">Add a command DB</button></Link>}>
      Scenarios are read from the commands your test scripts submitted: import a command database first.</Empty>;
  }
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <label>Command DB{" "}
          <select value={source?.id ?? ""} onChange={(e) => setSource(Number(e.target.value))} disabled={!imported.length}>
            {imported.map((s) => <option key={s.id} value={s.id}>{s.name || s.path} ({s.decoded_count} commands)</option>)}
          </select>
        </label>
        <button className="primary" disabled={!source || analyze.isPending} onClick={() => analyze.mutate()}><PlayCircle size={14} /> {a ? "Analyse again" : "Analyse executed tests"}</button>
        <button disabled={!source || !a || describe.isPending} onClick={() => describe.mutate(undefined)} title="Readable titles and proposed requirement links for every flow not described yet"><Bot size={14} /> Describe with AI</button>
        <span className="grow" />
        <select value={linkFilter} onChange={(e) => setLinkFilter(e.target.value)} aria-label="Requirement links">
          <option value="">All flows</option><option value="proposed">Requirement links to review</option><option value="approved">With approved requirements</option>
        </select>
        <input placeholder="Search title or test…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorNote error={sources.error ?? analysis.error ?? list.error ?? analyze.error ?? describe.error} />
      <Loading q={analysis} />
      {analysis.data && !a && (
        <Empty icon={PlayCircle} title="Not analysed yet" action={<button className="primary" disabled={analyze.isPending} onClick={() => analyze.mutate()}>Analyse executed tests</button>}>
          Reads every command of {source?.name || source?.path}, names it from the catalog and builds one scenario per test case: the commands it sent, in order, with their field values.
        </Empty>
      )}
      {a && <AnalysisCard a={a} />}
      {checked.size > 0 && (
        <div className="card row">
          <strong>{checked.size} selected</strong>
          <button className="primary" disabled={bulk.isPending} onClick={() => bulk.mutate("approved")}>Approve proposed requirements</button>
          <button className="danger" disabled={bulk.isPending} onClick={() => bulk.mutate("rejected")}>Reject proposed requirements</button>
          <button disabled={describe.isPending} onClick={() => describe.mutate([...checked])}><Bot size={14} /> Describe with AI</button>
          <button onClick={() => setChecked(new Set())}>Clear</button>
          {bulk.data && <span className="muted small">{bulk.data.updated} link(s) updated</span>}
        </div>
      )}
      <ErrorNote error={bulk.error} />
      {a && <Loading q={list} />}
      {a && list.data && rows.length === 0 && <Empty title="No flows match">Change the search or the filter.</Empty>}
      {rows.length > 0 && (
        <div className="card">
          <div className="scroll">
            <table className="t">
              <thead><tr>
                <th><input type="checkbox" aria-label="Select all" checked={rows.every((s) => checked.has(s.id))}
                  onChange={(e) => setChecked(e.target.checked ? new Set(rows.map((s) => s.id)) : new Set())} /></th>
                <th>Scenario</th><th>Tests</th><th>Commands</th><th>Steps</th><th>Items</th><th>Requirements</th></tr></thead>
              <tbody>{rows.map((s) => {
                const p = s.predicate as ExecPredicate;
                const lc = s.link_counts;
                return (
                  <tr key={s.id} className={`click ${open === s.id ? "sel" : ""}`} onClick={() => setOpen(s.id)}>
                    <td onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label={`Select ${s.title}`} checked={checked.has(s.id)} onChange={() => toggle(s.id)} /></td>
                    <td>{s.title}{s.provenance.ai && <span className="muted small"> · AI</span>}<div className="small muted">{p.tests.slice(0, 3).join(", ")}{p.test_count > 3 ? ` +${p.test_count - 3}` : ""}</div></td>
                    <td>{p.test_count}</td><td>{p.commands}</td><td>{p.steps.length}{p.truncated ? "+" : ""}</td><td>{lc.observed ?? 0}</td>
                    <td>{lc.approved ? <Badge value="approved" label={`${lc.approved} approved`} /> : null} {lc.proposed ? <Badge value="proposed" label={`${lc.proposed} to review`} /> : null}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
          <Pager total={list.data?.total ?? 0} offset={offset} limit={LIMIT} onChange={setOffset} />
        </div>
      )}
      {current && <Drawer s={current} setId={setId} onClose={() => setOpen(null)} />}
    </>
  );
}
