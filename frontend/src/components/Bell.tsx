import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Bell as BellIcon } from "lucide-react";
import { api } from "../api";
import { useAuth } from "./auth";
import { isActive, queueText, useJobs, type TrackedJob } from "./jobs";
import { Badge } from "./ui";
import { useToast } from "./Toasts";

const elapsed = (since: number) => {
  const s = Math.max(0, Math.round((Date.now() - since) / 1000));
  return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

/** Header bell: the jobs in progress (the user's own, every user's for an admin) with their place in the server's queue,
 *  and those that finished since the page was opened. Replaces the stack of floating progress cards. */
export function Bell() {
  const { jobs, clearFinished } = useJobs();
  const { push } = useToast();
  const { user, isAdmin, me } = useAuth();
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const [, tick] = useState(0);

  const shown = isAdmin ? jobs : jobs.filter((j) => j.mine !== false || j.requested);
  // running first, then queued in the order the server will start them (oldest first)
  const active = shown.filter(isActive).sort((a, b) => (a.state === b.state ? a.id - b.id : a.state === "running" ? -1 : 1));
  const finished = shown.filter((j) => !isActive(j)).slice(0, 8);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    const h = active.length ? setInterval(() => tick((n) => n + 1), 1000) : undefined;
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); if (h) clearInterval(h); };
  }, [open, active.length]);

  const cancel = (j: TrackedJob) => api.cancelJob(j.id).then(
    (r) => { if (r.handed_to) push({ title: `${j.label}: handed to ${r.handed_to}`, description: `${r.handed_to} asked for the same, so it keeps its place in the queue for them; it is no longer yours.`, tone: "info" }); },
    (e) => push({ title: "Cancel failed", description: e instanceof Error ? e.message : String(e), tone: "bad" }));
  const whose = (j: TrackedJob) => (me.auth && j.user && j.user !== user.username ? ` · ${j.user}` : "");
  const item = (j: TrackedJob) => (
    <li key={j.id} className="bell-item">
      <div className="row">
        {j.state === "running" ? <span className="spinner" /> : j.state === "queued" ? <Badge value="queued" label="Queued" /> : <Badge value={j.state} />}
        <strong className="grow bell-label" title={j.label}>{j.label}</strong>
        {j.state === "running" && <span className="muted small">{elapsed(j.startedAt)}</span>}
      </div>
      <div className="muted small">
        {j.state === "running" ? j.message || "Working…" : j.state === "queued" ? queueText(j) || "Waiting for a free worker" : j.state === "failed" ? (j.error ?? "").split("\n")[0] : ""}
        {whose(j)}
      </div>
      {j.state === "running" && <div className={`progress ${j.total > 0 ? "" : "indeterminate"}`}><span style={{ width: j.total > 0 ? `${Math.min(100, (100 * j.progress) / j.total)}%` : undefined }} /></div>}
      <div className="row small">
        <Link to={`/activity?job=${j.id}`} onClick={() => setOpen(false)}>View logs</Link>
        <span className="grow" />
        {isActive(j) && j.can_cancel && <button className="sm danger" onClick={() => cancel(j)}>Cancel</button>}
      </div>
    </li>
  );

  const n = active.length;
  return (
    <div className="bell-wrap" ref={wrap}>
      <button className="iconbtn" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen((o) => !o)} title={isAdmin && me.auth ? "Jobs on the server" : "Your jobs"}
        aria-label={n ? `${n} job${n > 1 ? "s" : ""} in progress, show them` : "Jobs: none in progress"}>
        <BellIcon size={17} />
        {n > 0 && <span className="bell-count">{n > 99 ? "99+" : n}</span>}
      </button>
      {open && (
        <div className="bell-panel" role="dialog" aria-label="Jobs">
          <div className="bell-head"><strong className="grow">{isAdmin && me.auth ? "Jobs on the server (all users)" : "Your jobs"}</strong>
            <Link className="small" to="/activity" onClick={() => setOpen(false)}>Activity →</Link></div>
          {n === 0 && finished.length === 0 && (
            <p className="muted small bell-empty">Nothing in progress. Parses and conversions you start show up here, with their place in the server's queue; they keep running when you leave the page.</p>
          )}
          {n > 0 && <><div className="bell-sub">In progress · {n}</div><ul className="bell-list">{active.map(item)}</ul></>}
          {finished.length > 0 && (
            <>
              <div className="bell-sub row"><span className="grow">Finished</span><button className="ghost sm" onClick={clearFinished}>Clear</button></div>
              <ul className="bell-list">{finished.map(item)}</ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
