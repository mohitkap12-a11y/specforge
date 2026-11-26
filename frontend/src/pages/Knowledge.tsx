import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookPlus, Eye, FileText, Library, Search, Trash2, UploadCloud, X } from "lucide-react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading, PageHeader, Segmented, Switch } from "../components/ui";
import { MarkdownPreview } from "../components/MarkdownPreview";
import { useAuth } from "../components/auth";
import { useJobs } from "../components/jobs";
import { useToast } from "../components/Toasts";
import { saveFile } from "../download";
import { copyText } from "../clipboard";
import { useDebounced } from "../hooks";
import type { KbDoc, KbHit, MarkdownConversion } from "../types";
import SpecsPage from "./Specs";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const when = (ts?: string | null) => (ts ? new Date(ts.replace(" ", "T") + "Z").toLocaleString() : "");
const ext = (n: string) => n.slice(n.lastIndexOf(".")).toLowerCase();

function Preview({ doc, onClose }: { doc: KbDoc; onClose: () => void }) {
  const q = useQuery({ queryKey: ["kb-doc", doc.id], queryFn: () => api.kbDoc(doc.id), staleTime: Infinity });
  const data = q.data as unknown as MarkdownConversion | undefined;
  return <MarkdownPreview title={doc.title} data={data} loading={q.isLoading} error={q.error} onClose={onClose}
    onCopy={() => void copyText(q.data?.markdown ?? "")} onDownload={() => q.data && saveFile(`${doc.title}.md`, q.data.markdown, "text/markdown")} />;
}

function SearchTest() {
  const [text, setText] = useState("");
  const q = useDebounced(text.trim(), 300);
  const hits = useQuery({ queryKey: ["kb-search", q], queryFn: () => api.kbSearch(q), enabled: q.length > 1 });
  return (
    <div className="card">
      <div className="row"><strong className="grow">Test search</strong><span className="muted small">What the chatbot finds for a question</span></div>
      <div className="md-search list-search" style={{ marginTop: 8 }}>
        <Search size={14} className="muted" />
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="e.g. what does Identify Controller return?" aria-label="Test the knowledge base search" />
        {text && <button className="iconbtn" aria-label="Clear" onClick={() => setText("")}><X size={14} /></button>}
      </div>
      {hits.data?.length === 0 && q.length > 1 && <div className="muted small" style={{ marginTop: 8 }}>Nothing found. The chatbot would answer that it has no source.</div>}
      {hits.data?.map((h: KbHit, i) => (
        <div key={h.id} className="kb-hit">
          <div className="small"><b>{i + 1}.</b> <strong>{h.title}</strong>{h.heading ? <span className="muted"> › {h.heading}</span> : null}</div>
          <pre>{h.text.slice(0, 400)}{h.text.length > 400 ? "…" : ""}</pre>
        </div>
      ))}
    </div>
  );
}

