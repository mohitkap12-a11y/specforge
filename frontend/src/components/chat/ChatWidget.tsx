import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Maximize2, MessageSquare, Minus, SquarePen, X } from "lucide-react";
import { api } from "../../api";
import { useStoredState } from "../../hooks";
import { ChatPanel } from "./ChatPanel";

/** The floating chat: a button at the bottom right that opens a small window. Expand opens the full Chatbot page on the same chat. */
export function ChatWidget() {
  const loc = useLocation();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [sid, setSid] = useStoredState<number | null>("specforge.chat.mini", null);
  const cfg = useQuery({ queryKey: ["chat-config"], queryFn: api.chatConfig, staleTime: 30_000 });
  const onSession = useCallback((id: number) => setSid(id), [setSid]);
  useEffect(() => {
    if (!open) return;
    const on = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [open]);
  // the full page has its own chat; a chat deleted there must not stay open here
  const exists = useQuery({ queryKey: ["chat", sid], queryFn: () => api.chatSession(sid!), enabled: sid !== null && open, retry: false });
  useEffect(() => { if (exists.isError) setSid(null); }, [exists.isError, setSid]);

  if (loc.pathname.startsWith("/chat") || cfg.data?.enabled === false) return null;
  return (
    <>
      {open && (
        <section className="chat-mini" role="dialog" aria-label="Chat assistant">
          <header className="chat-mini-head">
            <strong className="grow">Assistant</strong>
            <button className="iconbtn" aria-label="New chat" title="New chat" onClick={() => setSid(null)}><SquarePen size={15} /></button>
            <button className="iconbtn" aria-label="Expand to full page" title="Open full page" onClick={() => { setOpen(false); nav(sid ? `/chat?session=${sid}` : "/chat"); }}><Maximize2 size={15} /></button>
            <button className="iconbtn" aria-label="Minimise" title="Minimise" onClick={() => setOpen(false)}><Minus size={16} /></button>
          </header>
          <ChatPanel sessionId={sid} onSession={onSession} compact autoFocus />
        </section>
      )}
      <button className={`chat-fab ${open ? "open" : ""}`} aria-label={open ? "Close chat" : "Open chat assistant"} aria-expanded={open} title="Ask the assistant" onClick={() => setOpen((o) => !o)}>
        {open ? <X size={20} /> : <MessageSquare size={20} />}
      </button>
    </>
  );
}
