import { useState } from "react";
import { useInfiniteQuery, useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorNote } from "./ui";
import { useJobs } from "./jobs";
import { useDebounced } from "../hooks";
import type { SpecInSet } from "../types";

type Picked = Map<string, string>;       // key -> label
type Row = { key: string; label: string; sub?: string };
const PAGE = 60;

function Picker({ title, hint, picked, setPicked, specs, search }: {
  title: string; hint: string; picked: Picked; setPicked: (p: Picked) => void; specs: SpecInSet[];
  search: (q: string, spec: { family?: string; version?: string }, limit: number, offset: number) => Promise<{ rows: Row[]; total: number }>;
}) {
  const [q, setQ] = useState("");
  const [spec, setSpec] = useState("");          // index into `specs`: structural, so no character in a family or version name can break it
  const dq = useDebounced(q);
  // pages are fetched by offset and appended, so a set larger than the endpoints' row cap is still reachable
  const res = useInfiniteQuery({
    queryKey: ["cross-pick", title, dq, spec, specs.length],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => { const sel = spec === "" ? undefined : specs[Number(spec)]; return search(dq, { family: sel?.family, version: sel?.version }, PAGE, pageParam); },
    getNextPageParam: (last, all) => { const seen = all.reduce((n, p) => n + p.rows.length, 0); return seen < last.total && last.rows.length > 0 ? seen : undefined; },
    staleTime: 30_000,
  });
  const rows = res.data?.pages.flatMap((p) => p.rows) ?? [];
  const total = res.data?.pages[0]?.total ?? 0;
  const toggle = (k: string, label: string) => { const n = new Map(picked); n.has(k) ? n.delete(k) : n.set(k, label); setPicked(n); };
  const allShown = rows.length > 0 && rows.every((r) => picked.has(r.key));
  const toggleShown = () => { const n = new Map(picked); rows.forEach((r) => (allShown ? n.delete(r.key) : n.set(r.key, r.label))); setPicked(n); };
  return (
    <div style={{ flex: "1 1 320px", minWidth: 0 }}>
      <strong>{title}</strong> <span className="muted small">{hint}</span>
      <div className="row" style={{ margin: "6px 0", gap: 6 }}>
        <input className="grow" placeholder={`Search ${title.toLowerCase()}…`} value={q} onChange={(e) => setQ(e.target.value)} aria-label={`Search ${title}`} />
        {specs.length > 1 && (
          <select value={spec} onChange={(e) => setSpec(e.target.value)} aria-label={`${title}: spec`}>
            <option value="">All specs</option>
            {specs.map((s, i) => <option key={i} value={String(i)}>{s.family} {s.version}</option>)}
          </select>
        )}
      </div>
      <div style={{ maxHeight: 220, overflowY: "auto", border: "1px solid var(--border)", borderRadius: 6 }}>
        {rows.map((r) => (
          <label key={r.key} className="row small" style={{ padding: "3px 8px", gap: 8, cursor: "pointer" }}>
            <input type="checkbox" checked={picked.has(r.key)} onChange={() => toggle(r.key, r.label)} />
            <span>{r.label}</span><span className="muted">{r.sub}</span>
          </label>
        ))}
        {res.data && rows.length === 0 && <div className="muted small" style={{ padding: 8 }}>No matches.</div>}
      </div>
      <div className="row small" style={{ marginTop: 4, gap: 8 }}>
        <span className="muted grow">Showing {rows.length} of {total}{res.hasNextPage ? " — narrow with search or the spec filter, or load more" : ""}</span>
        {rows.length > 0 && <button onClick={toggleShown}>{allShown ? "Unselect shown" : "Select all shown"}</button>}
        {res.hasNextPage && <button disabled={res.isFetchingNextPage} onClick={() => res.fetchNextPage()}>Load more</button>}
      </div>
      {picked.size > 0 && (
        <div className="row" style={{ flexWrap: "wrap", gap: 4, marginTop: 6 }}>
          <span className="small muted">{picked.size} selected:</span>
          {[...picked].map(([k, l]) => <button key={k} className="chip on" onClick={() => toggle(k, l)} title="Remove">{l} ×</button>)}
          <button className="chip" onClick={() => setPicked(new Map())}>Clear all</button>
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
  const [picked, setPicked] = useState<string[] | null>(null);       // null: the server's default (all categories)
  const [variants, setVariants] = useState(false);
  const angles = useQuery({ queryKey: ["cross-angles"], queryFn: api.crossAngles, staleTime: Infinity });
  const cats = picked ?? angles.data?.default ?? ["functional", "cross", "negative"];
  const toggleCat = (id: string) => setPicked(cats.includes(id) ? cats.filter((c) => c !== id) : [...cats, id]);
  const depth = (angles.data?.angles ?? []).filter((a) => (a.id === "variants" ? variants : cats.includes(a.category))).length;
  const sel = { section_keys: [...sections.keys()], command_keys: [...commands.keys()] };
  const empty = sel.section_keys.length + sel.command_keys.length === 0 || cats.length === 0;

  const specs = useQuery({ queryKey: ["cross-specs", setId], queryFn: () => api.sections(setId, { limit: 1 }), staleTime: 60_000 }).data?.specs ?? [];
  const plan = useMutation({ mutationFn: () => api.crossPlan(setId, sel) });
  const gen = useMutation({ mutationFn: () => api.crossGenerate(setId, { ...sel, focus, max_per_section: max, categories: cats, variants }), onSuccess: (r) => track(r.job, "Scenarios") });

  return (
    <div className="card">
      <h2>Generate scenarios</h2>
      <p className="muted small">
        Pick commands and/or sections. Each selected section is read together with every passage in the spec set that connects to it
        (its references, what refers to it, commands it names, statements elsewhere naming its commands) and the model drafts scenarios in
        each category you choose: functional (the command used as the text intends, field by field), cross-command (flows that need several
        commands) and negative (invalid or reserved values, skipped preconditions, exceeded limits). Drafts stay unapproved until you review them.
      </p>
      <div className="row" style={{ alignItems: "flex-start", gap: 16, flexWrap: "wrap" }}>
        <Picker title="Commands" hint="select several (up to 24): they are tested together; each uses its own section" picked={commands} setPicked={setCommands} specs={specs}
          search={async (q, { family, version }, limit, offset) => { const r = await api.catalog(setId, { kind: "command", q, family, version, limit, offset }); return { total: r.total, rows: r.items.map((i) => ({ key: i.key, label: i.label, sub: `${i.attrs.family} ${i.attrs.version}` })) }; }} />
        <Picker title="Sections" hint="any section of any spec in the set" picked={sections} setPicked={setSections} specs={specs}
          search={async (q, { family, version }, limit, offset) => { const r = await api.sections(setId, { q, family, version, limit, offset }); return { total: r.total, rows: r.sections.map((s) => ({ key: s.key, label: `§${s.number} ${s.title}`, sub: `${s.family} ${s.version}` })) }; }} />
      </div>
      <div className="row" style={{ margin: "12px 0", gap: 12, flexWrap: "wrap" }}>
        <input className="grow" placeholder="Optional focus, e.g. “resource creation and deletion order”" value={focus} onChange={(e) => setFocus(e.target.value)} aria-label="Focus" />
        <span className="row small" role="group" aria-label="Scenario categories" style={{ gap: 10 }}>
          {(angles.data?.categories ?? []).map((c) => (
            <label key={c.id} title={c.title}><input type="checkbox" checked={cats.includes(c.id)} onChange={() => toggleCat(c.id)} /> {c.title.split(":")[0]}</label>
          ))}
          <label title="An extra pass that repeats flows across each value of the fields the text defines"><input type="checkbox" checked={variants} onChange={(e) => setVariants(e.target.checked)} /> Value variants</label>
        </span>
        <label className="small">Max per pass <input type="number" min={1} max={20} value={max} onChange={(e) => setMax(Math.min(20, Math.max(1, Number(e.target.value) || 1)))} style={{ width: 60 }} /></label>
        <button disabled={empty || plan.isPending} onClick={() => plan.mutate()}>Preview scope</button>
        <button className="primary" disabled={empty || gen.isPending} onClick={() => gen.mutate()}>Generate{empty ? "" : ` (${sel.section_keys.length + sel.command_keys.length} selected)`}</button>
      </div>
      <ErrorNote error={plan.error ?? gen.error} />
      {plan.data && (
        <div>
          <p className="small muted">{plan.data.units.length} section(s) × {depth} pass(es) = {plan.data.units.length * depth} model call(s). Each pass looks for a different kind of scenario within the chosen categories.</p>
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
