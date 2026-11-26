import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, Info, Layers, LogOut, Menu, Moon, Search, Sun, UserRound } from "lucide-react";
import { api } from "../api";
import { isActive, useJobs } from "./jobs";
import { Bell } from "./Bell";
import { CommandPalette } from "./CommandPalette";
import { COMING_SOON, GROUPS, NAV, isCoveragePage, navFor, type NavItem } from "./nav";
import { useAuth } from "./auth";
import { useTheme } from "../theme";

const KEY = "specforge.nav.collapsed";
function readCollapsed() { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } }

export function Shell({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [menu, setMenu] = useState(false);           // mobile drawer
  const [palette, setPalette] = useState(false);
  const [theme, setTheme] = useTheme();
  const { jobs } = useJobs();
  const { me, user, isAdmin, canManageUsers, coverage, signOut } = useAuth();
  const pages = navFor(canManageUsers, coverage);
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, staleTime: 60_000 });
  const running = jobs.filter(isActive).length;
  const here = (n: NavItem) => (n.to === "/" ? loc.pathname === "/" : loc.pathname.startsWith(n.to));
  // a Coverage Tool page opened while it is coming soon: the placeholder (the pages themselves are not in the menu)
  const current = coverage === "soon" && isCoveragePage(loc.pathname) ? COMING_SOON : pages.find(here) ?? NAV.find(here);
  const tool = current?.soon ? null : GROUPS.find((g) => g.key === current?.group)?.title ?? null;

  const toggle = useCallback(() => setCollapsed((c) => {
    try { localStorage.setItem(KEY, c ? "0" : "1"); } catch { /* ignore */ }
    return !c;
  }), []);
  useEffect(() => { setMenu(false); }, [loc.pathname]);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); setPalette((p) => !p); }
      else if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); toggle(); }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [toggle]);

  const dark = theme === "dark" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  const tip = (label: string) => (collapsed ? label : undefined);   // native tooltip only while the labels are hidden
  const link = (n: NavItem) => (
    <NavLink key={n.to} to={n.to} end={n.to === "/"} title={tip(n.soon ? `${n.label} (coming soon)` : n.label)} aria-label={n.soon ? `${n.label} (coming soon)` : n.label}
      className={({ isActive }) => `item ${n.soon ? "soon" : ""} ${isActive || (n.soon && current === COMING_SOON) ? "active" : ""}`}>
      <n.icon size={17} /><span className="lbl">{n.label}</span>
      {n.soon && <span className="soon-tag lbl">Soon</span>}
      {n.to === "/activity" && running > 0 && <span className="count" aria-label={`${running} running`}>{running}</span>}
      {n.to === "/users" && me.pending > 0 && <span className="count" aria-label={`${me.pending} access requests waiting`}>{me.pending}</span>}
    </NavLink>
  );

  return (
    <div className={`shell ${collapsed ? "collapsed" : ""}`}>
      {menu && <div className="scrim" onClick={() => setMenu(false)} />}
      <aside className={`nav-wrap ${menu ? "open" : ""}`}>
        <nav className="nav" aria-label="Main">
          <Link to="/" className="brand" title={tip("SpecForge")} aria-label="SpecForge home">
            <span className="brand-mark" aria-hidden="true"><Layers size={15} /></span>
            <span>SpecForge<small>NVMe spec workbench</small></span>
          </Link>
          <button className="side-search" onClick={() => setPalette(true)} aria-label="Search pages and actions" title={tip("Search")}>
            <Search size={16} /><span className="lbl grow" style={{ textAlign: "left" }}>Search…</span><span className="kbd lbl">{isMac ? "⌘" : "Ctrl"} K</span>
          </button>
          {GROUPS.map((g) => (
            <div key={g.key}>
              {g.title ? <div className="nav-group"><span className="lbl">{g.title}</span>
                {g.key === "coverage" && coverage === "preview" && <span className="lbl preview-tag" title="Only admins see the Coverage Tool until it is released in Settings → Feature rollout">Admin preview</span>}
              </div> : <div style={{ height: 6 }} />}
              {pages.filter((n) => n.group === g.key).map(link)}
            </div>
          ))}
          <div className="spacer" />
          {pages.filter((n) => n.group === "system").map(link)}
          <div className="nav-foot" title={tip(health.data ? `v${health.data.version} · ${me.auth ? user.username : "local"}` : "")}>
            <span className={`dot ${health.data?.ok ? "ok" : health.isError ? "bad" : ""}`} />
            <span className="lbl">{health.data ? `v${health.data.version} · ${me.auth ? "shared server" : "running locally"}` : health.isError ? "API unreachable" : "connecting…"}</span>
          </div>
        </nav>
        <button className="nav-toggle" onClick={toggle} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed}
          title={`${collapsed ? "Expand" : "Collapse"} sidebar (${isMac ? "⌘" : "Ctrl"} B)`}>
          <ChevronLeft size={14} />
        </button>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <button className="iconbtn menu-btn" aria-label="Open menu" onClick={() => setMenu(true)}><Menu size={18} /></button>
          <div className="crumbs"><span>SpecForge</span><span>/</span>{tool && <><span>{tool}</span><span>/</span></>}<strong>{current?.label ?? "Page"}</strong></div>
          <button className="iconbtn search-mobile" aria-label="Search pages and actions" onClick={() => setPalette(true)}><Search size={17} /></button>
          <Bell />
          <button className="iconbtn" aria-label={dark ? "Switch to light theme" : "Switch to dark theme"} title="Toggle theme" onClick={() => setTheme(dark ? "light" : "dark")}>
            {dark ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <a href="/about.html" target="_blank" rel="noopener" className="iconbtn about-btn" aria-label="About SpecForge (opens presentation page)" title="About SpecForge"><Info size={17} /><span className="about-lbl">About</span></a>
          <Link to="/settings" className="iconbtn" aria-label="Settings" title="Settings">{(() => { const I = NAV.find((n) => n.to === "/settings")!.icon; return <I size={17} />; })()}</Link>
          <Link to="/account" className="iconbtn user-btn" aria-label={`My account (${user.username})`} title={`Signed in as ${user.username}${isAdmin && me.auth ? " (admin)" : ""}`}>
            <UserRound size={17} /><span className="user-lbl">{user.display_name || user.username}</span>
          </Link>
          {me.auth && <button className="iconbtn" aria-label="Sign out" title="Sign out" onClick={signOut}><LogOut size={17} /></button>}
        </header>
        <main className="main" id="main">{children}</main>
      </div>
      <CommandPalette open={palette} onClose={() => setPalette(false)} />
    </div>
  );
}
