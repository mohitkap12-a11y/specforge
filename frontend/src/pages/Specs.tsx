import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { AlertTriangle, ChevronDown, ChevronUp, FileUp, UploadCloud, X } from "lucide-react";
import { Badge, Empty, ErrorNote, Loading, PageHeader } from "../components/ui";
import { useJobs } from "../components/jobs";
import { useAuth } from "../components/auth";
import type { ParseRun, ParserEngine, SpecDoc } from "../types";

const engineOf = (run: ParseRun) => (run.stats?.engine === "docling" ? "docling" : "builtin");

/** A run's number within its own spec: 1 for the first parse of that file, the highest for the latest (runs come newest first). */
export const runNumber = (doc: SpecDoc, run: ParseRun) => doc.runs.length - doc.runs.findIndex((r) => r.id === run.id);
/** The run the spec is served from (the active one), else the latest. */
export const currentRun = (doc: SpecDoc): ParseRun | undefined => doc.runs.find((r) => r.active) ?? doc.runs[0];

/** Errors of a run (its parse error, a failed action) behind a small chip: hover for the message, click to keep it open. */
function ErrorChip({ messages }: { messages: string[] }) {
  const [open, setOpen] = useState(false);
  if (!messages.length) return null;
  const text = messages.join("\n\n");
  return (
    <span className="err-chip-wrap">
      <button className="sm err-chip" aria-expanded={open} title={text} onClick={() => setOpen((o) => !o)}>
        <AlertTriangle size={12} /> Error
      </button>
      {open && (
        <div className="err-pop" role="dialog" aria-label="Error details">
          <div className="row"><strong className="grow">Error</strong><button className="sm" aria-label="Close" onClick={() => setOpen(false)}><X size={12} /></button></div>
          <pre>{text}</pre>
        </div>
      )}
    </span>
  );
}

const errText = (e: unknown) => (e instanceof Error ? e.message : e ? String(e) : "");

