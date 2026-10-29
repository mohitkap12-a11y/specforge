import { useRef, useState } from "react";
import { Copy, Download, FileDown, UploadCloud } from "lucide-react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, PageHeader, Switch } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useJobs } from "../components/jobs";
import { conversionSummary, jobErrorText } from "../util";
import type { ConvertEngine, MarkdownConversion } from "../types";

type Job = { id: number; name: string; state: "working" | "done" | "failed"; jobId?: number; result?: MarkdownConversion; error?: string };
let nextId = 1;

function download(r: MarkdownConversion) {
  const url = URL.createObjectURL(new Blob([r.markdown], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: r.filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function Result({ job }: { job: Job }) {
  const { push } = useToast();
  const r = job.result;
  const [open, setOpen] = useState(false);
  return (
    <div className="card">
      <div className="card-head">
        <strong>{job.name}</strong>
        {job.state === "working" && <span className="spinner" aria-label="Converting" />}
        {job.state === "working" && job.jobId !== undefined && <button onClick={() => void api.cancelJob(job.jobId!)}>Cancel</button>}
        {job.state === "failed" && <Badge value="failed" label="Failed" />}
        {r && <span className="muted small">{conversionSummary(r.stats)}</span>}
        {r && <Badge value={r.engine === "docling" ? "ok" : "info"} label={r.engine === "docling" ? "Docling" : "Built-in"} />}
        <span className="grow" />
        {r && (
          <>
            <button onClick={() => setOpen(!open)}>{open ? "Hide preview" : "Preview"}</button>
            <button onClick={() => navigator.clipboard.writeText(r.markdown).then(() => push({ title: "Markdown copied", tone: "ok" }), () => push({ title: "Copy failed", tone: "bad" }))}><Copy size={14} /> Copy</button>
            <button className="primary" onClick={() => download(r)}><Download size={14} /> Download {r.filename}</button>
          </>
        )}
      </div>
      <ErrorNote error={job.error ? new Error(job.error) : null} />
      {r?.warnings.map((w) => <div key={w} className="muted small">⚠ {w}</div>)}
      {r && open && <pre className="scroll" tabIndex={0}>{r.markdown}</pre>}
    </div>
  );
}

export default function ConvertPage() {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [front, setFront] = useState(true);
  const [engine, setEngine] = useState<ConvertEngine | "">("");   // "" = the server's default (SPECFORGE_CONVERT_ENGINE)
  const { track } = useJobs();
  const [jobs, setJobs] = useState<Job[]>([]);
  const patch = (id: number, p: Partial<Job>) => setJobs((js) => js.map((j) => (j.id === id ? { ...j, ...p } : j)));
  const run = async (files: File[]) => {
    const added = files.map((f) => ({ id: nextId++, name: f.name, state: "working" as const }));
    setJobs((js) => [...added, ...js]);
    for (const [i, f] of files.entries()) {   // background jobs: the server converts them one at a time, however long a spec takes
      const id = added[i].id;
      try {
        const { job } = await api.toMarkdownJob(f, front, engine || undefined);
        patch(id, { jobId: job });
        track(job, `Convert ${f.name}`, (j) => {
          if (j.state === "done") api.markdownResult(job).then((result) => patch(id, { state: "done", result }), (e) => patch(id, { state: "failed", error: e instanceof Error ? e.message : String(e) }));
          else patch(id, { state: "failed", error: j.state === "cancelled" ? "Cancelled." : jobErrorText(j.error) });
        });
      } catch (e) { patch(id, { state: "failed", error: e instanceof Error ? e.message : String(e) }); }
    }
  };
  const onFiles = (list: FileList | null) => { if (list?.length) void run(Array.from(list)); if (input.current) input.current.value = ""; };
  const busy = jobs.some((j) => j.state === "working");
  return (
    <>
      <PageHeader title="To Markdown" description="Convert a PDF, DOC or DOCX specification into a clean Markdown file with Docling: its layout model and TableFormer keep headings, text and tables (merged cells, multi-row headers, tables split across pages) aligned. Large specs take a few minutes. Nothing is stored." />
      <div className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); onFiles(e.dataTransfer.files); }}>
        <div className="ico">{busy ? <span className="spinner" /> : <UploadCloud size={20} />}</div>
        <div className="grow">
          <strong>{busy ? "Converting…" : "Drop PDF, DOC or DOCX files here"}</strong>
          <div className="muted small">Docling is the default engine; Built-in is faster but less accurate on complex tables. Files convert one after another in the background (keep this page open to get the result). Legacy .doc needs LibreOffice on the server.</div>
        </div>
        <select value={engine} onChange={(e) => setEngine(e.target.value as ConvertEngine | "")} aria-label="Conversion engine">
          <option value="">Server default engine</option>
          <option value="docling">Docling (best tables)</option>
          <option value="builtin">Built-in (fast)</option>
        </select>
        <Switch checked={front} onChange={setFront} label="YAML header" />
        <input ref={input} type="file" multiple accept=".pdf,.doc,.docx" className="sr-only" onChange={(e) => onFiles(e.target.files)} aria-label="Files to convert" tabIndex={-1} />
        <button className="primary" onClick={() => input.current?.click()}>Choose files</button>
      </div>
      {jobs.length === 0 && <Empty icon={FileDown} title="Nothing converted yet">Converted files appear here with a preview, copy and download.</Empty>}
      {jobs.map((j) => <Result key={j.id} job={j} />)}
    </>
  );
}
