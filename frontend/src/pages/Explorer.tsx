import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, fileUrl } from "../api";
import { BookOpen } from "lucide-react";
import { Badge, Empty, ErrorNote, Loading, PageHeader } from "../components/ui";
import type { Issue, SectionData, TreeNode } from "../types";

/** Section key containing a node key (`x/sec:5.2/req:ab` -> `x/sec:5.2`); null for figure-level keys, which have no section in their key. */
const sectionOf = (key: string) => key.match(/^(.*?\/sec:[^/]+)/)?.[1] ?? null;

function buildChildren(nodes: TreeNode[]) {
  const by = new Map<string | null, TreeNode[]>();
  const keys = new Set(nodes.map((n) => n.key));
  for (const n of nodes) {
    const p = n.parent_key && keys.has(n.parent_key) ? n.parent_key : null;   // orphans surface at the root instead of vanishing
    by.set(p, [...(by.get(p) ?? []), n]);
  }
  return by;
}

function Tree({ nodes, selected, onSelect, filter }: { nodes: TreeNode[]; selected: string | null; onSelect: (k: string) => void; filter: string }) {
  const by = useMemo(() => buildChildren(nodes), [nodes]);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const f = filter.trim().toLowerCase();
  const matches = (n: TreeNode) => `${n.number ?? ""} ${n.title}`.toLowerCase().includes(f);
  // when filtering, show a node if it or any descendant matches
  const visible = useMemo(() => {
    if (!f) return null;
    const ok = new Set<string>();
    const walk = (n: TreeNode): boolean => {
      let hit = matches(n);
      for (const c of by.get(n.key) ?? []) if (walk(c)) hit = true;
      if (hit) ok.add(n.key);
      return hit;
    };
    (by.get(null) ?? []).forEach(walk);
    return ok;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [f, by]);
  const render = (n: TreeNode, depth: number): JSX.Element | null => {
    if (visible && !visible.has(n.key)) return null;
    const kids = by.get(n.key) ?? [];
    const expanded = !!visible || open.has(n.key);
    return (
      <div key={n.key}>
        <div className={`tree-row ${selected === n.key ? "sel" : ""}`} style={{ paddingLeft: 4 + depth * 14 }} onClick={() => onSelect(n.key)}
          role="treeitem" aria-selected={selected === n.key} aria-expanded={kids.length ? expanded : undefined}>
          <span className="tw" onClick={(e) => { e.stopPropagation(); if (kids.length) setOpen((o) => { const x = new Set(o); x.has(n.key) ? x.delete(n.key) : x.add(n.key); return x; }); }}>
            {kids.length ? (expanded ? "▾" : "▸") : ""}
          </span>
          <span className="muted">{n.number}</span><span>{n.title}</span>
        </div>
        {expanded && kids.map((c) => render(c, depth + 1))}
      </div>
    );
  };
  return <div role="tree">{(by.get(null) ?? []).map((n) => render(n, 0))}</div>;
}

function TableEditor({ run, t, onDone }: { run: number; t: SectionData["tables"][number]; onDone: () => void }) {
  const [header, setHeader] = useState<string[]>(t.header ?? []);
  const [rows, setRows] = useState<string[][]>((t.rows ?? []).map((r) => r.map((c) => (typeof c === "string" ? c : c === null ? "" : JSON.stringify(c)))));
  const save = useMutation({ mutationFn: () => api.correctTable(run, t.key, header, rows) });
  const cols = Math.max(header.length, ...rows.map((r) => r.length), 1);
  const cell = (r: string[], j: number) => r[j] ?? "";
  const setCell = (i: number, j: number, v: string) => setRows((rs) => rs.map((r, x) => (x === i ? Array.from({ length: cols }, (_, c) => (c === j ? v : cell(r, c))) : r)));
  return (
    <div>
      <div className="scroll">
        <table className="t">
          <thead><tr>{Array.from({ length: cols }, (_, j) => (
            <th key={j}><input value={header[j] ?? ""} aria-label={`Header ${j + 1}`} onChange={(e) => setHeader((h) => Array.from({ length: cols }, (_, c) => (c === j ? e.target.value : h[c] ?? "")))} /></th>
          ))}<th /></tr></thead>
          <tbody>{rows.map((r, i) => (
            <tr key={i}>
              {Array.from({ length: cols }, (_, j) => <td key={j}><input value={cell(r, j)} aria-label={`Row ${i + 1} column ${j + 1}`} onChange={(e) => setCell(i, j, e.target.value)} /></td>)}
              <td><button className="danger" aria-label={`Delete row ${i + 1}`} onClick={() => setRows((rs) => rs.filter((_, x) => x !== i))}>×</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <button onClick={() => setRows((rs) => [...rs, Array(cols).fill("")])}>Add row</button>
        <button className="primary" disabled={save.isPending} onClick={() => save.mutate()}>Save correction</button>
        <button onClick={onDone}>Close editor</button>
      </div>
      <ErrorNote error={save.error} />
      {save.data && <div className="toast ok">{save.data.note}</div>}
    </div>
  );
}

function Content({ data, docId, run }: { data: SectionData; docId: number | null; run: number }) {
  const [editing, setEditing] = useState<string | null>(null);
  const s = data.section;
  const pdf = (p: number | null) => (docId && p ? `${fileUrl(docId)}#page=${p}` : null);
  return (
    <>
      <div className="row">
        <h2 className="grow">{s.number} {s.title}</h2>
        {pdf(s.page_from) && <a href={pdf(s.page_from)!} target="_blank" rel="noreferrer">PDF p.{s.page_from}</a>}
      </div>
      {s.text ? s.text.split(/\n\n+/).map((p, i) => <p key={i}>{p}</p>) : <p className="muted">No body text in this section.</p>}
      {data.requirements.length > 0 && <>
        <h3>Normative statements ({data.requirements.length})</h3>
        {data.requirements.map((r) => (
          <div key={r.key} className={`req ${r.strength ?? ""}`}>
            <span className="badge">{r.strength ?? "?"}</span> {r.text}
            {pdf(r.page_from) && <> <a className="small" href={pdf(r.page_from)!} target="_blank" rel="noreferrer">p.{r.page_from}</a></>}
          </div>
        ))}
      </>}
      {data.tables.map((t) => (
        <div key={t.key} style={{ marginTop: 14 }}>
          <h3>{t.number} {t.title} {pdf(t.page_from) && <a className="small" href={pdf(t.page_from)!} target="_blank" rel="noreferrer">p.{t.page_from}</a>}</h3>
          {editing === t.key ? <TableEditor run={run} t={t} onDone={() => setEditing(null)} /> : <div className="scroll">
            <table className="t">
              {t.header && <thead><tr>{t.header.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>}
              <tbody>{(t.rows ?? []).map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{typeof c === "string" ? c : JSON.stringify(c)}</td>)}</tr>)}</tbody>
            </table>
          </div>}
          {editing !== t.key && <button className="small" style={{ marginTop: 6 }} onClick={() => setEditing(t.key)}>Edit table</button>}
        </div>
      ))}
      {data.references.length > 0 && <>
        <h3>References</h3>
        <div className="row">{data.references.map((r, i) => <span key={i} className="badge info" title={`${r.kind}${r.target_family ? ` · ${r.target_family}` : ""}`}>{r.raw}</span>)}</div>
      </>}
    </>
  );
}

function Issues({ run, onGoto }: { run: number; onGoto: (key: string) => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["issues", run], queryFn: () => api.issues(run) });
  const [sev, setSev] = useState("");
  const patch = useMutation({
    mutationFn: ({ i, state }: { i: Issue; state: string }) => api.patchIssue(i.id, { state, note: i.note ?? undefined }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["issues", run] }),
  });
  const list = (q.data ?? []).filter((i) => !sev || i.severity === sev);
  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <select value={sev} onChange={(e) => setSev(e.target.value)} aria-label="Severity">
          <option value="">All severities</option><option value="error">Errors</option><option value="warning">Warnings</option><option value="info">Info</option>
        </select>
        <span className="muted">{list.length} issue{list.length === 1 ? "" : "s"}</span>
      </div>
      <Loading q={q} /><ErrorNote error={patch.error} />
      <div className="scroll">
        <table className="t">
          <thead><tr><th>Severity</th><th>Kind</th><th>Where</th><th>Message</th><th>State</th></tr></thead>
          <tbody>{list.map((i) => (
            <tr key={i.id}>
              <td><Badge value={i.severity} label={i.severity} /></td><td className="small">{i.kind}</td>
              <td className="small">{i.node_key ? (sectionOf(i.node_key) ? <a href="#" onClick={(e) => { e.preventDefault(); onGoto(sectionOf(i.node_key!)!); }}>{i.node_key.split("/").pop()}</a> : i.node_key.split("/").pop()) : "–"}{i.page ? ` p.${i.page}` : ""}</td>
              <td>{i.message}</td>
              <td><select value={i.state} onChange={(e) => patch.mutate({ i, state: e.target.value })} aria-label="Issue state">
                <option value="open">open</option><option value="accepted">accepted</option><option value="resolved">resolved</option></select></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}

export default function ExplorerPage() {
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState("");
  const [tab, setTab] = useState<"content" | "issues">("content");
  const parsed = (specs.data ?? []).flatMap((d) => d.runs.filter((r) => r.status === "done").map((r) => ({ d, r })));
  const runParam = Number(params.get("run")) || null;
  const cur = parsed.find((x) => x.r.id === runParam) ?? parsed.find((x) => x.r.active) ?? parsed[0];
  const run = cur?.r.id ?? null;
  const sel = params.get("sec");
  const set = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) v === null ? n.delete(k) : n.set(k, v);
    setParams(n, { replace: true });
  };
  const tree = useQuery({ queryKey: ["tree", run], queryFn: () => api.tree(run!), enabled: run !== null });
  const section = useQuery({ queryKey: ["section", run, sel], queryFn: () => api.section(run!, sel!), enabled: run !== null && !!sel });
  return (
    <>
      <PageHeader title="Explorer" description="Browse what the parser found: section text, normative statements, tables (editable) and cross-references." />
      <Loading q={specs} />
      {specs.data && !parsed.length && <Empty icon={BookOpen} title="Nothing parsed yet" action={<Link to="/specs"><button className="primary">Go to Specs</button></Link>}>Upload and parse a specification to explore its contents here.</Empty>}
      {cur && (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <label>Parse run{" "}
              <select value={run ?? ""} onChange={(e) => set({ run: e.target.value, sec: null })}>
                {parsed.map(({ d, r }) => <option key={r.id} value={r.id}>{d.family} {d.version} · run #{r.id}{r.active ? " (active)" : ""}</option>)}
              </select>
            </label>
            <a href={fileUrl(cur.d.id)} target="_blank" rel="noreferrer">Open original file</a>
            <div className="tabs">
              <button className={tab === "content" ? "active" : ""} onClick={() => setTab("content")}>Content</button>
              <button className={tab === "issues" ? "active" : ""} onClick={() => setTab("issues")}>Parse issues</button>
            </div>
          </div>
          <div className="explorer">
            <div className="card treebox">
              <input placeholder="Filter sections…" value={filter} onChange={(e) => setFilter(e.target.value)} style={{ width: "100%", marginBottom: 8 }} />
              <Loading q={tree} />
              {tree.data && <Tree nodes={tree.data} selected={sel} filter={filter} onSelect={(k) => { set({ sec: k }); setTab("content"); }} />}
            </div>
            <div className="card">
              {tab === "issues" && run !== null && <Issues run={run} onGoto={(k) => { set({ sec: k }); setTab("content"); }} />}
              {tab === "content" && (!sel ? <div className="muted">Pick a section from the tree.</div> : <><Loading q={section} />{section.data && <Content key={section.data.section.key} data={section.data} docId={cur.d.id} run={run!} />}</>)}
            </div>
          </div>
        </>
      )}
    </>
  );
}
