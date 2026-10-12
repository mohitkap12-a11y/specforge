import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { Job } from "../types";

interface Tracker { track: (jobId: number, label: string, onDone?: (job: Job) => void) => void; active: Job[] }
const Ctx = createContext<Tracker>({ track: () => {}, active: [] });
export const useJobs = () => useContext(Ctx);

const FINAL = new Set(["done", "failed", "cancelled"]);

/** Follows submitted jobs by polling (cheap, works through any proxy) and refreshes cached data when one finishes. */
export function JobsProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [jobs, setJobs] = useState<Record<number, { job?: Job; label: string }>>({});
  const callbacks = useRef(new Map<number, (j: Job) => void>());

  const track = useCallback((id: number, label: string, onDone?: (j: Job) => void) => {
    if (onDone) callbacks.current.set(id, onDone);
    setJobs((j) => ({ ...j, [id]: { label } }));
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
          setJobs((j) => ({ ...j, [id]: { label: j[id]?.label ?? job.type, job } }));
          if (FINAL.has(job.state)) {
            callbacks.current.get(id)?.(job);
            callbacks.current.delete(id);
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
    active: Object.values(jobs).flatMap((v) => (v.job ? [{ ...v.job, type: v.label }] : [])),
  }), [jobs, track]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function JobsTray() {
  const { active } = useJobs();
  const [dismissed, setDismissed] = useState<Set<number>>(new Set());
  const shown = active.filter((j) => !dismissed.has(j.id)).slice(-4);
  if (!shown.length) return null;
  return (
    <div>
      {shown.map((j) => {
        const running = !FINAL.has(j.state);
        return (
          <div key={j.id} className="jobs-pill" role="status">
            <div className="row">
              <strong className="grow">{j.type}</strong>
              {running ? <button className="danger small" onClick={() => api.cancelJob(j.id)}>Cancel</button>
                : <button className="small" onClick={() => setDismissed((d) => new Set(d).add(j.id))}>×</button>}
            </div>
            <div className={j.state === "failed" ? "" : "muted"}>{j.state === "failed" ? `Failed: ${(j.error ?? "").split("\n")[0]}` : j.state === "done" ? "Done" : j.state === "cancelled" ? "Cancelled" : j.message || j.state}</div>
            {running && j.total > 0 && <div className="progress"><span style={{ width: `${Math.min(100, (100 * j.progress) / j.total)}%` }} /></div>}
          </div>
        );
      })}
    </div>
  );
}
