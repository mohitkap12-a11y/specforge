import { useCallback, useEffect, useState } from "react";

export const SET_KEY = "specforge.set";
/** Make `id` the selected spec set for pages that restore it on mount (guarded: storage can be unavailable). */
export function rememberSet(id: number) {
  try { localStorage.setItem(SET_KEY, JSON.stringify(id)); } catch { /* ignore */ }
}

/** useState mirrored to localStorage; every storage access is guarded (private windows, blocked storage). */
export function useStoredState<T>(key: string, initial: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try { const s = localStorage.getItem(key); return s === null ? initial : (JSON.parse(s) as T); } catch { return initial; }
  });
  const set = useCallback((x: T) => { setV(x); try { localStorage.setItem(key, JSON.stringify(x)); } catch { /* ignore */ } }, [key]);
  return [v, set];
}

export function useDebounced<T>(value: T, ms = 300): T {
  const [d, setD] = useState(value);
  useEffect(() => { const h = setTimeout(() => setD(value), ms); return () => clearTimeout(h); }, [value, ms]);
  return d;
}
