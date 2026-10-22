import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { FlaskConical } from "lucide-react";
import { Badge, Empty, ErrorNote, Loading, Pager, PageHeader } from "../components/ui";
import { SetPicker, useSelectedSet } from "../components/SetPicker";
import CrossGenerator from "../components/CrossGenerator";
import ExecScenarios from "../components/ExecScenarios";
import { useDebounced, useStoredState } from "../hooks";
import type { Scenario } from "../types";

const LIMIT = 100;

function Drawer({ s, setId, onClose }: { s: Scenario; setId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(s.title);
  const [desc, setDesc] = useState(s.description ?? "");
  const [pred, setPred] = useState(JSON.stringify(s.predicate, null, 2));
  useEffect(() => { setTitle(s.title); setDesc(s.description ?? ""); setPred(JSON.stringify(s.predicate, null, 2)); }, [s.id, s.title, s.description, s.predicate]);

  let parsed: unknown = undefined, parseErr: string | null = null;
  try { parsed = JSON.parse(pred); } catch (e) { parseErr = (e as Error).message; }
  const debounced = useDebounced(pred, 400);
  const live = useQuery({
    queryKey: ["validate", setId, debounced],
    enabled: !parseErr,
    queryFn: () => api.validate(setId, JSON.parse(debounced)),
    staleTime: 60_000,
  });
  const errors = parseErr ? [`Invalid JSON: ${parseErr}`] : debounced !== pred ? [] : live.data?.errors ?? [];

  const save = useMutation({
    mutationFn: (b: Record<string, unknown>) => api.patchScenario(s.id, b),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scenarios"] }),
  });
  const dirty = title !== s.title || desc !== (s.description ?? "") || JSON.stringify(parsed) !== JSON.stringify(s.predicate);
  const body = () => ({ title, description: desc, ...(JSON.stringify(parsed) !== JSON.stringify(s.predicate) ? { predicate: parsed } : {}) });
  const prov = s.provenance ?? {};
  const quotes: string[] = Array.isArray((prov as any).quotes) ? (prov as any).quotes : (prov as any).quote ? [(prov as any).quote] : [];

  return (
    <aside className="drawer" aria-label="Scenario detail">
      <div className="row"><h2 className="grow">{s.sid}</h2><button onClick={onClose}>Close</button></div>
      <div className="row"><Badge value={s.state} label={s.state} /><span className="badge">{s.origin}</span><span className="badge">{s.observability}</span></div>
            <label>Title<input style={{ width: "100%" }} value={title} onChange={(e) => setTitle(e.target.value)} /></label>
      <label>Description<textarea rows={3} value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
      <h3>Predicate</h3>
      <textarea rows={16} spellCheck={false} value={pred} onChange={(e) => setPred(e.target.value)} aria-label="Predicate JSON" />
      {errors.length > 0 ? <div className="toast bad" role="alert">{errors.map((e, i) => <div key={i}>{e}</div>)}</div>
        : !parseErr && live.data && debounced === pred && <div className="toast ok">Predicate is valid.</div>}
      <ErrorNote error={save.error} />
      {(
        <div className="row" style={{ margin: "10px 0" }}>
          <button disabled={!dirty || !!parseErr || save.isPending} onClick={() => save.mutate(body())}>Save{s.state === "approved" && JSON.stringify(parsed) !== JSON.stringify(s.predicate) ? " (resets to draft)" : ""}</button>
          <button className="primary" disabled={s.state === "approved" && !dirty || !!parseErr || save.isPending} onClick={() => save.mutate({ ...body(), state: "approved" })}>Approve</button>
          <button className="danger" disabled={s.state === "rejected" || save.isPending} onClick={() => save.mutate({ state: "rejected" })}>Reject</button>
        </div>
      )}
      <h3>Linked catalog items ({s.items.length})</h3>
      <div className="small mono">{s.items.slice(0, 30).map((k) => <div key={k}>{k}</div>)}{s.items.length > 30 && <div className="muted">+{s.items.length - 30} more</div>}</div>
      {(prov as any).ungrounded && <div className="req">No supporting sentence was found for this draft: check it against the spec before approving.</div>}
      {quotes.length > 0 && <><h3>Source quotes</h3>{quotes.map((q, i) => <div key={i} className="req">{q}</div>)}</>}
      {Object.keys(prov).length > 0 && <><h3>Provenance</h3><pre>{JSON.stringify(prov, null, 1)}</pre></>}
    </aside>
  );
}

/** Spec-driven scenarios: drafted by AI from selected sections, for areas the executed tests do not reach yet. */
function GapSuggestions({ setId }: { setId: number }) {
  const qc = useQueryClient();
  const origin = "cross";
  const [state, setState] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => { setOffset(0); setChecked(new Set()); }, [setId, state, dq]);

  const list = useQuery({
    queryKey: ["scenarios", setId, origin, state, dq, offset],
    queryFn: () => api.scenarios(setId, { origin, state, q: dq, offset, limit: LIMIT }),
  });
  const total = list.data?.total;
  useEffect(() => {   // a review action can shrink a filtered list below the current page; return to the last valid page
    if (total !== undefined && offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor((total - 1) / LIMIT) * LIMIT));
  }, [total, offset]);
  const bulk = useMutation({
    mutationFn: (st: string) => api.bulkScenarios([...checked], st),
    onSuccess: () => { setChecked(new Set()); qc.invalidateQueries({ queryKey: ["scenarios"] }); },
  });
  const [showGen, setShowGen] = useState(false);
  const rows = list.data?.scenarios ?? [];
  const current = rows.find((s) => s.id === open);
  const toggle = (id: number) => setChecked((c) => { const n = new Set(c); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <>
      <p className="muted small">For areas the executed tests do not reach yet: a local model drafts cross-command scenarios from selected spec sections. Drafts do not count; approved ones are matched against the command DB when coverage runs.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <select value={state} onChange={(e) => setState(e.target.value)} aria-label="State">
          <option value="">All states</option><option value="draft">Draft</option><option value="approved">Approved</option><option value="rejected">Rejected</option>
        </select>
        <input placeholder="Search title or id…" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="grow" />
        <button className={showGen ? "" : "primary"} onClick={() => setShowGen((v) => !v)}>{showGen ? "Hide generator" : "Suggest scenarios from the spec"}</button>
      </div>
      <ErrorNote error={list.error} />
      {showGen && <CrossGenerator setId={setId} />}
      {checked.size > 0 && (
        <div className="card row">
          <strong>{checked.size} selected</strong>
          <button className="primary" disabled={bulk.isPending} onClick={() => bulk.mutate("approved")}>Approve</button>
          <button disabled={bulk.isPending} onClick={() => bulk.mutate("draft")}>Back to draft</button>
          <button className="danger" disabled={bulk.isPending} onClick={() => bulk.mutate("rejected")}>Reject</button>
          <button onClick={() => setChecked(new Set())}>Clear</button>
        </div>
      )}
      <ErrorNote error={bulk.error} />
      {bulk.data && bulk.data.refused.length > 0 && (
        <div className="toast bad" role="alert">
          {bulk.data.updated.length} updated, {bulk.data.refused.length} refused:
          {bulk.data.refused.slice(0, 8).map((r) => <div key={r.id}>#{r.id}: {r.errors.join("; ")}</div>)}
        </div>
      )}
      <Loading q={list} />
      {list.data && rows.length === 0 && <Empty icon={FlaskConical} title="No suggestions match">Use “Suggest scenarios from the spec” to draft some for sections your tests do not cover, or change the filters.</Empty>}
      {rows.length > 0 && (
        <div className="card">
          <div className="scroll">
            <table className="t">
              <thead><tr>
                <th><input type="checkbox" aria-label="Select all" checked={rows.length > 0 && rows.every((s) => checked.has(s.id))}
                  onChange={(e) => setChecked(e.target.checked ? new Set(rows.map((s) => s.id)) : new Set())} /></th>
                <th>ID</th><th>Title</th><th>Origin</th><th>State</th><th>Observability</th><th>Items</th></tr></thead>
              <tbody>{rows.map((s) => (
                <tr key={s.id} className={`click ${open === s.id ? "sel" : ""}`} onClick={() => setOpen(s.id)}>
                  <td onClick={(e) => e.stopPropagation()}><input type="checkbox" aria-label={`Select ${s.sid}`} checked={checked.has(s.id)} onChange={() => toggle(s.id)} /></td>
                  <td className="mono small">{s.sid}</td><td>{s.title}</td><td>{s.origin}</td><td><Badge value={s.state} label={s.state} /></td>
                  <td className="small">{s.observability}</td><td>{s.items.length}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <Pager total={list.data?.total ?? 0} offset={offset} limit={LIMIT} onChange={setOffset} />
        </div>
      )}
      {current && <Drawer s={current} setId={setId} onClose={() => setOpen(null)} />}
    </>
  );
}

export default function ScenariosPage() {
  const sel = useSelectedSet();
  const [tab, setTab] = useStoredState<"executed" | "gaps">("specforge.scenarios.tab", "executed");
  return (
    <>
      <PageHeader title="Scenarios" description="What your test scripts actually executed, read from a command DB: one scenario per test case, named from the catalog. Spec-driven suggestions cover what the tests do not reach." />
      <div className="row" style={{ marginBottom: 12 }}><SetPicker sel={sel} /></div>
      <ErrorNote error={sel.error} />
      <div className="tabs">
        <button className={tab === "executed" ? "active" : ""} onClick={() => setTab("executed")}>Executed (from command DB)</button>
        <button className={tab === "gaps" ? "active" : ""} onClick={() => setTab("gaps")}>Gap suggestions (from spec)</button>
      </div>
      {sel.setId !== null && (tab === "gaps" ? <GapSuggestions setId={sel.setId} /> : <ExecScenarios setId={sel.setId} />)}
    </>
  );
}
