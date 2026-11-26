import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowDown, ArrowUp, BookOpen, Check, Copy, RefreshCw, Sparkles, Square, ThumbsDown, ThumbsUp, X } from "lucide-react";
import { api } from "../../api";
import { copyText } from "../../clipboard";
import { useAuth } from "../auth";
import type { ChatMessage, ChatSource } from "../../types";
import { ChatMarkdown } from "./ChatMarkdown";
import { useChat } from "./useChat";

const STARTERS = [
  "What does the Identify command return?",
  "Summarise the Namespace Management flow",
  "List the fields of the Submission Queue Entry",
  "Write a Python snippet that parses an NVMe Identify Controller response",
];

function Sources({ sources }: { sources: ChatSource[] }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!sources.length) return null;
  const cur = sources.find((s) => s.n === open);
  return (
    <div className="chat-sources">
      <div className="chat-src-row">
        <BookOpen size={12} aria-hidden="true" /><span className="muted small">Sources</span>
        {sources.map((s) => (
          <button key={s.n} className={`chat-src ${open === s.n ? "on" : ""}`} onClick={() => setOpen(open === s.n ? null : s.n)} title={`${s.title}${s.heading ? " › " + s.heading : ""}`}>
            <b>{s.n}</b> {s.heading ? s.heading.split(" › ").pop() : s.title}
          </button>
        ))}
      </div>
      {cur && (
        <div className="chat-src-body">
          <div className="row"><strong className="grow small">{cur.title}{cur.heading ? ` › ${cur.heading}` : ""}</strong><button className="sm" aria-label="Close source" onClick={() => setOpen(null)}><X size={12} /></button></div>
          <pre>{cur.text}</pre>
        </div>
      )}
    </div>
  );
}

function Bubble({ m, last, busy, onSuggest, onRegenerate }: { m: ChatMessage; last: boolean; busy: boolean; onSuggest: (q: string) => void; onRegenerate: () => void }) {
  const [copied, setCopied] = useState(false);
  const [fb, setFb] = useState<number | null>(m.feedback);
  useEffect(() => setFb(m.feedback), [m.feedback]);
  if (m.role === "user") return <div className="chat-row user"><div className="chat-bubble">{m.content}</div></div>;
  const vote = (v: 1 | -1) => { const next = fb === v ? null : v; setFb(next); void api.chatFeedback(m.id, next); };
  return (
    <div className="chat-row assistant">
      <span className="chat-avatar" aria-hidden="true"><Sparkles size={13} /></span>
      <div className="chat-body">
        <ChatMarkdown text={m.content} />
        <Sources sources={m.sources} />
        <div className="chat-actions">
          <button className="sm" aria-label="Copy answer" title="Copy" onClick={() => copyText(m.content).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => undefined)}>{copied ? <Check size={13} /> : <Copy size={13} />}</button>
          <button className="sm" aria-label="Good answer" aria-pressed={fb === 1} title="Good answer" onClick={() => vote(1)}><ThumbsUp size={13} fill={fb === 1 ? "currentColor" : "none"} /></button>
          <button className="sm" aria-label="Bad answer" aria-pressed={fb === -1} title="Bad answer" onClick={() => vote(-1)}><ThumbsDown size={13} fill={fb === -1 ? "currentColor" : "none"} /></button>
          {last && !busy && <button className="sm" aria-label="Regenerate" title="Regenerate" onClick={onRegenerate}><RefreshCw size={13} /></button>}
          {m.stats && <span className="muted small chat-stats">{m.stats.stopped ? "stopped · " : ""}{m.stats.tps ? `${m.stats.tps} tok/s · ` : ""}{m.stats.seconds ? `${m.stats.seconds}s` : ""}{m.stats.model ? ` · ${m.stats.model}` : ""}</span>}
        </div>
        {last && !busy && m.suggestions.length > 0 && (
          <div className="chat-suggest" aria-label="Suggested follow-up questions">
            {m.suggestions.map((q) => <button key={q} onClick={() => onSuggest(q)}>{q}</button>)}
          </div>
        )}
      </div>
    </div>
  );
}

