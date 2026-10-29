import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { LucideIcon } from "lucide-react";
import { CornerDownLeft, Moon, Monitor, Search, Sun, UserRound } from "lucide-react";
import { NAV, navFor } from "./nav";
import { useAuth } from "./auth";
import { useTheme } from "../theme";

interface Cmd { id: string; group: string; label: string; hint?: string; icon: LucideIcon; run: () => void; keywords?: string }

/** ⌘K / Ctrl+K launcher: jump to any page, open common settings, switch theme. */
export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const [, setTheme] = useTheme();
  const [q, setQ] = useState("");
  const [i, setI] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const go = (to: string) => () => nav(to);
  const { canManageUsers } = useAuth();
  const cmds = useMemo<Cmd[]>(() => [
    ...navFor(canManageUsers).map((n) => ({ id: n.to, group: "Go to", label: n.label, hint: n.desc, icon: n.icon, run: go(n.to), keywords: n.desc })),
    { id: "s-ai", group: "Settings", label: "AI providers & models", icon: NAV.find((n) => n.to === "/settings")!.icon, run: go("/settings#ai"), keywords: "ollama opencode model llm" },
    { id: "s-log", group: "Settings", label: "Logging options", icon: NAV.find((n) => n.to === "/settings")!.icon, run: go("/settings#logging"), keywords: "prompt capture level" },
    { id: "s-mi", group: "Settings", label: "NVMe-MI message layout", icon: NAV.find((n) => n.to === "/settings")!.icon, run: go("/settings#mi"), keywords: "mi layout overrides" },
    { id: "a-err", group: "Activity", label: "Show errors in the activity log", icon: NAV.find((n) => n.to === "/activity")!.icon, run: go("/activity?level=error"), keywords: "failures problems" },
    { id: "a-ai", group: "Activity", label: "Watch the AI agents", icon: NAV.find((n) => n.to === "/activity")!.icon, run: go("/activity?lens=ai"), keywords: "llm agent prompts" },
    { id: "acct", group: "Account", label: "My account", hint: "Change password, sign out", icon: UserRound, run: go("/account"), keywords: "password logout sign out user" },
    { id: "t-l", group: "Theme", label: "Light theme", icon: Sun, run: () => setTheme("light") },
    { id: "t-d", group: "Theme", label: "Dark theme", icon: Moon, run: () => setTheme("dark") },
    { id: "t-s", group: "Theme", label: "Match system theme", icon: Monitor, run: () => setTheme("system") },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [canManageUsers]);
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return t ? cmds.filter((c) => `${c.label} ${c.group} ${c.keywords ?? ""}`.toLowerCase().includes(t)) : cmds;
  }, [q, cmds]);
  useEffect(() => { if (open) { setQ(""); setI(0); setTimeout(() => input.current?.focus(), 0); } }, [open]);
  useEffect(() => { setI(0); }, [q]);
  if (!open) return null;
  const choose = (c?: Cmd) => { if (c) { onClose(); c.run(); } };
  let last = "";
  return (
    <div className="palette-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette">
        <input ref={input} value={q} placeholder="Search pages and actions…" aria-label="Search" onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setI((x) => Math.min(shown.length - 1, x + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setI((x) => Math.max(0, x - 1)); }
            else if (e.key === "Enter") { e.preventDefault(); choose(shown[i]); }
            else if (e.key === "Escape") onClose();
          }} />
        <ul role="listbox">
          {shown.length === 0 && <li className="grp">No results</li>}
          {shown.map((c, idx) => {
            const head = c.group !== last ? <li key={`g-${c.group}`} className="grp">{c.group}</li> : null;
            last = c.group;
            const Icon = c.icon;
            return [head, (
              <li key={c.id} role="option" aria-selected={idx === i} className="opt" onMouseMove={() => setI(idx)} onClick={() => choose(c)}
                ref={(el) => { if (el && idx === i) el.scrollIntoView({ block: "nearest" }); }}>
                <Icon size={16} /><span>{c.label}</span>{c.hint && <span className="muted small grow">{c.hint}</span>}
              </li>
            )];
          })}
        </ul>
        <div className="foot"><span><span className="kbd">↑↓</span> navigate</span><span><span className="kbd"><CornerDownLeft size={10} style={{ verticalAlign: "-1px" }} /></span> open</span><span><span className="kbd">esc</span> close</span></div>
      </div>
    </div>
  );
}

export const SearchHint = () => <Search size={15} />;
