import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, chatStream } from "../../api";
import type { ChatEvent, ChatMessage, ChatSource } from "../../types";

export interface Live { question: string; answer: string; sources: ChatSource[]; started: boolean; regen: boolean }

/** One conversation: its saved messages plus the answer being streamed. `onSession` is told the id of a chat created by the first question. */
export function useChat(sessionId: number | null, onSession?: (id: number) => void) {
  const qc = useQueryClient();
  const session = useQuery({ queryKey: ["chat", sessionId], queryFn: () => api.chatSession(sessionId!), enabled: sessionId !== null });
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const sid = useRef(sessionId);
  sid.current = sessionId;
  const created = useRef<number | null>(null);      // the chat this hook made for its first question: not a switch
  useEffect(() => {
    setError(null);
    if (sessionId !== created.current) { abort.current?.abort(); abort.current = null; setLive(null); }   // another conversation: drop the old stream
  }, [sessionId]);
  useEffect(() => () => abort.current?.abort(), []);

  const run = useCallback(async (body: { text?: string; regenerate?: boolean }, question: string) => {
    setError(null);
    abort.current?.abort();
    const ctl = new AbortController();
    abort.current = ctl;
    setLive({ question, answer: "", sources: [], started: false, regen: body.regenerate === true });
    let id = sid.current;
    try {
      if (id === null) {
        id = (await api.chatNew()).id;
        sid.current = id;
        created.current = id;
        qc.setQueryData(["chat", id], { id, title: "New chat", pinned: 0, created: "", updated: "", messages: [] });
        onSession?.(id);
      }
      await chatStream(id, body, (e: ChatEvent) => {
        if (e.type === "start") setLive((l) => l && { ...l, sources: e.sources, started: true });
        else if (e.type === "delta") setLive((l) => l && { ...l, answer: l.answer + e.text });
        else if (e.type === "error") setError(e.message);
        else if (e.type === "suggestions") {
          qc.setQueryData(["chat", id], (old: { messages: ChatMessage[] } | undefined) =>
            old && { ...old, messages: old.messages.map((m) => (m.id === e.message_id ? { ...m, suggestions: e.items } : m)) });
        }
      }, ctl.signal);
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (abort.current === ctl) { abort.current = null; }
      if (id !== null) await qc.invalidateQueries({ queryKey: ["chat", id] });
      void qc.invalidateQueries({ queryKey: ["chat-sessions"] });
      setLive((l) => (abort.current === null ? null : l));
    }
  }, [qc, onSession]);

  const send = useCallback((text: string) => { const t = text.trim(); if (t) void run({ text: t }, t); }, [run]);
  const regenerate = useCallback(() => {
    const last = [...(session.data?.messages ?? [])].reverse().find((m) => m.role === "user");
    if (last) void run({ regenerate: true }, last.content);
  }, [run, session.data]);
  const stop = useCallback(() => abort.current?.abort(), []);
  return { session: session.data, loading: session.isLoading && sessionId !== null, live, error, send, regenerate, stop, busy: live !== null, dismissError: () => setError(null) };
}
