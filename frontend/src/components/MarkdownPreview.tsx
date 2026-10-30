import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, Copy, Download, Search, X } from "lucide-react";
import { MarkdownView, blockText, countHits, parseMarkdown, sourceBlocks } from "./Markdown";
import { ErrorNote, Segmented } from "./ui";
import type { MarkdownConversion } from "../types";

type Mode = "formatted" | "source";

/** Full-screen preview of a converted file: rendered Markdown (or its source), an outline, search and download. */
export function MarkdownPreview({ title, data, loading, error, onClose, onCopy, onDownload }: {
  title: string; data?: MarkdownConversion; loading: boolean; error: unknown;
  onClose: () => void; onCopy: () => void; onDownload: () => void;
}) {
  const [mode, setMode] = useState<Mode>("formatted");
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  const [current, setCurrent] = useState(0);
  const [jump, setJump] = useState<{ block: number; n: number }>();
  const search = useRef<HTMLInputElement>(null);

  const markdown = data?.markdown ?? "";
  const blocks = useMemo(() => (mode === "formatted" ? parseMarkdown(markdown) : sourceBlocks(markdown)), [markdown, mode]);
  const outline = useMemo(() => (mode === "formatted" ? blocks.flatMap((b, i) => (b.t === "h" && b.level <= 4 ? [{ i, level: b.level, text: blockText(b) }] : [])) : []), [blocks, mode]);
  const total = useMemo(() => countHits(blocks, query), [blocks, query]);

  useEffect(() => { const t = setTimeout(() => setQuery(text.trim()), 200); return () => clearTimeout(t); }, [text]);
  useEffect(() => setCurrent(0), [query, mode]);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    search.current?.focus();
    return () => { document.body.style.overflow = overflow; prev?.focus?.(); };
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); search.current?.select(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const step = (d: number) => { if (total) setCurrent((c) => (c + d + total) % total); };
  return (
    <div className="modal-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal md-modal" role="dialog" aria-modal="true" aria-label={`Preview of ${title}`}>
        <header className="modal-head">
          <div className="grow">
            <strong className="modal-title">{data?.title || title}</strong>
            {data && <div className="muted small">{data.filename}</div>}
          </div>
          <button onClick={onCopy} disabled={!data}><Copy size={14} /> Copy</button>
          <button className="primary" onClick={onDownload} disabled={!data}><Download size={14} /> Download</button>
          <button className="iconbtn" aria-label="Close preview" title="Close (Esc)" onClick={onClose}><X size={16} /></button>
        </header>
        <div className="modal-bar">
          <div className="md-search">
            <Search size={14} className="muted" />
            <input ref={search} value={text} placeholder="Search this document" aria-label="Search this document"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); if (text.trim() !== query) setQuery(text.trim()); else step(e.shiftKey ? -1 : 1); } }} />
            {query && <span className="muted small md-count" aria-live="polite">{total ? `${current + 1} of ${total}` : "No matches"}</span>}
            <button className="iconbtn" aria-label="Previous match" title="Previous (Shift+Enter)" disabled={!total} onClick={() => step(-1)}><ChevronUp size={14} /></button>
            <button className="iconbtn" aria-label="Next match" title="Next (Enter)" disabled={!total} onClick={() => step(1)}><ChevronDown size={14} /></button>
            {text && <button className="iconbtn" aria-label="Clear search" onClick={() => { setText(""); setQuery(""); search.current?.focus(); }}><X size={14} /></button>}
          </div>
          <span className="grow" />
          <Segmented label="View" value={mode} onChange={setMode} options={[{ value: "formatted", label: "Formatted" }, { value: "source", label: "Markdown source" }]} />
        </div>
        <div className="modal-body">
          {outline.length > 1 && (
            <nav className="md-outline" aria-label="Outline">
              {outline.map((o) => (
                <button key={o.i} style={{ paddingLeft: 8 + (o.level - 1) * 12 }} title={o.text} onClick={() => setJump({ block: o.i, n: Date.now() })}>{o.text}</button>
              ))}
            </nav>
          )}
          <div className="md-scroll" tabIndex={0}>
            {loading && <div className="row muted" role="status"><span className="spinner" /> Loading…</div>}
            <ErrorNote error={error} />
            {data && <MarkdownView blocks={blocks} query={query} current={total ? current : -1} jump={jump} />}
          </div>
        </div>
      </div>
    </div>
  );
}
