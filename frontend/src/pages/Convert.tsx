import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, FileDown, Trash2, UploadCloud } from "lucide-react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, PageHeader, Switch } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useJobs } from "../components/jobs";
import { conversionSummary, jobErrorText } from "../util";
import type { ConvertEngine, ConversionEntry } from "../types";

/** A file still being uploaded, or whose upload was refused (too large, wrong type): not on the server's list yet. */
type Upload = { id: number; name: string; error?: string };
let nextId = 1;

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function Conversion({ c }: { c: ConversionEntry }) {
  const { push } = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const r = c.result;
  const md = useQuery({ queryKey: ["conversion", c.job], queryFn: () => api.markdownResult(c.job), enabled: open && !!r, staleTime: Infinity });
  const working = c.state === "queued" || c.state === "running";
  const copy = () => api.markdownResult(c.job).then((x) => navigator.clipboard.writeText(x.markdown))
    .then(() => push({ title: "Markdown copied", tone: "ok" }), () => push({ title: "Copy failed", tone: "bad" }));
  const remove = () => api.removeConversion(c.job).then(() => qc.invalidateQueries({ queryKey: ["conversions"] }), (e) => push({ title: "Remove failed", description: errText(e), tone: "bad" }));
  return (
    <div className="card">
      <div className="card-head">
        <strong>{c.source ?? `Conversion #${c.job}`}</strong>
        {working && <span className="spinner" aria-label="Converting" />}
        {working && <span className="muted small">{c.state === "queued" ? "Waiting for the previous file…" : c.message ?? "Converting…"}</span>}
        {c.state === "failed" && <Badge value="failed" label="Failed" />}
        {c.state === "cancelled" && <Badge value="draft" label="Cancelled" />}
        {r && <span className="muted small">{conversionSummary(r.stats)}</span>}
        {r && <Badge value={r.engine === "docling" ? "ok" : "info"} label={r.engine === "docling" ? "Docling" : "Built-in"} />}
        <span className="grow" />
        {working && <button onClick={() => void api.cancelJob(c.job)}>Cancel</button>}
        {r && (
          <>
            <button onClick={() => setOpen(!open)}>{open ? "Hide preview" : "Preview"}</button>
            <button onClick={() => void copy()}><Copy size={14} /> Copy</button>
            <a href={`/api/convert/markdown/jobs/${c.job}?download=true`} download={r.filename} tabIndex={-1}><button className="primary"><Download size={14} /> Download {r.filename}</button></a>
          </>
        )}
        {!working && <button aria-label="Remove from the list" title="Remove" onClick={() => void remove()}><Trash2 size={14} /></button>}
      </div>
      {c.state === "failed" && <ErrorNote error={new Error(jobErrorText(c.error))} />}
      {r?.warnings.map((w) => <div key={w} className="muted small">⚠ {w}</div>)}
      {open && md.isLoading && <div className="muted small">Loading…</div>}
      <ErrorNote error={open ? md.error : null} />
      {open && md.data && <pre className="scroll" tabIndex={0}>{md.data.markdown}</pre>}
    </div>
  );
}

export default function ConvertPage() {
  const input = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const [over, setOver] = useState(false);
  const [front, setFront] = useState(true);
  const [engine, setEngine] = useState<ConvertEngine | "">("");   // "" = the server's default (SPECFORGE_CONVERT_ENGINE)
  const [uploads, setUploads] = useState<Upload[]>([]);
  const { track } = useJobs();
  // the list lives on the server, so conversions survive leaving the page, a refresh or another tab
  const list = useQuery({
    queryKey: ["conversions"], queryFn: api.conversions,
    refetchInterval: (q) => ((q.state.data as ConversionEntry[] | undefined)?.some((c) => c.state === "queued" || c.state === "running") ? 1500 : false),
  });
  const run = async (files: File[]) => {
    for (const f of files) {   // background jobs: the server converts them one at a time, however long a spec takes
      const id = nextId++;
      setUploads((u) => [{ id, name: f.name }, ...u]);
      try {
        const { job } = await api.toMarkdownJob(f, front, engine || undefined);
        track(job, `Convert ${f.name}`);
        setUploads((u) => u.filter((x) => x.id !== id));
        await qc.invalidateQueries({ queryKey: ["conversions"] });
      } catch (e) { setUploads((u) => u.map((x) => (x.id === id ? { ...x, error: errText(e) } : x))); }
    }
  };
  const onFiles = (list: FileList | null) => { if (list?.length) void run(Array.from(list)); if (input.current) input.current.value = ""; };
  const items = list.data ?? [];
  const busy = uploads.some((u) => !u.error) || items.some((c) => c.state === "queued" || c.state === "running");
  return (
    <>
      <PageHeader title="To Markdown" description="Convert a PDF, DOC or DOCX specification into a clean Markdown file with Docling: its layout model and TableFormer keep headings, text and tables (merged cells, multi-row headers, tables split across pages) aligned. Large specs take a while. Your converted files stay here for 24 hours." />
      <div className={`dropzone ${over ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); onFiles(e.dataTransfer.files); }}>
        <div className="ico">{busy ? <span className="spinner" /> : <UploadCloud size={20} />}</div>
        <div className="grow">
          <strong>{busy ? "Converting…" : "Drop PDF, DOC or DOCX files here"}</strong>
          <div className="muted small">Docling is the default engine; Built-in is faster but less accurate on complex tables. Files convert one after another in the background; you can leave this page and come back. Legacy .doc needs LibreOffice on the server.</div>
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
      <ErrorNote error={list.error} />
      {uploads.map((u) => (
        <div key={u.id} className="card">
          <div className="card-head">
            <strong>{u.name}</strong>
            {u.error ? <Badge value="failed" label="Failed" /> : <><span className="spinner" aria-label="Uploading" /><span className="muted small">Uploading…</span></>}
            <span className="grow" />
            {u.error && <button aria-label="Dismiss" onClick={() => setUploads((x) => x.filter((y) => y.id !== u.id))}><Trash2 size={14} /></button>}
          </div>
          <ErrorNote error={u.error ? new Error(u.error) : null} />
        </div>
      ))}
      {!list.isLoading && items.length === 0 && uploads.length === 0 && <Empty icon={FileDown} title="Nothing converted yet">Converted files appear here with a preview, copy and download.</Empty>}
      {items.map((c) => <Conversion key={c.job} c={c} />)}
    </>
  );
}
