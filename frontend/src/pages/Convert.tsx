import { useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, Eye, FileDown, Search, Trash2, UploadCloud, X } from "lucide-react";
import { api } from "../api";
import { MarkdownPreview } from "../components/MarkdownPreview";
import { Badge, Empty, ErrorNote, PageHeader, Segmented, Switch } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useJobs } from "../components/jobs";
import { conversionSummary, jobErrorText } from "../util";
import type { ConvertEngine, ConversionEntry } from "../types";

/** A file still being uploaded, or whose upload was refused (too large, wrong type): not on the server's list yet. */
type Upload = { id: number; name: string; error?: string };
let nextId = 1;

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const when = (ts?: string | null) => (ts ? new Date(ts.replace(" ", "T") + "Z").toLocaleString() : "");

function saveFile(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Copy text that is still being fetched. The clipboard call has to start inside the click (Safari refuses it after an
 *  await), so the text goes in as a promise when the browser supports that; otherwise it is written once it arrives. */
async function copyLater(text: Promise<string>): Promise<void> {
  if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
    const blob = text.then((t) => new Blob([t], { type: "text/plain" }));
    return navigator.clipboard.write([new ClipboardItem({ "text/plain": blob })]);
  }
  return navigator.clipboard.writeText(await text);
}

function Conversion({ c }: { c: ConversionEntry }) {
  const { push } = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const r = c.result;
  const mdQuery = { queryKey: ["conversion", c.job], queryFn: () => api.markdownResult(c.job), staleTime: Infinity };
  const md = useQuery({ ...mdQuery, enabled: open && !!r });
  const fetchMd = () => qc.fetchQuery(mdQuery);     // shared with the preview: fetched once
  const working = c.state === "queued" || c.state === "running";
  const copy = () => {
    const cached = md.data?.markdown;
    (cached !== undefined ? navigator.clipboard.writeText(cached) : copyLater(fetchMd().then((x) => x.markdown)))
      .then(() => push({ title: "Markdown copied", tone: "ok" }), (e) => push({ title: "Copy failed", description: errText(e), tone: "bad" }));
  };
  const download = () => fetchMd().then((x) => saveFile(x.filename, x.markdown), (e) => push({ title: "Download failed", description: errText(e), tone: "bad" }));
  const remove = () => {
    if (r && !c.mine && !window.confirm(`Remove ${c.source ?? "this file"} from the library? ${c.user ?? "Its owner"} converted it, and nobody will be able to open it any more.`)) return;
    api.removeConversion(c.job).then(() => qc.invalidateQueries({ queryKey: ["conversions"] }), (e) => push({ title: "Remove failed", description: errText(e), tone: "bad" }));
  };
  return (
    <div className="card">
      <div className="card-head conv-head">
        <div className="grow">
          <div className="row">
            <strong>{c.source ?? `Conversion #${c.job}`}</strong>
            {working && <span className="spinner" aria-label="Converting" />}
            {working && <span className="muted small">{c.cancelling ? "Cancelling: stops when the current file is done…" : c.state === "queued" ? "Waiting for the previous file…" : c.message ?? "Converting…"}</span>}
            {c.state === "failed" && <Badge value="failed" label="Failed" />}
            {c.state === "cancelled" && <Badge value="draft" label="Cancelled" />}
            {r && <Badge value={r.engine === "docling" ? "ok" : "info"} label={r.engine === "docling" ? "Docling" : "Built-in"} />}
          </div>
          <div className="muted small">
            {r && <>{conversionSummary(r.stats)} · </>}
            {c.mine ? "converted by you" : `converted by ${c.user ?? "unknown"}`}{r ? ` · ${when(c.updated)}` : ""}
          </div>
        </div>
        <div className="row">
          {working && c.mine && !c.cancelling && <button onClick={() => api.cancelJob(c.job).then(() => qc.invalidateQueries({ queryKey: ["conversions"] }))}>Cancel</button>}
          {r && (
            <>
              <button onClick={() => setOpen(true)}><Eye size={14} /> Preview</button>
              <button onClick={copy}><Copy size={14} /> Copy</button>
              <button className="primary" onClick={() => void download()}><Download size={14} /> Download .md</button>
            </>
          )}
          {c.can_remove && <button aria-label="Remove from the list" title={working ? "Cancel and remove" : "Remove"} onClick={remove}><Trash2 size={14} /></button>}
        </div>
      </div>
      {c.state === "failed" && <ErrorNote error={new Error(jobErrorText(c.error))} />}
      {r?.warnings.map((w) => <div key={w} className="muted small">⚠ {w}</div>)}
      {open && r && <MarkdownPreview title={c.source ?? r.filename} data={md.data} loading={md.isLoading} error={md.error}
        onClose={() => setOpen(false)} onCopy={copy} onDownload={() => void download()} />}
    </div>
  );
}