function RunRow({ doc, run, prev, reparseHere }: { doc: SpecDoc; run: ParseRun; prev?: ParseRun; reparseHere: boolean }) {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [diff, setDiff] = useState<Record<string, any> | null>(null);
  const act = useMutation({ mutationFn: () => api.activateRun(run.id), onSuccess: () => qc.invalidateQueries() });
  const [engine, setEngine] = useState<"builtin" | "docling">(engineOf(run));
  const reparse = useMutation({ mutationFn: () => api.parse(doc.id, false, engine), onSuccess: (r) => track(r.job, `Reparse ${doc.family} ${doc.version}`) });
  const cmp = useMutation({ mutationFn: () => api.runDiff(prev!.id, run.id), onSuccess: setDiff });
  const ready = run.audit?.coverage_ready;
  const errors = [run.error ?? "", errText(act.error), errText(cmp.error), errText(reparse.error)].filter(Boolean);
  return (
    <>
      <tr>
        <td title={`Parse run id ${run.id}`}>#{runNumber(doc, run)} {run.active ? <Badge value="active" label="active" /> : null}</td>
        <td><span className="row" style={{ gap: 6, flexWrap: "nowrap" }}><Badge value={run.status} /><ErrorChip messages={errors} /></span></td>
        <td>{run.audit?.score !== undefined ? <>{Math.round(run.audit.score * 100)}% <Badge value={ready ? "ok" : "warning"} label={ready ? "coverage-ready" : "not ready"} /></> : "–"}</td>
        <td className="muted small">
          {run.status === "done" && <Badge value={engineOf(run) === "docling" ? "ok" : "info"} label={engineOf(run) === "docling" ? "Docling" : "Built-in"} />}{" "}
          {run.parser_version}{run.outdated ? " (reparse available)" : ""}
        </td>
        <td className="row">
          {run.status === "done" && <Link to={`/explorer?run=${run.id}`}>Open</Link>}
          {run.status === "done" && !run.active && <button disabled={act.isPending} onClick={() => act.mutate()}>Set active</button>}
          {prev && run.status === "done" && prev.status === "done" && <button disabled={cmp.isPending} onClick={() => cmp.mutate()}>Diff vs #{runNumber(doc, prev)}</button>}
          {reparseHere && (
            <>
              <select value={engine} onChange={(e) => setEngine(e.target.value as "builtin" | "docling")} aria-label="Engine to reparse with"
                title="Built-in rebuilds nested tables (a field's bits, a bit's values) from the PDF; Docling uses the file's Docling conversion">
                <option value="builtin">Built-in</option>
                <option value="docling">Docling</option>
              </select>
              <button disabled={reparse.isPending} onClick={() => reparse.mutate()}>Reparse</button>
            </>
          )}
        </td>
      </tr>
      {diff && (
        <tr><td colSpan={5}>
          <div className="row"><strong className="grow small">Diff vs #{prev ? runNumber(doc, prev) : "?"}</strong><button className="sm" onClick={() => setDiff(null)}>Hide</button></div>
          <pre>{JSON.stringify(diff.summary ?? diff, null, 1)}</pre>
        </td></tr>
      )}
    </>
  );
}

function DocCard({ doc }: { doc: SpecDoc }) {
  const { track } = useJobs();
  const { isAdmin } = useAuth();
  const qc = useQueryClient();
  const [fam, setFam] = useState(doc.family);
  const [ver, setVer] = useState(doc.version);
  const save = useMutation({ mutationFn: () => api.patchSpec(doc.id, { family: fam, version: ver }), onSuccess: () => qc.invalidateQueries() });
  const [engine, setEngine] = useState<ParserEngine>("auto");
  const parse = useMutation({
    mutationFn: () => api.parse(doc.id, true, engine === "auto" ? undefined : engine),
    onSuccess: (r) => { track(r.job, `Parse ${doc.family} ${doc.version}`); qc.invalidateQueries({ queryKey: ["specs"] }); },
  });
  const del = useMutation({ mutationFn: () => api.deleteSpec(doc.id), onSuccess: () => qc.invalidateQueries() });
  // collapsed by default: one row for the run the spec is served from; the rest on demand
  const [expanded, setExpanded] = useState(false);
  const current = currentRun(doc), latest = doc.runs[0];
  const shown = expanded ? doc.runs : current ? [current] : [];
  const dirty = fam !== doc.family || ver !== doc.version;
  return (
    <div className="card">
      <div className="row">
        <strong className="grow">{doc.filename ?? doc.title}</strong>
        {doc.docling && <span title="This file was converted with Docling (To Markdown library): parsing reuses that conversion"><Badge value="ok" label="Docling conversion ready" /></span>}
        <span className="muted small">{doc.format.toUpperCase()}{doc.pages ? ` · ${doc.pages} pages` : ""}</span>
      </div>
      <div className="row" style={{ margin: "8px 0" }}>
        <label>Family <input value={fam} onChange={(e) => setFam(e.target.value)} size={14} /></label>
        <label>Version <input value={ver} onChange={(e) => setVer(e.target.value)} size={8} /></label>
        <button disabled={!dirty || !fam.trim() || !ver.trim() || save.isPending} onClick={() => save.mutate()}>Save</button>
        <span className="grow" />
        <select value={engine} onChange={(e) => setEngine(e.target.value as ParserEngine)} aria-label="Parser"
          title="Docling rebuilds tables (merged cells, multi-row headers, tables over several pages) far better than the built-in parser">
          <option value="auto">{doc.docling ? "Docling (conversion ready)" : "Auto: Docling if converted, else built-in"}</option>
          {!doc.docling && <option value="docling">Docling: convert first (slow)</option>}
          <option value="builtin">Built-in (fast)</option>
        </select>
        <button className="primary" disabled={parse.isPending} onClick={() => parse.mutate()}>{doc.runs.length ? "Parse again" : "Parse"}</button>
        {isAdmin && <button className="danger" disabled={del.isPending} onClick={() => { if (confirm(`Delete ${doc.filename ?? doc.family} and all its parse runs?`)) del.mutate(); }}>Delete</button>}
      </div>
      <ErrorNote error={save.error ?? parse.error ?? del.error} />
      {doc.runs.length === 0 ? <div className="muted">Not parsed yet.</div> : (
        <>
          <table className="t">
            <thead><tr><th>Run</th><th>Status</th><th>Audit</th><th>Parser</th><th /></tr></thead>
            <tbody>{shown.map((r) => {
              const i = doc.runs.indexOf(r);
              return <RunRow key={r.id} doc={doc} run={r} prev={doc.runs[i + 1]} reparseHere={expanded ? i === 0 : r === current} />;
            })}</tbody>
          </table>
          {doc.runs.length > 1 && (
            <button className="sm link-btn" aria-expanded={expanded} onClick={() => setExpanded((x) => !x)}>
              {expanded ? <ChevronUp size={13} /> : <ChevronDown size={13} />}{" "}
              {expanded ? "Show only the current parse" : `Show all ${doc.runs.length} parses`}
              {!expanded && latest !== current && <span className="muted"> · latest #{runNumber(doc, latest)}: {latest.status}</span>}
            </button>
          )}
        </>
      )}
    </div>
  );
}

export default function SpecsPage() {
  const qc = useQueryClient();
  const { coverage } = useAuth();
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const upload = useMutation({
    mutationFn: (files: File[]) => api.upload(files),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["specs"] }),
  });
  const onFiles = (list: FileList | null) => { if (list?.length) upload.mutate(Array.from(list)); if (input.current) input.current.value = ""; };
  const { isAdmin } = useAuth();
  const { track } = useJobs();
  const [engine, setEngine] = useState<ParserEngine | "">("builtin");
  const outdated = (specs.data ?? []).filter((d) => d.runs.some((r) => r.active && r.outdated));
  const reparseAll = useMutation({
    mutationFn: () => api.reparseOutdated(engine || undefined),
    onSuccess: (r) => { r.jobs.forEach((j) => track(j.job, `Reparse spec #${j.document_id}`)); void qc.invalidateQueries({ queryKey: ["specs"] }); },
  });
  return (
    <>
      <PageHeader title="Specs" description={<>Upload specification documents and parse them into sections, tables, bit fields and normative statements, with an audit of what was found. Parsing uses Docling (accurate tables) whenever the file has been converted on the <Link to="/convert">To Markdown</Link> page; a spec converted there can also be added here with one click, and is never converted twice.</>} />
      <div className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); onFiles(e.dataTransfer.files); }}>
        <div className="ico">{upload.isPending ? <span className="spinner" /> : <UploadCloud size={20} />}</div>
        <div className="grow">
          <strong>{upload.isPending ? "Uploading…" : "Drop PDF or DOCX files here"}</strong>
          <div className="muted small">Family and version are detected from the title page and can be edited afterwards.</div>
        </div>
        <input ref={input} type="file" multiple accept=".pdf,.docx" className="sr-only" onChange={(e) => onFiles(e.target.files)} aria-label="Spec files" tabIndex={-1} />
        <button className="primary" disabled={upload.isPending} onClick={() => input.current?.click()}>Choose files</button>
      </div>
      <ErrorNote error={upload.error} />
      {isAdmin && outdated.length > 0 && (
        <div className="card row">
          <span className="grow">
            <strong>{outdated.length} spec{outdated.length === 1 ? " is" : "s are"} served from a run made by an older parser.</strong>{" "}
            <span className="muted small">Reparsing rebuilds nested tables (a field's bits, a bit's values) with the built-in engine. The new runs are not activated: open each, compare it with the old run, then Set active. Rebuild the catalogs of their sets afterwards.</span>
          </span>
          <select value={engine} onChange={(e) => setEngine(e.target.value as ParserEngine | "")} aria-label="Engine to reparse with">
            <option value="builtin">Built-in</option>
            <option value="">Same engine as before</option>
            <option value="docling">Docling</option>
          </select>
          <button className="primary" disabled={reparseAll.isPending} onClick={() => reparseAll.mutate()}>Reparse all {outdated.length}</button>
        </div>
      )}
      <ErrorNote error={reparseAll.error} />
      <Loading q={specs} />
      {specs.data?.length === 0 && <Empty icon={FileUp} title="No specs yet">Upload the NVMe Base and NVM Command Set specifications to begin. PDF and DOCX are supported.</Empty>}
      {specs.data?.map((d) => <DocCard key={d.id} doc={d} />)}
      {specs.data && specs.data.length > 0 && coverage !== "soon" && <p className="muted">Next: group specs into a set on the <Link to="/sets">Sets</Link> page.</p>}
    </>
  );
}
