import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { FileUp, UploadCloud } from "lucide-react";
import { Badge, Empty, ErrorNote, Loading, PageHeader } from "../components/ui";
import { useJobs } from "../components/jobs";
import type { ParseRun, SpecDoc } from "../types";

function RunRow({ doc, run, prev }: { doc: SpecDoc; run: ParseRun; prev?: ParseRun }) {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [diff, setDiff] = useState<Record<string, any> | null>(null);
  const act = useMutation({ mutationFn: () => api.activateRun(run.id), onSuccess: () => qc.invalidateQueries() });
  const reparse = useMutation({ mutationFn: () => api.parse(doc.id), onSuccess: (r) => track(r.job, `Reparse ${doc.family} ${doc.version}`) });
  const cmp = useMutation({ mutationFn: () => api.runDiff(prev!.id, run.id), onSuccess: setDiff });
  const ready = run.audit?.coverage_ready;
  return (
    <>
      <tr>
        <td>#{run.id} {run.active ? <Badge value="active" label="active" /> : null}</td>
        <td><Badge value={run.status} /></td>
        <td>{run.audit?.score !== undefined ? <>{Math.round(run.audit.score * 100)}% <Badge value={ready ? "ok" : "warning"} label={ready ? "coverage-ready" : "not ready"} /></> : "–"}</td>
        <td className="muted small">{run.parser_version}{run.outdated ? " (reparse available)" : ""}</td>
        <td className="row">
          {run.status === "done" && <Link to={`/explorer?run=${run.id}`}>Open</Link>}
          {run.status === "done" && !run.active && <button disabled={act.isPending} onClick={() => act.mutate()}>Set active</button>}
          {prev && run.status === "done" && prev.status === "done" && <button disabled={cmp.isPending} onClick={() => cmp.mutate()}>Diff vs #{prev.id}</button>}
          {run === doc.runs[0] && <button disabled={reparse.isPending} onClick={() => reparse.mutate()}>Reparse</button>}
        </td>
      </tr>
      {(run.error || act.error || cmp.error || reparse.error || diff) && (
        <tr><td colSpan={5}>
          {run.error && <div className="toast bad">{run.error}</div>}
          <ErrorNote error={act.error ?? cmp.error ?? reparse.error} />
          {diff && <pre>{JSON.stringify(diff.summary ?? diff, null, 1)}</pre>}
        </td></tr>
      )}
    </>
  );
}

function DocCard({ doc }: { doc: SpecDoc }) {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [fam, setFam] = useState(doc.family);
  const [ver, setVer] = useState(doc.version);
  const save = useMutation({ mutationFn: () => api.patchSpec(doc.id, { family: fam, version: ver }), onSuccess: () => qc.invalidateQueries() });
  const parse = useMutation({ mutationFn: () => api.parse(doc.id, true), onSuccess: (r) => track(r.job, `Parse ${doc.family} ${doc.version}`) });
  const del = useMutation({ mutationFn: () => api.deleteSpec(doc.id), onSuccess: () => qc.invalidateQueries() });
  const dirty = fam !== doc.family || ver !== doc.version;
  return (
    <div className="card">
      <div className="row">
        <strong className="grow">{doc.filename ?? doc.title}</strong>
        <span className="muted small">{doc.format.toUpperCase()}{doc.pages ? ` · ${doc.pages} pages` : ""}</span>
      </div>
      <div className="row" style={{ margin: "8px 0" }}>
        <label>Family <input value={fam} onChange={(e) => setFam(e.target.value)} size={14} /></label>
        <label>Version <input value={ver} onChange={(e) => setVer(e.target.value)} size={8} /></label>
        <button disabled={!dirty || !fam.trim() || !ver.trim() || save.isPending} onClick={() => save.mutate()}>Save</button>
        <span className="grow" />
        <button className="primary" disabled={parse.isPending} onClick={() => parse.mutate()}>{doc.runs.length ? "Parse again" : "Parse"}</button>
        <button className="danger" disabled={del.isPending} onClick={() => { if (confirm(`Delete ${doc.filename ?? doc.family} and all its parse runs?`)) del.mutate(); }}>Delete</button>
      </div>
      <ErrorNote error={save.error ?? parse.error ?? del.error} />
      {doc.runs.length === 0 ? <div className="muted">Not parsed yet.</div> : (
        <table className="t">
          <thead><tr><th>Run</th><th>Status</th><th>Audit</th><th>Parser</th><th /></tr></thead>
          <tbody>{doc.runs.map((r, i) => <RunRow key={r.id} doc={doc} run={r} prev={doc.runs[i + 1]} />)}</tbody>
        </table>
      )}
    </div>
  );
}

export default function SpecsPage() {
  const qc = useQueryClient();
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const upload = useMutation({
    mutationFn: (files: File[]) => api.upload(files),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["specs"] }),
  });
  const onFiles = (list: FileList | null) => { if (list?.length) upload.mutate(Array.from(list)); if (input.current) input.current.value = ""; };
  return (
    <>
      <PageHeader title="Specs" description="Upload specification documents and parse them into sections, tables, bit fields and normative statements, with an audit of what was found." />
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
      <Loading q={specs} />
      {specs.data?.length === 0 && <Empty icon={FileUp} title="No specs yet">Upload the NVMe Base and NVM Command Set specifications to begin. PDF and DOCX are supported.</Empty>}
      {specs.data?.map((d) => <DocCard key={d.id} doc={d} />)}
      {specs.data && specs.data.length > 0 && <p className="muted">Next: group specs into a set on the <Link to="/sets">Sets</Link> page.</p>}
    </>
  );
}
