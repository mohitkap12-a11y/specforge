import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Download, GitCompareArrows, Trash2 } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorNote, Loading, PageHeader, Pager, Switch } from "../components/ui";
import { useJobs } from "../components/jobs";
import { compareVersions, splitMarks } from "../util";
import type { AIPoint, CompareChange, CompareEntity, CompareOption, Comparison, EntityGroup, Impact } from "../types";

const IMPACTS: { key: Impact; label: string; hint: string }[] = [
  { key: "breaking", label: "Breaking", hint: "May break implementations or tests: a shall weakened or removed, a field removed or made reserved" },
  { key: "new", label: "New", hint: "New sections, tables, rows and requirements" },
  { key: "changed", label: "Changed", hint: "Behaviour or values that differ" },
  { key: "clarification", label: "Clarification", hint: "Wording changes without a normative effect" },
  { key: "editorial", label: "Editorial", hint: "Renumbering and renaming only" },
];
const PAGE = 25;

const ImpactBadge = ({ impact }: { impact: Impact }) => <span className={`badge ${impact}`}>{impact}</span>;
const label = (o: CompareOption) => `${o.version}${o.filename ? ` · ${o.filename}` : ""}${o.parsed ? "" : "  (not parsed yet)"}`;

function Marked({ text, chunks }: { text: string; chunks: string[] }) {
  return <>{splitMarks(text, chunks).map((p, i) => (p.m ? <mark key={i}>{p.t}</mark> : <span key={i}>{p.t}</span>))}</>;
}

function Strength({ s }: { s: string | null | undefined }) {
  return s ? <span className="badge" title="Requirement level of the statement">{s}</span> : null;
}

function Details({ c }: { c: CompareChange }) {
  const d = c.details;
  if (c.change === "moved" || c.change === "renamed") {
    return (
      <div className="body"><div className="muted">
        {c.change === "renamed" ? `Renamed from “${c.title_from ?? ""}” to “${c.title}”` : `${c.kind === "table" ? "Figure" : "Section"} number changed from ${c.number_from} to ${c.number_to}`}
        {d.how ? ` (matched by ${d.how.replace("+", " and ")}).` : "."} The content itself is unchanged.</div></div>
    );
  }
  if (c.kind === "section" && c.change === "modified") {
    return (
      <div className="body">
        {d.modified?.map((m, i) => (
          <div key={`m${i}`} className="diff-line mod">
            <div><span className="lbl2">before</span><Marked text={m.before} chunks={m.delta.removed} /></div>
            <div><span className="lbl2">after</span><Marked text={m.after} chunks={m.delta.added} /> <Strength s={m.strength[1]} /></div>
          </div>
        ))}
        {d.added?.map((s, i) => <div key={`a${i}`} className="diff-line add"><span className="lbl2">added</span>{s.text} <Strength s={s.strength} /></div>)}
        {d.removed?.map((s, i) => <div key={`r${i}`} className="diff-line del"><span className="lbl2">removed</span>{s.text} <Strength s={s.strength} /></div>)}
        {c.title_from && <div className="muted small">Renamed from “{c.title_from}”.</div>}
        {d.truncated && <div className="muted small">Showing the first {Math.max(d.added?.length ?? 0, d.removed?.length ?? 0, d.modified?.length ?? 0)} of {d.counts?.added ?? 0} added, {d.counts?.removed ?? 0} removed and {d.counts?.modified ?? 0} modified sentences; the Markdown export may also omit some.</div>}
      </div>
    );
  }
  if (c.kind === "section") {
    return <div className="body">{d.excerpt?.map((s, i) => <div key={i} className={`diff-line ${c.change === "added" ? "add" : "del"}`}>{s}</div>)}
      {!d.excerpt?.length && <div className="muted">No body text.</div>}</div>;
  }
  if (c.change === "modified") {
    return (
      <div className="body">
        {d.header_changed && <div className="diff-line mod"><span className="lbl2">columns</span>{d.header_before?.join(" | ")} → {d.header_after?.join(" | ")}</div>}
        {d.rows_changed?.map((r, i) => (
          <div key={`c${i}`} className="diff-line mod">
            <strong>{r.key}</strong>{r.rekeyed && <span className="muted small"> (was {r.key_before})</span>}
            {r.cells.map((cell, j) => <div key={j}><span className="lbl2">{cell.column}</span><s>{cell.before || "∅"}</s> → {cell.after || "∅"}</div>)}
          </div>
        ))}
        {d.rows_added?.map((r, i) => <div key={`a${i}`} className="diff-line add"><span className="lbl2">row added</span>{r.join(" | ")}</div>)}
        {d.rows_removed?.map((r, i) => <div key={`r${i}`} className="diff-line del"><span className="lbl2">row removed</span>{r.join(" | ")}</div>)}
        {d.truncated && <div className="muted small">Showing the first rows of {d.rows_changed_count ?? 0} changed, {d.rows_added_count ?? 0} added and {d.rows_removed_count ?? 0} removed; the Markdown export may also omit some.</div>}
      </div>
    );
  }
  return (
    <div className="body">
      <div className="muted small">{d.header?.join(" | ")} · {d.row_count ?? 0} row(s)</div>
      {d.rows?.map((r, i) => <div key={i} className={`diff-line ${c.change === "added" ? "add" : "del"}`}>{r.join(" | ")}</div>)}
    </div>
  );
}

