import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { CheckCircle2, X, XCircle } from "lucide-react";
import { api } from "../api";
import { useJobs } from "./jobs";

export interface ToastInput { title: string; description?: string; tone?: "ok" | "bad" | "info"; to?: string; toLabel?: string }
interface Item extends ToastInput { id: number }
const Ctx = createContext<{ push: (t: ToastInput) => void }>({ push: () => {} });
export const useToast = () => useContext(Ctx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Item[]>([]);
  const seq = useRef(0);
  const dismiss = useCallback((id: number) => setItems((l) => l.filter((x) => x.id !== id)), []);
  const push = useCallback((t: ToastInput) => {
    const id = ++seq.current;
    setItems((l) => [...l.slice(-3), { ...t, id }]);
    setTimeout(() => dismiss(id), t.tone === "bad" ? 9000 : 5500);
  }, [dismiss]);
  const value = useMemo(() => ({ push }), [push]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <div className="toasts" aria-live="polite">
        <RunningJobs />
        {items.map((t) => (
          <div key={t.id} className={`tcard ${t.tone === "bad" ? "bad" : t.tone === "ok" ? "ok" : ""}`} role="status">
            <div className="t-title">
              {t.tone === "bad" ? <XCircle size={16} color="var(--bad)" /> : <CheckCircle2 size={16} color="var(--ok)" />}
              <span className="grow">{t.title}</span>
              <button className="ghost sm" aria-label="Dismiss" onClick={() => dismiss(t.id)}><X size={14} /></button>
            </div>
            {t.description && <div className="t-body">{t.description}</div>}
            {t.to && <Link className="small" to={t.to} onClick={() => dismiss(t.id)}>{t.toLabel ?? "View"} →</Link>}
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}

const elapsed = (since: number) => {
  const s = Math.max(0, Math.round((Date.now() - since) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};

/** Floating cards for jobs that are running right now: progress, current step, link to their logs, cancel. */
function RunningJobs() {
  const { active } = useJobs();
  const onActivity = useLocation().pathname.startsWith("/activity");     // that page already shows running jobs in full
  const running = onActivity ? [] : active.filter((j) => j.state === "running" || j.state === "queued");
  const [, tick] = useState(0);
  useEffect(() => { if (!running.length) return; const h = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(h); }, [running.length]);
  if (!running.length) return null;
  return (
    <>
      {running.slice(0, 3).map((j) => (
        <div key={j.id} className="tcard run" role="status">
          <div className="t-title"><span className="spinner" /><span className="grow">{j.type}</span><span className="muted small">{j.startedAt ? elapsed(j.startedAt) : ""}</span></div>
          <div className="t-body">{j.state === "queued" ? "Queued…" : j.message || "Working…"}</div>
          <div className={`progress ${j.total > 0 ? "" : "indeterminate"}`}><span style={{ width: j.total > 0 ? `${Math.min(100, (100 * j.progress) / j.total)}%` : undefined }} /></div>
          <div className="row">
            <Link className="small" to={`/activity?job=${j.id}`}>View logs →</Link><span className="grow" />
            <button className="sm danger" onClick={() => api.cancelJob(j.id)}>Cancel</button>
          </div>
        </div>
      ))}
    </>
  );
}