function Documents() {
  const qc = useQueryClient();
  const { isAdmin } = useAuth();
  const { track } = useJobs();
  const { push } = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState<KbDoc | null>(null);
  const [filter, setFilter] = useState("");
  const [autoAdd, setAutoAdd] = useState(true);
  const [waiting, setWaiting] = useState<number[]>([]);       // conversions started here: added as soon as they finish
  const kb = useQuery({ queryKey: ["kb"], queryFn: api.kb, refetchInterval: 5000 });
  const refresh = () => { void qc.invalidateQueries({ queryKey: ["kb"] }); void qc.invalidateQueries({ queryKey: ["conversions"] }); };
  const report = (r: { added: unknown[]; failed: { error: string }[] }) => push({
    title: `Added ${r.added.length} file${r.added.length === 1 ? "" : "s"} to the knowledge base`, description: r.failed.length ? r.failed.map((f) => f.error).join("; ") : undefined, tone: r.failed.length ? (r.added.length ? "info" : "bad") : "ok" });

  const addPending = useMutation({ mutationFn: (jobs?: number[]) => api.kbAddConversions(jobs), onSuccess: (r) => { report(r); refresh(); }, onError: (e) => push({ title: "Not added", description: errText(e), tone: "bad" }) });
  const toggle = useMutation({ mutationFn: (d: KbDoc) => api.kbEnable(d.id, !d.enabled), onSuccess: refresh });
  const del = useMutation({ mutationFn: (id: number) => api.kbDelete(id), onSuccess: refresh });
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      const direct = files.filter((f) => kb.data?.direct.includes(ext(f.name)));
      const convert = files.filter((f) => kb.data?.convertible.includes(ext(f.name)));
      const rest = files.filter((f) => !direct.includes(f) && !convert.includes(f));
      if (rest.length) push({ title: `${rest.length} file${rest.length === 1 ? "" : "s"} not supported`, description: rest.map((f) => f.name).join(", "), tone: "bad" });
      if (direct.length) report(await api.kbUpload(direct));
      for (const f of convert) {     // PDF, DOC and DOCX: To Markdown first, in the background
        const { job, reused } = await api.toMarkdownJob(f);
        if (autoAdd) setWaiting((w) => [...w, job]);
        track(job, `Convert ${f.name}`, () => refresh());
        if (reused) push({ title: `${f.name} is already converted`, description: "Its existing conversion is used.", tone: "ok" });
        else push({ title: `Converting ${f.name}`, description: autoAdd ? "It is added to the knowledge base when the conversion finishes." : "Add it from the list below when the conversion finishes.", tone: "info" });
      }
    },
    onSuccess: refresh, onError: (e) => push({ title: "Upload failed", description: errText(e), tone: "bad" }),
  });
  // conversions started here: add each one as soon as it shows up as converted and not added
  useEffect(() => {
    const ready = (kb.data?.pending ?? []).filter((p) => waiting.includes(p.job)).map((p) => p.job);
    if (ready.length && !addPending.isPending) { setWaiting((w) => w.filter((j) => !ready.includes(j))); addPending.mutate(ready); }
  }, [kb.data, waiting, addPending]);

  const onFiles = (l: FileList | null) => { if (l?.length) upload.mutate(Array.from(l)); if (input.current) input.current.value = ""; };
  const q = filter.trim().toLowerCase();
  const docs = (kb.data?.docs ?? []).filter((d) => !q || [d.title, d.filename, d.added_by].some((v) => v?.toLowerCase().includes(q)));
  const pending = kb.data?.pending ?? [];
  const accept = [...(kb.data?.direct ?? []), ...(kb.data?.convertible ?? [])].join(",");

  return (
    <>
      {isAdmin && (
        <div className={`dropzone ${over ? "over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); onFiles(e.dataTransfer.files); }}>
          <div className="ico">{upload.isPending ? <span className="spinner" /> : <UploadCloud size={20} />}</div>
          <div className="grow">
            <strong>{upload.isPending ? "Uploading…" : "Drop any document here"}</strong>
            <div className="muted small">Markdown, text, HTML, JSON, YAML and CSV are added at once. PDF, Word (DOCX, DOC) go through <Link to="/convert">To Markdown</Link> first.</div>
          </div>
          <label className="row small" title="Add PDF and Word files to the knowledge base as soon as they are converted"><Switch checked={autoAdd} onChange={setAutoAdd} label="Add when converted" /> Add when converted</label>
          <input ref={input} type="file" multiple accept={accept} className="sr-only" onChange={(e) => onFiles(e.target.files)} aria-label="Documents" tabIndex={-1} />
          <button className="primary" disabled={upload.isPending || !kb.data} onClick={() => input.current?.click()}>Choose files</button>
        </div>
      )}
      {pending.length > 0 && (
        <div className="card">
          <div className="row">
            <span className="grow"><strong>{pending.length} converted file{pending.length === 1 ? "" : "s"} not added yet</strong>
              <span className="muted small"> · {isAdmin ? "from the To Markdown library" : "an admin adds them"}</span></span>
            {isAdmin && <button className="primary" disabled={addPending.isPending} onClick={() => addPending.mutate(undefined)}><BookPlus size={14} /> Add all {pending.length}</button>}
          </div>
          <table className="t" style={{ marginTop: 8 }}>
            <tbody>{pending.map((p) => (
              <tr key={p.job}>
                <td>{p.source ?? `Conversion #${p.job}`}</td>
                <td className="muted small">converted by {p.user ?? "unknown"} · {when(p.created)}</td>
                <td style={{ textAlign: "right" }}>{isAdmin && <button className="sm" disabled={addPending.isPending} onClick={() => addPending.mutate([p.job])}>Add</button>}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <ErrorNote error={kb.error} />
      <Loading q={kb} />
      {kb.data && kb.data.docs.length === 0 && pending.length === 0 && (
        <Empty icon={Library} title="The knowledge base is empty">{isAdmin ? "Drop documents above, or convert specifications on the To Markdown page and add them here." : "An admin has not added documents yet."}</Empty>
      )}
      {kb.data && kb.data.docs.length > 0 && (
        <>
          <div className="cmp-filters">
            <div className="md-search list-search">
              <Search size={14} className="muted" />
              <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter documents" aria-label="Filter documents" />
              {filter && <button className="iconbtn" aria-label="Clear filter" onClick={() => setFilter("")}><X size={14} /></button>}
            </div>
            <span className="muted small">{kb.data.docs.length} document{kb.data.docs.length === 1 ? "" : "s"} · {kb.data.chunks} searchable passages</span>
          </div>
          {docs.map((d) => (
            <div key={d.id} className={`card ${d.enabled ? "" : "kb-off"}`}>
              <div className="row">
                <FileText size={16} className="muted" aria-hidden="true" />
                <div className="grow">
                  <strong>{d.title}</strong>{" "}
                  {!d.enabled && <Badge value="draft" label="Not used by the chatbot" />}
                  <div className="muted small">{d.filename} · {d.source === "conversion" ? "from To Markdown" : "uploaded"} by {d.added_by ?? "unknown"} · {d.chunks} passages · {d.chars < 1000 ? d.chars : `${Math.round(d.chars / 1000)}k`} characters · {when(d.created)}</div>
                </div>
                <button onClick={() => setPreview(d)}><Eye size={14} /> Preview</button>
                {isAdmin && <label className="row small" title="Turn off to keep the document but hide it from the chatbot"><Switch checked={!!d.enabled} onChange={() => toggle.mutate(d)} label={`Use ${d.title} in chat`} /> In chat</label>}
                {isAdmin && <button className="danger" aria-label={`Remove ${d.title}`} onClick={() => { if (confirm(`Remove “${d.title}” from the knowledge base? The chatbot will no longer see it.`)) del.mutate(d.id); }}><Trash2 size={14} /></button>}
              </div>
            </div>
          ))}
          <SearchTest />
        </>
      )}
      <ErrorNote error={toggle.error ?? del.error} />
      {preview && <Preview doc={preview} onClose={() => setPreview(null)} />}
    </>
  );
}

export default function KnowledgePage() {
  const [params, setParams] = useSearchParams();
  const { coverage } = useAuth();
  const specsOk = coverage !== "soon";
  const tab = specsOk && params.get("tab") === "specs" ? "specs" : "docs";
  return (
    <>
      <PageHeader title="Knowledge base" description={<>The documents the <Link to="/chat">chatbot</Link> answers from. Add converted specifications from <Link to="/convert">To Markdown</Link>, or upload any document: it is converted if needed and indexed for instant search.</>} />
      {specsOk && <div style={{ marginBottom: 14 }}><Segmented label="Knowledge base section" value={tab} onChange={(v) => setParams(v === "specs" ? { tab: "specs" } : {})}
        options={[{ value: "docs", label: "Documents" }, { value: "specs", label: "Parsed specs" }]} /></div>}
      {tab === "specs" ? <SpecsPage embedded /> : <Documents />}
    </>
  );
}