const matches = (c: ConversionEntry, q: string) =>
  [c.source, c.user, c.result?.title, c.result?.filename, c.result?.format, c.result?.engine].some((v) => v?.toLowerCase().includes(q));

export default function ConvertPage() {
  const input = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const [over, setOver] = useState(false);
  const [front, setFront] = useState(true);
  const [engine, setEngine] = useState<ConvertEngine | "">("");   // "" = the server's default (SPECFORGE_CONVERT_ENGINE)
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [filter, setFilter] = useState("");
  const [whose, setWhose] = useState<"all" | "mine">("all");
  const { track } = useJobs();
  const { push } = useToast();
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
        const { job, reused } = await api.toMarkdownJob(f, front, engine || undefined);
        if (reused) push({ title: `${f.name} is already converted`, description: "The existing file in the list is used; it was not converted again.", tone: "ok" });
        else track(job, `Convert ${f.name}`);
        setUploads((u) => u.filter((x) => x.id !== id));
        await qc.invalidateQueries({ queryKey: ["conversions"] });
      } catch (e) { setUploads((u) => u.map((x) => (x.id === id ? { ...x, error: errText(e) } : x))); }
    }
  };
  const onFiles = (list: FileList | null) => { if (list?.length) void run(Array.from(list)); if (input.current) input.current.value = ""; };
  const items = list.data ?? [];
  const q = filter.trim().toLowerCase();
  const shown = useMemo(() => items.filter((c) => (whose === "all" || c.mine) && (!q || matches(c, q))), [items, whose, q]);
  const busy = uploads.some((u) => !u.error) || items.some((c) => c.state === "queued" || c.state === "running");
  return (
    <>
      <PageHeader title="To Markdown" description="Convert a PDF, DOC or DOCX specification into a clean Markdown file with Docling: its layout model and TableFormer keep headings, text and tables (merged cells, multi-row headers, tables split across pages) aligned. Large specs take a while. Converted files form a library every user can preview and download; a file already in it is not converted again." />
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
      {items.length > 0 && (
        <div className="cmp-filters">
          <div className="md-search list-search">
            <Search size={14} className="muted" />
            <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Search by file name, title or who converted it" aria-label="Search converted files" />
            {filter && <button className="iconbtn" aria-label="Clear search" onClick={() => setFilter("")}><X size={14} /></button>}
          </div>
          <Segmented label="Show" value={whose} onChange={setWhose} options={[{ value: "all", label: "All files" }, { value: "mine", label: "Mine" }]} />
          <span className="muted small">{shown.length === items.length ? `${items.length} file${items.length === 1 ? "" : "s"}` : `${shown.length} of ${items.length}`}</span>
        </div>
      )}
      {!list.isLoading && !list.error && items.length === 0 && uploads.length === 0 && <Empty icon={FileDown} title="Nothing converted yet">Converted files appear here with a preview, copy and download.</Empty>}
      {items.length > 0 && shown.length === 0 && <Empty icon={Search} title="No matching files">Nothing in the list matches “{filter.trim()}”{whose === "mine" ? " among your files" : ""}.</Empty>}
      {shown.map((c) => <Conversion key={c.job} c={c} />)}
    </>
  );
}