function ChangeRow({ c }: { c: CompareChange }) {
  const num = c.number_from && c.number_to && c.number_from !== c.number_to ? `${c.number_from} → ${c.number_to}` : (c.number_to ?? c.number_from ?? "");
  return (
    <details className="change">
      <summary>
        <ImpactBadge impact={c.impact} />
        <span className="badge">{c.kind} {c.change}</span>
        <span className="num">{c.kind === "table" ? `Fig ${num}` : `§${num}`}</span>
        <span className="ttl">{c.title}</span>
        {c.nested && <span className="muted small">part of a {c.change} {c.kind === "table" ? "section" : "parent"}</span>}
        {!!c.normative && <span className="muted small">{c.normative} normative</span>}
      </summary>
      <Details c={c} />
    </details>
  );
}

function Points({ items }: { items: AIPoint[] }) {
  return <ul>{items.map((p, i) => <li key={i}><ImpactBadge impact={p.impact} /> <strong>{p.title}</strong>. {p.detail}</li>)}</ul>;
}

function Summary({ comp }: { comp: Comparison }) {
  const qc = useQueryClient();
  const { track, active } = useJobs();
  const [waiting, setWaiting] = useState(false);     // the submitted summary job has not finished yet
  const run = useMutation({
    mutationFn: (redo: boolean) => api.compareSummarize(comp.id, redo),
    onSuccess: (r) => {
      setWaiting(true);
      track(r.job, `AI summary of comparison #${comp.id}`, () => { setWaiting(false); qc.invalidateQueries({ queryKey: ["comparison", comp.id] }); });
    },
  });
  const writing = waiting || run.isPending || active.some((j) => j.type === "compare-summary");   // also covers a job started before a page reload
  const ai = comp.ai;
  return (
    <div className="card">
      <div className="card-head">
        <h2 style={{ margin: 0 }}>Summary</h2><span className="grow" />
        <a href={`/api/compare/${comp.id}/export`} download><button><Download size={14} /> Export Markdown</button></a>
        <button className={ai ? "" : "primary"} disabled={writing} onClick={() => run.mutate(!!ai)}><Bot size={14} /> {writing ? "Writing summary…" : ai ? "Regenerate AI summary" : "Generate AI summary"}</button>
      </div>
      <p style={{ margin: "0 0 8px" }}>{comp.overview}</p>
      <ErrorNote error={run.error} />
      {writing && !ai && <div className="muted small">The AI summary is being written; follow it in <Link to="/activity?lens=ai">Activity</Link>. It appears here when ready.</div>}
      {ai && (
        <div className="ai-box">
          <div className="muted small" style={{ marginBottom: 6 }}>AI summary · {ai.meta.provider}/{ai.meta.model}. It describes the detected changes below; the list is the authoritative record.</div>
          <p style={{ margin: 0 }}>{ai.overview}</p>
          {ai.breaking.length > 0 && <><h3>May break existing implementations or tests</h3><ul>{ai.breaking.map((b, i) => <li key={i}>{b}</li>)}</ul></>}
          {ai.highlights.length > 0 && <><h3>Highlights</h3><Points items={ai.highlights} /></>}
          {ai.upgrade_notes.length > 0 && <><h3>What to review next</h3><ul>{ai.upgrade_notes.map((u, i) => <li key={i}>{u}</li>)}</ul></>}
          {ai.chapters.length > 0 && <details style={{ marginTop: 10 }}><summary className="small">By chapter</summary>
            {ai.chapters.map((c) => <div key={c.chapter}><h3>{c.chapter} {c.title}</h3><p style={{ margin: "0 0 4px" }}>{c.headline}</p><Points items={c.points} /></div>)}
          </details>}
        </div>
      )}
    </div>
  );
}

