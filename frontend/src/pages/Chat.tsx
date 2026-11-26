import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, MessageSquarePlus, Pencil, Pin, PinOff, PanelLeft, Search, Trash2, X } from "lucide-react";
import { api } from "../api";
import { useDebounced } from "../hooks";
import { ChatPanel } from "../components/chat/ChatPanel";
import type { ChatSessionRow } from "../types";

const when = (ts: string) => {
  const d = new Date(ts.replace(" ", "T") + "Z");
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  return days <= 0 ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : days === 1 ? "Yesterday" : days < 7 ? d.toLocaleDateString([], { weekday: "long" }) : d.toLocaleDateString();
};
const group = (s: ChatSessionRow) => {
  if (s.pinned) return "Pinned";
  const days = Math.floor((Date.now() - new Date(s.updated.replace(" ", "T") + "Z").getTime()) / 86_400_000);
  return days <= 0 ? "Today" : days === 1 ? "Yesterday" : days < 8 ? "Previous 7 days" : "Older";
};

function Row({ s, active, onOpen }: { s: ChatSessionRow; active: boolean; onOpen: () => void }) {
  const qc = useQueryClient();
  const [edit, setEdit] = useState(false);
  const [title, setTitle] = useState(s.title);
  const refresh = () => qc.invalidateQueries({ queryKey: ["chat-sessions"] });
  const rename = useMutation({ mutationFn: () => api.chatRename(s.id, { title }), onSuccess: () => { setEdit(false); void refresh(); } });
  const pin = useMutation({ mutationFn: () => api.chatRename(s.id, { pinned: !s.pinned }), onSuccess: refresh });
  const del = useMutation({ mutationFn: () => api.chatDelete(s.id), onSuccess: () => { qc.removeQueries({ queryKey: ["chat", s.id] }); void refresh(); } });
  if (edit) {
    return (
      <div className="chat-item editing">
        <input autoFocus value={title} maxLength={120} aria-label="Chat title" onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && title.trim()) rename.mutate(); if (e.key === "Escape") setEdit(false); }} />
        <button className="sm" aria-label="Save title" disabled={!title.trim()} onClick={() => rename.mutate()}><Check size={13} /></button>
        <button className="sm" aria-label="Cancel" onClick={() => setEdit(false)}><X size={13} /></button>
      </div>
    );
  }
  return (
    <div className={`chat-item ${active ? "active" : ""}`}>
      <button className="chat-item-main" onClick={onOpen} title={s.title}>
        <span className="chat-item-title">{s.pinned ? <Pin size={11} aria-label="Pinned" /> : null} {s.title}</span>
        <span className="muted small">{when(s.updated)} · {s.messages} message{s.messages === 1 ? "" : "s"}</span>
      </button>
      <span className="chat-item-actions">
        <button className="sm" aria-label={s.pinned ? "Unpin" : "Pin"} title={s.pinned ? "Unpin" : "Pin"} onClick={() => pin.mutate()}>{s.pinned ? <PinOff size={13} /> : <Pin size={13} />}</button>
        <button className="sm" aria-label="Rename" title="Rename" onClick={() => { setTitle(s.title); setEdit(true); }}><Pencil size={13} /></button>
        <a className="sm iconlink" aria-label="Export as Markdown" title="Export as Markdown" href={api.chatExportUrl(s.id)} download><Download size={13} /></a>
        <button className="sm" aria-label="Delete chat" title="Delete" onClick={() => { if (confirm(`Delete “${s.title}”?`)) del.mutate(); }}><Trash2 size={13} /></button>
      </span>
    </div>
  );
}

export default function ChatPage() {
  const [params, setParams] = useSearchParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const sid = params.get("session") ? Number(params.get("session")) : null;
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 250);
  const [side, setSide] = useState(false);          // mobile drawer
  const sessions = useQuery({ queryKey: ["chat-sessions", dq], queryFn: () => api.chatSessions(dq) });
  const open = (id: number | null) => { setParams(id ? { session: String(id) } : {}, { replace: false }); setSide(false); };
  const clear = useMutation({ mutationFn: api.chatDeleteAll, onSuccess: () => { qc.removeQueries({ queryKey: ["chat"] }); void qc.invalidateQueries({ queryKey: ["chat-sessions"] }); open(null); } });
  // a chat deleted (here or in another tab) while open: back to a new chat
  const current = useQuery({ queryKey: ["chat", sid], queryFn: () => api.chatSession(sid!), enabled: sid !== null, retry: false });
  useEffect(() => { if (current.isError && sid !== null) nav("/chat", { replace: true }); }, [current.isError, sid, nav]);

  const order = ["Pinned", "Today", "Yesterday", "Previous 7 days", "Older"];
  const groups = order.map((g) => [g, (sessions.data ?? []).filter((s) => group(s) === g)] as const).filter(([, l]) => l.length);
  return (
    <div className="chat-page">
      {side && <div className="scrim" onClick={() => setSide(false)} />}
      <aside className={`chat-side ${side ? "open" : ""}`} aria-label="Chat history">
        <button className="primary chat-new" onClick={() => open(null)}><MessageSquarePlus size={15} /> New chat</button>
        <div className="md-search chat-search"><Search size={14} aria-hidden="true" /><input placeholder="Search chats" aria-label="Search chats" value={q} onChange={(e) => setQ(e.target.value)} />{q && <button className="iconbtn" aria-label="Clear search" onClick={() => setQ("")}><X size={13} /></button>}</div>
        <div className="chat-list">
          {sessions.isLoading && <div className="muted small pad">Loading…</div>}
          {sessions.data?.length === 0 && <div className="muted small pad">{dq ? "No chats match." : "No chats yet. Your conversations are saved here."}</div>}
          {groups.map(([g, list]) => (
            <div key={g}>
              <div className="chat-group">{g}</div>
              {list.map((s) => <Row key={s.id} s={s} active={s.id === sid} onOpen={() => open(s.id)} />)}
            </div>
          ))}
        </div>
        {(sessions.data?.length ?? 0) > 0 && !dq && <button className="chat-clear" disabled={clear.isPending} onClick={() => { if (confirm("Delete all your chats? This cannot be undone.")) clear.mutate(); }}><Trash2 size={13} /> Delete all chats</button>}
      </aside>
      <section className="chat-main">
        <div className="chat-main-head">
          <button className="iconbtn chat-side-btn" aria-label="Show chat history" onClick={() => setSide(true)}><PanelLeft size={17} /></button>
          <strong className="grow chat-main-title">{current.data?.title ?? "New chat"}</strong>
        </div>
        <ChatPanel sessionId={sid} onSession={(id) => setParams({ session: String(id) }, { replace: true })} autoFocus />
      </section>
    </div>
  );
}
