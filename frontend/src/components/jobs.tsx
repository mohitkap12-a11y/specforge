import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { Job } from "../types";
import { useAuth } from "./auth";

/** A job the bell shows: `requested` when this browser asked for it (not adopted from the server's list). */
export interface TrackedJob extends Job { startedAt: number; label: string; requested: boolean }
interface Tracker {
  track: (jobId: number, label: string, onDone?: (job: Job) => void) => void;
  jobs: TrackedJob[];        // newest first: running and queued ones, then those that finished since the page was opened
  clearFinished: () => void;
}
const Ctx = createContext<Tracker>({ track: () => {}, jobs: [], clearFinished: () => {} });
export const useJobs = () => useContext(Ctx);
export const isActive = (j: Job) => j.state === "queued" || j.state === "running";

const FINAL = new Set(["done", "failed", "cancelled"]);
const ADOPT_EVERY_MS = 5000;
/** What the toast layer is told about a job this user asked for. */
export type Notice = { kind: "queued" | "joined" | "finished"; job: Job; label: string };
let noticeListener: ((n: Notice) => void) | null = null;
/** The toast layer registers here (kept outside React state to avoid a provider cycle). */
export const onJobNotice = (fn: ((n: Notice) => void) | null) => { noticeListener = fn; };

interface Entry { job?: Job; label: string; startedAt: number; requested: boolean; announced: boolean }

/** Follows jobs by polling (cheap, works through any proxy) and refreshes cached data when one finishes. The server's
 *  list of queued and running jobs (everyone's for an admin, the user's own and those they joined otherwise) is
 *  re-read every few seconds, so work started elsewhere (another tab, another machine, the CLI) shows up too. */
export function JobsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [jobs, setJobs] = useState<Record<number, Entry>>({});
  const callbacks = useRef(new Map<number, (j: Job) => void>());
  const entries = useRef(jobs);
  entries.current = jobs;

  const track = useCallback((id: number, label: string, onDone?: (j: Job) => void) => {
    if (onDone) callbacks.current.set(id, onDone);
    setJobs((j) => (j[id] ? { ...j, [id]: { ...j[id], requested: true } } : { ...j, [id]: { label, startedAt: Date.now(), requested: true, announced: false } }));
  }, []);

  useEffect(() => {   // adopt jobs this page did not start (page refresh, another machine, the CLI)
    let stop = false;
    const adopt = () => api.activeJobs().then((list) => {
      if (stop) return;
      setJobs((j) => {
        const next = { ...j };
        for (const job of list) {
          const e = next[job.id];
          next[job.id] = e ? { ...e, job: e.job && FINAL.has(e.job.state) ? e.job : job }
            : { job, label: job.label ?? job.type, startedAt: Date.parse(job.created ? `${job.created.replace(" ", "T")}Z` : "") || Date.now(), requested: false, announced: true };
        }
        return next;
      });
    }).catch(() => {});
    adopt();
    const h = setInterval(() => { if (document.visibilityState !== "hidden") adopt(); }, ADOPT_EVERY_MS);
    return () => { stop = true; clearInterval(h); };
  }, []);

  const pending = Object.entries(jobs).filter(([, v]) => !v.job || !FINAL.has(v.job.state)).map(([k]) => Number(k));
  const pendingKey = pending.join(",");
  useEffect(() => {
    if (!pending.length) return;
    let stop = false;
    const tick = async () => {
      for (const id of pending) {
        try {
          const job = await api.job(id);
          if (stop) return;
          const e = entries.current[id];
          const label = job.label ?? e?.label ?? job.type;
          if (e?.requested && !e.announced) {     // first look at a job this user just asked for: say where it stands
            if (job.user && job.user !== user.username) noticeListener?.({ kind: "joined", job, label });
            else if (job.state === "queued" && (job.ahead ?? 0) > 0) noticeListener?.({ kind: "queued", job, label });
          }
          setJobs((j) => ({ ...j, [id]: { ...(j[id] ?? { label, startedAt: Date.now(), requested: false }), job, announced: true } }));
          if (FINAL.has(job.state)) {
            callbacks.current.get(id)?.(job);
            callbacks.current.delete(id);
            if (e?.requested || job.mine) noticeListener?.({ kind: "finished", job, label });   // an admin is not told about everyone's
            qc.invalidateQueries();
          }
        } catch { /* transient: retry on the next tick */ }
      }
    };
    tick();
    const h = setInterval(tick, 1000);
    return () => { stop = true; clearInterval(h); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey, qc]);

  const clearFinished = useCallback(() => setJobs((j) => Object.fromEntries(Object.entries(j).filter(([, v]) => !v.job || !FINAL.has(v.job.state)))), []);

  const value = useMemo<Tracker>(() => ({
    track, clearFinished,
    jobs: Object.values(jobs).flatMap((v) => (v.job ? [{ ...v.job, label: v.job.label ?? v.label, startedAt: v.startedAt, requested: v.requested }] : []))
      .sort((a, b) => b.id - a.id),
  }), [jobs, track, clearFinished]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** "3rd in line, behind Convert Base.pdf (ann)" */
export function queueText(j: Pick<Job, "state" | "ahead" | "ahead_of">): string {
  const n = j.ahead ?? 0;
  if (j.state !== "queued") return "";
  if (n === 0) return "Next in line";
  const behind = j.ahead_of ? `, behind ${j.ahead_of.label}${j.ahead_of.user ? ` (${j.ahead_of.user})` : ""}` : "";
  return `${n} ahead of it in the queue${behind}`;
}

/** Bridges job notices to toasts. */
export function JobToasts({ push }: { push: (t: { title: string; description?: string; tone?: "ok" | "bad" | "info"; to?: string; toLabel?: string }) => void }) {
  useEffect(() => {
    onJobNotice(({ kind, job, label }) => {
      if (kind === "joined") push({ title: `${label}: already requested by ${job.user}`, description: "Nothing new was queued: you follow that request and are told when it is done.", tone: "info" });
      else if (kind === "queued") push({ title: `${label} is queued on the server`, description: `The server works first come, first served: ${queueText(job)}. It keeps going if you leave the page; follow it from the bell.`, tone: "info" });
      else if (job.state === "done") push({ title: `${label} finished`, tone: "ok", to: `/activity?job=${job.id}`, toLabel: "View logs" });
      else if (job.state === "failed") push({ title: `${label} failed`, description: (job.error ?? "").split("\n")[0], tone: "bad", to: `/activity?job=${job.id}`, toLabel: "View logs" });
      else push({ title: `${label} cancelled`, tone: "ok" });
    });
    return () => onJobNotice(null);
  }, [push]);
  return null;
}