const ENTITY_TABLES: { group: EntityGroup; title: string; empty: string }[] = [
  { group: "commands_added", title: "New commands", empty: "No command was added." },
  { group: "commands_modified", title: "Commands modified", empty: "No existing command changed." },
  { group: "structures_added", title: "New data structures", empty: "No data structure was added." },
  { group: "structures_modified", title: "Data structures modified", empty: "No existing data structure changed." },
  { group: "commands_removed", title: "Commands removed", empty: "" },
  { group: "structures_removed", title: "Data structures removed", empty: "" },
];

function EntityTables({ comp, selected, onSelect }: { comp: Comparison; selected: string; onSelect: (key: string, e: CompareEntity | null) => void }) {
  const ent = comp.summary.entities;
  if (!ent) {
    return <div className="card"><div className="card-head"><h2 style={{ margin: 0 }}>Commands and data structures</h2></div>
      <div className="muted small">Not available: one of the compared parse runs no longer exists. Compare the versions again to get these tables.</div></div>;
  }
  const ai = comp.ai?.entities ?? {};
  return (
    <div className="card">
      <div className="card-head">
        <h2 style={{ margin: 0 }}>Commands and data structures</h2>
      </div>
      <p className="muted small" style={{ margin: "0 0 10px" }}>
        {comp.ai?.entities ? "Descriptions written by AI from the changes they list. " : "Generate the AI summary to get a written description of each row. "}
        Select a row to list its changes below.</p>
      {ENTITY_TABLES.map(({ group, title, empty }) => {
        const g = ent[group] ?? { total: 0, items: [] };
        if (!empty && !g.total) return null;
        const cmd = group.startsWith("commands");
        return (
          <div key={group} className="entity-table">
            <h3>{title} <span className="muted">({g.total})</span></h3>
            {g.items.length === 0 ? <div className="muted small">{empty}</div> : (
              <div className="table-scroll">
                <table className="t">
                  <thead><tr><th>{cmd ? "Command" : "Structure"}</th>{cmd && <th>Opcode</th>}<th>Impact</th><th>{group.endsWith("_added") ? "What it is" : "What changed"}</th><th>Where</th></tr></thead>
                  <tbody>{g.items.map((e) => {
                    const sel = `${group}:${e.key}`;
                    const text = ai[group]?.[e.key];
                    return (
                      <tr key={e.key} className={`click ${selected === sel ? "sel" : ""}`} aria-selected={selected === sel}
                        onClick={() => onSelect(selected === sel ? "" : sel, selected === sel ? null : e)}>
                        <td><strong>{e.name}</strong></td>{cmd && <td className="nowrap">{e.opcode || "—"}</td>}
                        <td><ImpactBadge impact={e.impact} /></td>
                        <td>{text ? <>{text}<div className="muted small">{e.change_count} change(s)</div></> : (
                          <ul className="plain">{e.changes.slice(0, 4).map((c, i) => <li key={i}>{c.text}</li>)}
                            {e.change_count > 4 && <li className="muted small">+{e.change_count - 4} more</li>}</ul>)}</td>
                        <td className="small">{e.where.slice(0, 6).join(", ")}{e.where.length > 6 ? " …" : ""}</td>
                      </tr>
                    );
                  })}</tbody>
                </table>
              </div>
            )}
            {g.total > g.items.length && <div className="muted small">Showing the first {g.items.length} of {g.total}.</div>}
          </div>
        );
      })}
    </div>
  );
}