/** The conversation view shared by the floating window and the full page: messages, streaming answer, suggestions and the composer. */
export function ChatPanel({ sessionId, onSession, compact, autoFocus }: { sessionId: number | null; onSession?: (id: number) => void; compact?: boolean; autoFocus?: boolean }) {
  const { user } = useAuth();
  const cfg = useQuery({ queryKey: ["chat-config"], queryFn: api.chatConfig, staleTime: 30_000 });
  const chat = useChat(sessionId, onSession);
  const [text, setText] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const [pinned, setPinned] = useState(true);       // following the bottom while the answer streams
  const msgs = chat.session?.messages ?? [];
  // a regenerated answer replaces the last question and answer, which the server removes when it starts
  const history = chat.live?.regen && msgs.length >= 2 ? msgs.slice(0, -2) : msgs;

  useEffect(() => { if (autoFocus) input.current?.focus(); }, [autoFocus, sessionId]);
  useEffect(() => { setPinned(true); }, [sessionId]);
  useEffect(() => { const el = box.current; if (el && pinned) el.scrollTop = el.scrollHeight; }, [history.length, chat.live?.answer, chat.live?.started, pinned, chat.error]);
  useEffect(() => { const el = input.current; if (el) { el.style.height = "auto"; el.style.height = `${Math.min(el.scrollHeight, 180)}px`; } }, [text]);

  const submit = (t = text) => { if (!t.trim() || chat.busy) return; chat.send(t); setText(""); setPinned(true); };
  const lastIdx = history.length - 1;
  const off = cfg.data && (!cfg.data.enabled ? "The chatbot is turned off. An admin can turn it on in Settings → Chatbot." : !cfg.data.configured ? "No chat model is set yet. An admin can choose one in Settings → Chatbot." : null);
  const empty = !chat.loading && history.length === 0 && !chat.live;

  return (
    <div className={`chat-panel ${compact ? "compact" : ""}`}>
      <div className="chat-scroll" ref={box} onScroll={(e) => { const el = e.currentTarget; setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 60); }} role="log" aria-live="polite" aria-label="Conversation">
        <div className="chat-inner">
          {empty && (
            <div className="chat-empty">
              <span className="chat-logo"><Sparkles size={20} /></span>
              <h2>Hi {user.display_name || user.username}, ask about your documents</h2>
              <p className="muted">{cfg.data ? `Answers come from ${cfg.data.docs} document${cfg.data.docs === 1 ? "" : "s"} in the knowledge base, with sources.` : "Answers come from the knowledge base, with sources."}</p>
              {cfg.data?.docs === 0 && <p className="muted small">The knowledge base is empty. <Link to="/knowledge">Add documents</Link> first.</p>}
              <div className="chat-suggest start">{STARTERS.slice(0, compact ? 3 : 4).map((q) => <button key={q} disabled={!!off} onClick={() => submit(q)}>{q}</button>)}</div>
            </div>
          )}
          {history.map((m, i) => <Bubble key={m.id} m={m} last={i === lastIdx} busy={chat.busy} onSuggest={submit} onRegenerate={chat.regenerate} />)}
          {chat.live && (
            <>
              <div className="chat-row user"><div className="chat-bubble">{chat.live.question}</div></div>
              <div className="chat-row assistant">
                <span className="chat-avatar" aria-hidden="true"><Sparkles size={13} /></span>
                <div className="chat-body">
                  {chat.live.answer ? <ChatMarkdown text={chat.live.answer} streaming /> : <div className="chat-typing" aria-label="Thinking"><i /><i /><i /><span className="muted small">{chat.live.started ? `Reading ${chat.live.sources.length} excerpt${chat.live.sources.length === 1 ? "" : "s"}…` : "Searching the knowledge base…"}</span></div>}
                </div>
              </div>
            </>
          )}
          {chat.error && (
            <div className="chat-error" role="alert">
              <span className="grow">{chat.error}</span>
              <button className="sm" onClick={chat.dismissError} aria-label="Dismiss"><X size={12} /></button>
            </div>
          )}
        </div>
      </div>
      {!pinned && <button className="chat-down" onClick={() => setPinned(true)} aria-label="Scroll to latest"><ArrowDown size={14} /></button>}
      <div className="chat-composer">
        {off && <div className="chat-off small">{off}</div>}
        <div className="chat-box">
          <textarea ref={input} rows={1} value={text} placeholder={compact ? "Ask a question…" : "Ask about a command, field or procedure…  (Shift+Enter for a new line)"} aria-label="Message" disabled={!!off}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
          {chat.busy
            ? <button className="chat-send stop" onClick={chat.stop} aria-label="Stop generating" title="Stop"><Square size={13} fill="currentColor" /></button>
            : <button className="chat-send" disabled={!text.trim() || !!off} onClick={() => submit()} aria-label="Send" title="Send"><ArrowUp size={16} /></button>}
        </div>
        {!compact && <div className="muted small chat-foot">Answers can be wrong: check the sources, especially bit positions and values.</div>}
      </div>
    </div>
  );
}
