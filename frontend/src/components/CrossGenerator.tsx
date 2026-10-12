import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorNote } from "./ui";
import { useJobs } from "./jobs";
import { useDebounced } from "../hooks";

type Picked = Map<string, string>;       // key -> label

function Picker({ title, hint, picked, setPicked, search }: {
  title: string; hint: string; picked: Picked; setPicked: (p: Picked) => void;
  search: (q: string) => Promise<{ key: string; label: string; sub?: string }[]>;
}) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const res = useQuery({ queryKey: ["cross-pick", title, dq], queryFn: () => search(dq), staleTime: 30_000 });
  const toggle = (k: string, label: string) => { const n = new Map(picked); n.has(k) ? n.delete(k) : n.set(k, label); setPicked(n); };
  return (
    <div style={{ flex: "1 1 320px", minWidth: 0 }}>
      <strong>{title}</strong> <span className="muted small">{hint}</span>
      <input placeholder={`Search ${title.toLowerCase()}…`} value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%", margin: "6px 0" }} aria-label={`Search ${title}`} />
      <div style={{ maxHeight: 190, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
        {(res.data ?? []).map((r) => (
          <label key={r.key} className="row small" style={{ padding: "3px 8px", gap: 8, cursor: "pointer" }}>
            <input type="checkbox" checked={picked.has(r.key)} onChange={() => toggle(r.key, r.label)} />
            <span>{r.label}</span><span className="muted">{r.sub}</span>
          </label>
        ))}
        {res.data && res.data.length === 0 && <div className="muted small" style={{ padding: 8 }}>No matches.</div>}
      </div>
      {picked.size > 0 && (
        <div className="row" style={{ flexWrap: "wrap", gap: 4, marginTop: 6 }}>
          {[...picked].map(([k, l]) => <button key={k} className="chip on" onClick={() => toggle(k, l)} title="Remove">{l} ×</button>)}
        </div>
      )}
    </div>
  );
}

export default function CrossGenerator({ setId }: { setId: number }) {
  const { track } = useJobs();
  const [sections, setSections] = useState<Picked>(new Map());
  const [commands, setCommands] = useState<Picked>(new Map());
  const [focus, setFocus] = useState("");
  const [max, setMax] = useState(6);
  const sel = { section_keys: [...sections.keys()], command_keys: [...commands.keys()] };
  const empty = sel.section_keys.length + sel.command_keys.length === 0;

  const plan = useMutation({ mutationFn: () => api.crossPlan(setId, sel) });
  const gen = useMutation({ mutationFn: () => api.crossGenerate(setId, { ...sel, focus, max_per_section: max }), onSuccess: (r) => track(r.job, "Cross-command scenarios") });

  return (
    <div className="card">
      <h2>Generate cross-command scenarios</h2>
      <p className="muted small">
        Pick commands and/or sections. Each selected section is read together with every passage in the spec set that connects to it
        (its references, what refers to it, commands it names, statements elsewhere naming its commands) and the model drafts scenarios that
        need several commands. Drafts stay unapproved until you review them.
      </p>
      <div className="row" style={{ alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
        <Picker title="Commands" hint="uses the command's own section" picked={commands} setPicked={setCommands}
          search={async (q) => (await api.catalog(setId, { kind: "command", q, limit: 60 })).items.map((i) => ({ key: i.key, label: i.label, sub: i.key }))} />
        <Picker title="Sections" hint="any section of any spec in the set" picked={sections} setPicked={setSections}
          search={async (q) => (await api.sections(setId, { q, limit: 80 })).sections.map((s) => ({ key: s.key, label: `§${s.number} ${s.title}`, sub: `${s.family} ${s.version}` }))} />
      </div>
      <div className="row" style={{ margin: "12px 0", gap: 12, flexWrap: "wrap" }}>
        <input className="grow" placeholder="Optional focus, e.g. “resource creation and deletion order”" value={focus} onChange={(e) => setFocus(e.target.value)} aria-label="Focus" />
        <label className="small">Max per section <input type="number" min={1} max={20} value={max} onChange={(e) => setMax(Math.min(20, Math.max(1, Number(e.target.value) || 1)))} style={{ width: 60 }} /></label>
        <button disabled={empty || plan.isPending} onClick={() => plan.mutate()}>Preview scope</button>
        <button className="primary" disabled={empty || gen.isPending} onClick={() => gen.mutate()}>Generate{empty ? "" : ` (${sel.section_keys.length + sel.command_keys.length} selected)`}</button>
      </div>
      <ErrorNote error={plan.error ?? gen.error} />
      {plan.data && (
        <div>
          {plan.data.units.map((u) => (
            <details key={u.section_key} open={plan.data!.units.length === 1} style={{ marginBottom: 6 }}>
              <summary><strong>{u.title}</strong> <span className="muted small">{u.family} {u.version} · {u.primary_chars.toLocaleString()} chars · {u.commands.length} command(s) · {u.related.length} connected passage(s)</span></summary>
              {u.related.length === 0 ? <div className="muted small">Nothing else in the set connects to this section; the model sees only the section itself.</div>
                : <ul className="small">{u.related.map((r) => <li key={r.key}><span className="mono">{r.key}</span> {r.title && `· ${r.title}`} <span className="muted">— {r.why}</span></li>)}</ul>}
            </details>
          ))}
        </div>
      )}
    </div>
  );
}