function Result({ id }: { id: number }) {
  const comp = useQuery({ queryKey: ["comparison", id], queryFn: () => api.comparison(id) });
  const [impact, setImpact] = useState<Impact | "">("");
  const [kind, setKind] = useState("");
  const [chapter, setChapter] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<"document" | "importance">("importance");
  const [flat, setFlat] = useState(false);       // include changes that are part of an added/removed parent
  const [offset, setOffset] = useState(0);
  const [entity, setEntity] = useState<{ key: string; name: string; ids: number[] } | null>(null);   // changes of one table row
  useEffect(() => { setOffset(0); }, [impact, kind, chapter, q, sort, flat, id, entity]);
  useEffect(() => { setEntity(null); }, [id]);
  const params = entity ? { ids: entity.ids.join(","), sort, offset, limit: PAGE } : { impact: impact || undefined, kind: kind || undefined, chapter: chapter || undefined, q: q || undefined, sort, nested: flat ? "true" : "false", offset, limit: PAGE };
  const list = useQuery({ queryKey: ["compare-changes", id, params], queryFn: () => api.compareChanges(id, params), placeholderData: (p) => p });
  if (comp.isLoading) return <Loading q={comp} />;
  if (!comp.data) return <ErrorNote error={comp.error} />;
  const s = comp.data.summary;
  return (
    <>
      <h2 style={{ margin: "20px 0 10px" }}>{s.to.family}: {s.from.version} → {s.to.version}</h2>
      <Summary comp={comp.data} />
      <EntityTables comp={comp.data} selected={entity?.key ?? ""} onSelect={(key, e) => {
        setEntity(e ? { key, name: e.name, ids: e.change_ids ?? e.changes.map((c) => c.id) } : null);
        if (e) setTimeout(() => document.getElementById("cmp-changes")?.scrollIntoView({ behavior: "smooth", block: "start" }), 0);
      }} />
      <div className="impacts">
        {IMPACTS.map((i) => (
          <button key={i.key} className={`impact-card ${i.key} ${impact === i.key ? "on" : ""}`} title={i.hint} aria-pressed={impact === i.key}
            onClick={() => setImpact(impact === i.key ? "" : i.key)}><strong>{s.by_impact[i.key]}</strong><span>{i.label}</span></button>
        ))}
      </div>
      <div className="card">
        <div className="card-head"><h2 style={{ margin: 0 }}>By chapter</h2><span className="muted small">{s.matched.sections} sections and {s.matched.tables} tables matched between the versions</span></div>
        <table className="t">
          <thead><tr><th>Chapter</th><th>Title</th><th>Changes</th><th>Breaking</th><th>New</th><th>Changed</th></tr></thead>
          <tbody>{s.chapters.slice(0, 12).map((c) => (
            <tr key={c.chapter} className="click" onClick={() => setChapter(chapter === c.chapter ? "" : c.chapter)} aria-selected={chapter === c.chapter}>
              <td>{c.chapter}</td><td>{c.title}</td><td>{c.total}</td><td>{c.breaking || ""}</td><td>{c.new || ""}</td><td>{c.changed || ""}</td>
            </tr>))}</tbody>
        </table>
      </div>
      {entity && (
        <div className="cmp-filters" id="cmp-changes">
          <span>Changes behind <strong>{entity.name}</strong></span><span className="grow" />
          <button className="sm" onClick={() => setEntity(null)}>Show all changes</button>
        </div>
      )}
      {!entity && <div className="cmp-filters" id="cmp-changes">
        <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}><option value="">Sections and tables</option><option value="section">Sections</option><option value="table">Tables / figures</option></select>
        <select aria-label="Chapter" value={chapter} onChange={(e) => setChapter(e.target.value)}><option value="">All chapters</option>{s.chapters.map((c) => <option key={c.chapter} value={c.chapter}>{c.chapter} {c.title}</option>)}</select>
        <input aria-label="Search changes" placeholder="Search title, number or text…" value={q} onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Order" value={sort} onChange={(e) => setSort(e.target.value as "document" | "importance")}><option value="importance">Most important first</option><option value="document">Document order</option></select>
        <Switch checked={flat} onChange={setFlat} label="Include parts of new/removed sections" />
        <span className="grow" /><span className="muted small">{list.data?.total ?? 0} change(s)</span>
      </div>}
      <ErrorNote error={list.error} />
      {list.data?.items.map((c) => <ChangeRow key={c.id} c={c} />)}
      {list.data && list.data.total === 0 && <Empty title="No changes match">{s.total === 0 ? "The two versions have the same content." : "Clear a filter to see more."}</Empty>}
      {list.data && list.data.total > PAGE && <Pager total={list.data.total} offset={offset} limit={PAGE} onChange={setOffset} />}
    </>
  );
}

