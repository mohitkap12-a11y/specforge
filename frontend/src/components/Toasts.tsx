import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, Info, X, XCircle } from "lucide-react";

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
        {items.map((t) => (
          <div key={t.id} className={`tcard ${t.tone === "bad" ? "bad" : t.tone === "ok" ? "ok" : t.tone === "info" ? "run" : ""}`} role="status">
            <div className="t-title">
              {t.tone === "bad" ? <XCircle size={16} color="var(--bad)" /> : t.tone === "info" ? <Info size={16} color="var(--info)" /> : <CheckCircle2 size={16} color="var(--ok)" />}
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
