import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading } from "../components/ui";
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

function Sets({ docs }: { docs: SpecDoc[] }) {
  const { track } = useJobs();
  const qc = useQueryClient();
  const sets = useQuery({ queryKey: ["sets"], queryFn: api.sets });
  const [name, setName] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const create = useMutation({
    mutationFn: () => api.createSet(name.trim(), picked),
    onSuccess: () => { setName(""); setPicked([]); qc.invalidateQueries({ queryKey: ["sets"] }); },
  });
  const build = useMutation({ mutationFn: (id: number) => api.buildCatalog(id), onSuccess: (r, id) => track(r.job, `Catalog for set #${id}`) });
  return (
    <div className="card">
      <h2>Spec sets</h2>
      <p className="muted small">A set groups spec versions that reference each other (e.g. Base 2.4 + NVM Command Set 1.3). Build its catalog to create the coverage denominator and template scenarios.</p>
      <Loading q={sets} />
      {sets.data && sets.data.length > 0 && (
        <table className="t">
          <thead><tr><th>Name</th><th>Members</th><th>Catalog items</th><th>Scenarios</th><th /></tr></thead>
          <tbody>{sets.data.map((s) => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td className="small">{s.members.map((m: any) => `${m.family ?? ""} ${m.version ?? ""}`.trim() || `doc ${m.document_id ?? m.id}`).join(", ")}</td>
              <td>{s.catalog_items}</td><td>{s.scenarios}</td>
              <td><button disabled={build.isPending} onClick={() => build.mutate(s.id)}>{s.catalog_items ? "Rebuild catalog" : "Build catalog"}</button></td>
            </tr>
          ))}</tbody>
        </table>
      )}
      <ErrorNote error={build.error} />
      <h3>New set</h3>
      <div className="row">
        <input placeholder="Set name" value={name} onChange={(e) => setName(e.target.value)} />
        {docs.map((d) => (
          <label key={d.id}><input type="checkbox" checked={picked.includes(d.id)}
            onChange={(e) => setPicked(e.target.checked ? [...picked, d.id] : picked.filter((x) => x !== d.id))} /> {d.family} {d.version}</label>
        ))}
        <button className="primary" disabled={!name.trim() || !picked.length || create.isPending} onClick={() => create.mutate()}>Create</button>
      </div>
      <ErrorNote error={create.error} />
    </div>
  );
}

export default function SpecsPage() {
  const qc = useQueryClient();
  const specs = useQuery({ queryKey: ["specs"], queryFn: api.specs });
  const input = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (files: File[]) => api.upload(files),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["specs"] }),
  });
  const onFiles = (list: FileList | null) => { if (list?.length) upload.mutate(Array.from(list)); if (input.current) input.current.value = ""; };
  return (
    <>
      <h1>Specs</h1>
      <div className="card"
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); onFiles(e.dataTransfer.files); }}>
        <div className="row">
          <input ref={input} type="file" multiple accept=".pdf,.docx" onChange={(e) => onFiles(e.target.files)} aria-label="Spec files" />
          <span className="muted">{upload.isPending ? "Uploading…" : "Drop or choose PDF / DOCX files. Family and version are detected from the title page and can be edited."}</span>
        </div>
        <ErrorNote error={upload.error} />
      </div>
      <Loading q={specs} />
      {specs.data?.length === 0 && <Empty>No specs yet. Upload the Base and NVM Command Set specs to begin.</Empty>}
      {specs.data?.map((d) => <DocCard key={d.id} doc={d} />)}
      {specs.data && specs.data.length > 0 && <Sets docs={specs.data} />}
    </>
  );
}