export default function ComparePage() {
  const qc = useQueryClient();
  const [sp, setSp] = useSearchParams();
  const open = Number(sp.get("id")) || null;
  const opts = useQuery({ queryKey: ["compare-options"], queryFn: api.compareOptions });
  const past = useQuery({ queryKey: ["comparisons"], queryFn: api.comparisons });
  const families = useMemo(() => [...new Set((opts.data ?? []).map((o) => o.family))].sort(), [opts.data]);
  const [family, setFamily] = useState("");
  const versions = useMemo(() => (opts.data ?? []).filter((o) => o.family === family).sort((a, b) => compareVersions(a.version, b.version) || a.doc_id - b.doc_id), [opts.data, family]);
  const [from, setFrom] = useState<number | "">("");
  const [to, setTo] = useState<number | "">("");
  useEffect(() => {   // default: the family with the most versions, comparing its two newest parsed ones
    if (!opts.data?.length || family) return;
    const best = families.map((f) => ({ f, n: opts.data!.filter((o) => o.family === f && o.parsed).length })).sort((a, b) => b.n - a.n)[0];
    setFamily(best?.f ?? families[0] ?? "");
  }, [opts.data, families, family]);
  useEffect(() => {
    const ok = versions.filter((v) => v.parsed);
    setTo(ok.length ? ok[ok.length - 1].doc_id : "");
    setFrom(ok.length > 1 ? ok[ok.length - 2].doc_id : "");
  }, [versions]);
  const go = useMutation({
    mutationFn: () => {
      const a = versions.find((v) => v.doc_id === from)!, b = versions.find((v) => v.doc_id === to)!;
      return api.compare(a.run_id!, b.run_id!);
    },
    onSuccess: (c) => { setSp({ id: String(c.id) }); qc.setQueryData(["comparison", c.id], c); qc.invalidateQueries({ queryKey: ["comparisons"] }); },
  });
  const del = useMutation({ mutationFn: (id: number) => api.deleteComparison(id), onSuccess: (_r, id) => { if (open === id) setSp({}); qc.invalidateQueries({ queryKey: ["comparisons"] }); } });
  const parsedCount = versions.filter((v) => v.parsed).length;
  const same = from !== "" && from === to;
  return (
    <>
      <PageHeader title="Compare spec versions" description="Choose two versions of a specification from your library, for example NVMe Base 2.3 and 2.4. SpecForge lists what is new, removed and modified, flags changes that can break implementations, and can write an AI summary of what the new version brings." />
      <Loading q={opts} />
      {opts.data && parsedCount < 2 && (
        <Empty icon={GitCompareArrows} title={families.length ? "Parse at least two versions of the same spec" : "No specs in the library yet"}
          action={<Link to="/specs"><button className="primary">Go to Specs</button></Link>}>
          Upload and parse the older and the newer version on the Specs page; they then appear in the lists here.
        </Empty>
      )}
      {opts.data && opts.data.length > 0 && (
        <div className="card">
          <div className="pick">
            <label>Specification<select value={family} onChange={(e) => setFamily(e.target.value)}>{families.map((f) => <option key={f} value={f}>{f}</option>)}</select></label>
            <label>Old version<select value={from} onChange={(e) => setFrom(e.target.value === "" ? "" : Number(e.target.value))}>
              <option value="">Select…</option>{versions.map((v) => <option key={v.doc_id} value={v.doc_id} disabled={!v.parsed}>{label(v)}</option>)}</select></label>
            <label>New version<select value={to} onChange={(e) => setTo(e.target.value === "" ? "" : Number(e.target.value))}>
              <option value="">Select…</option>{versions.map((v) => <option key={v.doc_id} value={v.doc_id} disabled={!v.parsed}>{label(v)}</option>)}</select></label>
            <button className="primary" disabled={from === "" || to === "" || same || go.isPending} onClick={() => go.mutate()}>
              {go.isPending ? <span className="spinner" /> : <GitCompareArrows size={15} />} Compare</button>
          </div>
          {same && <div className="muted small" style={{ marginTop: 8 }}>Choose two different versions.</div>}
          <ErrorNote error={go.error} />
        </div>
      )}
      <ErrorNote error={past.error ?? del.error} />
      {open ? <Result id={open} /> : (
        (past.data ?? []).length > 0 && (
          <div className="card">
            <div className="card-head"><h2 style={{ margin: 0 }}>Earlier comparisons</h2></div>
            <table className="t">
              <thead><tr><th>Specification</th><th>Versions</th><th>Changes</th><th>Breaking</th><th>AI</th><th /></tr></thead>
              <tbody>{past.data!.map((c) => (
                <tr key={c.id} className="click" onClick={() => setSp({ id: String(c.id) })}>
                  <td>{c.to.family}</td><td>{c.from.version} → {c.to.version}</td><td>{c.total}</td><td>{c.by_impact.breaking || ""}</td><td>{c.has_ai ? "✓" : ""}</td>
                  <td><button className="sm" aria-label="Delete comparison" onClick={(e) => { e.stopPropagation(); del.mutate(c.id); }}><Trash2 size={13} /></button></td>
                </tr>))}</tbody>
            </table>
          </div>
        )
      )}
      {open && <p><button className="sm" onClick={() => setSp({})}>← Back to comparisons</button></p>}
    </>
  );
}
