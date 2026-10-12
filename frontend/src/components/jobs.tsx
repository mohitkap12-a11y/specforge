import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { Job } from "../types";

export interface TrackedJob extends Job { startedAt?: number }
interface Tracker { track: (jobId: number, label: string, onDone?: (job: Job) => void) => void; active: TrackedJob[] }
const Ctx = createContext<Tracker>({ track: () => {}, active: [] });
export const useJobs = () => useContext(Ctx);

const FINAL = new Set(["done", "failed", "cancelled"]);
type Listener = (job: Job, label: string) => void;
let finishListener: Listener | null = null;
/** The toast layer registers here to be told when a tracked job finishes (kept outside React state to avoid a provider cycle). */
export const onJobFinished = (fn: Listener | null) => { finishListener = fn; };

/** Follows submitted jobs by polling (cheap, works through any proxy) and refreshes cached data when one finishes.
 *  Jobs already running when the page loads are adopted, so a refresh does not lose sight of them. */
export function JobsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [jobs, setJobs] = useState<Record<number, { job?: Job; label: string; startedAt: number }>>({});
  const callbacks = useRef(new Map<number, (j: Job) => void>());

  const track = useCallback((id: number, label: string, onDone?: (j: Job) => void) => {
    if (onDone) callbacks.current.set(id, onDone);
    setJobs((j) => (j[id] ? j : { ...j, [id]: { label, startedAt: Date.now() } }));
  }, []);

  useEffect(() => {   // adopt jobs that were already running (page refresh, started from the CLI/API)
    api.jobs().then((list) => list.filter((j) => j.state === "running" || j.state === "queued").forEach((j) => track(j.id, j.type))).catch(() => {});
  }, [track]);

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
          setJobs((j) => ({ ...j, [id]: { ...(j[id] ?? { label: job.type, startedAt: Date.now() }), job } }));
          if (FINAL.has(job.state)) {
            callbacks.current.get(id)?.(job);
            callbacks.current.delete(id);
            finishListener?.(job, jobs[id]?.label ?? job.type);
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

  const value = useMemo<Tracker>(() => ({
    track,
    active: Object.values(jobs).flatMap((v) => (v.job ? [{ ...v.job, type: v.label, startedAt: v.startedAt }] : [])),
  }), [jobs, track]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Bridges job completion to toasts. */
export function JobToasts({ push }: { push: (t: { title: string; description?: string; tone?: "ok" | "bad"; to?: string; toLabel?: string }) => void }) {
  useEffect(() => {
    onJobFinished((job, label) => {
      if (job.state === "done") push({ title: `${label} finished`, tone: "ok", to: `/activity?job=${job.id}`, toLabel: "View logs" });
      else if (job.state === "failed") push({ title: `${label} failed`, description: (job.error ?? "").split("\n")[0], tone: "bad", to: `/activity?job=${job.id}`, toLabel: "View logs" });
      else push({ title: `${label} cancelled`, tone: "ok" });
    });
    return () => onJobFinished(null);
  }, [push]);
  return null;
}
