import { useCallback, useEffect, useState } from "react";

export type ThemePref = "system" | "light" | "dark";
const KEY = "specforge.theme";

function read(): ThemePref {
  try { const v = localStorage.getItem(KEY); return v === "light" || v === "dark" ? v : "system"; } catch { return "system"; }
}
function apply(pref: ThemePref) {
  const el = document.documentElement;
  if (pref === "system") el.removeAttribute("data-theme"); else el.setAttribute("data-theme", pref);
}
apply(read());   // before first paint, so there is no flash of the wrong theme

/** Theme preference persisted per browser; "system" follows the OS. */
export function useTheme(): [ThemePref, (t: ThemePref) => void] {
  const [pref, setPref] = useState<ThemePref>(read);
  useEffect(() => { apply(pref); }, [pref]);
  const set = useCallback((t: ThemePref) => {
    setPref(t);
    try { if (t === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, t); } catch { /* ignore */ }
    window.dispatchEvent(new CustomEvent("specforge-theme", { detail: t }));
  }, []);
  useEffect(() => {
    const on = (e: Event) => setPref((e as CustomEvent<ThemePref>).detail);     // keep every hook instance in sync
    window.addEventListener("specforge-theme", on);
    return () => window.removeEventListener("specforge-theme", on);
  }, []);
  return [pref, set];
}
