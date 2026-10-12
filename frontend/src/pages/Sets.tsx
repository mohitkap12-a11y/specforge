import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading } from "../components/ui";
import { useJobs } from "../components/jobs";
import { useSelectedSet } from "../components/SetPicker";
import type { RefResult, SpecDoc, SpecSet } from "../types";

function Members({ set, docs }: { set: SpecSet; docs: SpecDoc[] }) {
  const qc = useQueryClient();
  const current = set.members.map((m: any) => m.document_id as number);
  const [picked, setPicked] = useState<number[]>(current);
  const save = useMutation({ mutationFn: () => api.setMembers(set.id, picked), onSuccess: () => qc.invalidateQueries() });
  const dirty = [...picked].sort().join() !== [...current].sort().join();
  return (
    <div>
      <div className="row">
        {docs.map((d) => (
          <label key={d.id}><input type="checkbox" checked={picked.includes(d.id)}
            onChange={(e) => setPicked(e.target.checked ? [...picked, d.id] : picked.filter((x) => x !== d.id))} /> {d.family} {d.version}</label>
        ))}
        <button disabled={!dirty || !picked.length || save.isPending} onClick={() => save.mutate()}>Save members</button>
      </div>
      {set.members.some((m: any) => m.run_id === null) && (
        <div className="toast bad">A member has no active parse run. Parse it (and make a run active) on the Specs page before building the catalog.</div>
      )}
      <ErrorNote error={save.error} />
    </div>
  );
}

function Ambiguous({ setId, r }: { setId: number; r: RefResult }) {
  const qc = useQueryClient();
  const choose = useMutation({ mutationFn: (k: string) => api.chooseRef(setId, r.src_key, r.raw, k), onSuccess: () => qc.invalidateQueries({ queryKey: ["refs"] }) });
  return (
    <tr>
      <td className="small">{r.from_family} {r.from_version}<div className="mono muted">{r.src_key.split("/").slice(1).join("/")}</div></td>
      <td>{r.raw}</td>
      <td>
        <select defaultValue="" onChange={(e) => e.target.value && choose.mutate(e.target.value)} aria-label={`Resolve ${r.raw}`}>
          <option value="">Choose target…</option>
          {r.candidates.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
        <ErrorNote error={choose.error} />
      </td>
    </tr>
  );
}

function References({ setId }: { setId: number }) {
  const [state, setState] = useState("");
  const q = useQuery({ queryKey: ["refs", setId, state], queryFn: () => api.references(setId, state || undefined) });
  if (q.isLoading || q.error || !q.data) return <Loading q={q} />;
  const sum = q.data.summary;
  const list = q.data.references;
  return (
    <>
      <div className="row" style={{ margin: "8px 0" }}>
        <strong>{sum.total} references</strong><span className="muted">{sum.cross_spec} cross-spec</span>
        {(["resolved", "ambiguous", "external-not-loaded", "broken"] as const).map((s) => (
          <button key={s} className={state === s ? "primary" : ""} onClick={() => setState(state === s ? "" : s)}>{s} {sum[s] ?? 0}</button>
        ))}
      </div>
      {list.length < (state ? sum[state] ?? 0 : sum.total) && (
        <div className="toast bad">Showing the first {list.length} of {state ? sum[state] : sum.total} references (the API returns at most 3,000). Pick a state filter to narrow the list; resolved references need no action.</div>
      )}
      {sum.ambiguous > 0 && !state && <div className="toast bad">{sum.ambiguous} ambiguous reference(s): the same label exists in more than one member. Pick the intended target.</div>}
      <div className="scroll" style={{ maxHeight: 360 }}>
        <table className="t">
          <thead><tr><th>From</th><th>Reference</th><th>Target</th></tr></thead>
          <tbody>
            {list.map((r, i) => r.state === "ambiguous"
              ? <Ambiguous key={`${r.src_key}|${r.raw}|${i}`} setId={setId} r={r} />
              : (
                <tr key={`${r.src_key}|${r.raw}|${i}`}>
                  <td className="small">{r.from_family} {r.from_version}</td><td>{r.raw}</td>
                  <td className="small"><Badge value={r.state === "resolved" ? "ok" : r.state === "broken" ? "error" : "warning"} label={r.state} /> {r.target_key ?? ""}{r.chosen ? " (chosen)" : ""}</td>
                </tr>
              ))}
          </tbody>
        </table>
      </div>
      {list.length === 0 && <div className="muted">No references in this state.</div>}
    </>
  );
}

export default function SetsPage() {
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const sel = useSelectedSet();
  const { track } = useJobs();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const create = useMutation({
    mutationFn: () => api.createSet(name.trim(), picked),
    onSuccess: (r) => { setName(""); setPicked([]); sel.setSetId(r.id); setOpen(r.id); qc.invalidateQueries({ queryKey: ["sets"] }); },
  });
  const build = useMutation({ mutationFn: (id: number) => api.buildCatalog(id), onSuccess: (r, id) => track(r.job, `Catalog for set #${id}`) });
  const del = useMutation({ mutationFn: (id: number) => api.deleteSet(id), onSuccess: () => qc.invalidateQueries() });
  const docs = specs.data ?? [];
  return (
    <>
      <h1>Spec sets</h1>
      <p className="muted">A set groups spec versions that reference each other (for example Base 2.4 and NVM Command Set 1.3). Its catalog is the coverage denominator.</p>
      <ErrorNote error={sel.error} />
      {!sel.loading && sel.sets.length === 0 && <Empty>No sets yet. Create one below.</Empty>}
      {sel.sets.map((s) => (
        <div key={s.id} className="card">
          <div className="row">
            <strong className="grow">{s.name}</strong>
            <span className="muted small">{s.catalog_items} catalog items · {s.scenarios} scenarios</span>
            <button onClick={() => setOpen(open === s.id ? null : s.id)}>{open === s.id ? "Hide details" : "Members & references"}</button>
            <button className="primary" disabled={build.isPending} onClick={() => build.mutate(s.id)}>{s.catalog_items ? "Rebuild catalog" : "Build catalog"}</button>
            <button className="danger" disabled={del.isPending} onClick={() => { if (confirm(`Delete set "${s.name}" with its catalog, scenarios and coverage runs?`)) del.mutate(s.id); }}>Delete</button>
          </div>
          <div className="muted small">{s.members.map((m: any) => `${m.family} ${m.version}`).join(", ") || "no members"}</div>
          {open === s.id && <div style={{ marginTop: 10 }}><Members set={s} docs={docs} /><References setId={s.id} /></div>}
        </div>
      ))}
      <ErrorNote error={build.error ?? del.error} />
      <div className="card">
        <h2>New set</h2>
        <div className="row">
          <input placeholder="Set name" value={name} onChange={(e) => setName(e.target.value)} />
          {docs.map((d) => (
            <label key={d.id}><input type="checkbox" checked={picked.includes(d.id)}
              onChange={(e) => setPicked(e.target.checked ? [...picked, d.id] : picked.filter((x) => x !== d.id))} /> {d.family} {d.version}</label>
          ))}
          <button className="primary" disabled={!name.trim() || !picked.length || create.isPending} onClick={() => create.mutate()}>Create</button>
        </div>
        {docs.length === 0 && <div className="muted small">Upload specs first.</div>}
        <ErrorNote error={create.error} />
      </div>
    </>
  );
}
