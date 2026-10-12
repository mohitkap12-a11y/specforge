import type { ReactNode } from "react";
import type { Counts } from "../types";

const STATE_LABEL: Record<string, string> = {
  covered: "Covered", stimulus_covered: "Stimulus covered", partial: "Partial", not_covered: "Not covered",
  no_scenario: "No scenario", needs_review: "Needs review", na: "N/A",
};
export const STATE_ORDER = ["covered", "stimulus_covered", "partial", "not_covered", "no_scenario", "needs_review", "na"] as const;
export const stateLabel = (s: string) => STATE_LABEL[s] ?? s;

const TONE: Record<string, string> = {
  covered: "ok", stimulus_covered: "ok", approved: "ok", done: "ok", ok: "ok", active: "ok",
  partial: "warn", draft: "warn", running: "info", queued: "info", needs_review: "info", warning: "warn",
  not_covered: "bad", no_scenario: "bad", rejected: "bad", failed: "bad", error: "bad",
};
export function Badge({ value, label }: { value: string; label?: string }) {
  return <span className={`badge ${TONE[value] ?? ""}`}>{label ?? stateLabel(value)}</span>;
}

export function Pct({ v }: { v: number | null | undefined }) {
  return <>{v === null || v === undefined ? "–" : `${v.toFixed(1)}%`}</>;
}

/** Stacked bar of coverage states. Segment widths are shares of `total`, so N/A is visible but greyed. */
export function StateBar({ c }: { c: Counts }) {
  const total = c.total || 1;
  return (
    <div role="img" aria-label={STATE_ORDER.map((s) => `${stateLabel(s)} ${c[s]}`).join(", ")}>
      <div className="bar">
        {STATE_ORDER.map((s) => (c[s] ? <span key={s} className={`seg-${s}`} style={{ width: `${(100 * c[s]) / total}%` }} title={`${stateLabel(s)}: ${c[s]}`} /> : null))}
      </div>
    </div>
  );
}
export function Legend({ c }: { c?: Counts }) {
  return (
    <div className="legend">
      {STATE_ORDER.filter((s) => !c || c[s]).map((s) => (
        <span key={s}><i className={`seg-${s}`} />{stateLabel(s)}{c ? ` ${c[s]}` : ""}</span>
      ))}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="toast bad" role="alert">{error instanceof Error ? error.message : String(error)}</div>;
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="card muted">{children}</div>;
}
export function Loading({ q }: { q: { isLoading: boolean; error: unknown } }) {
  if (q.isLoading) return <div className="muted">Loading…</div>;
  return <ErrorNote error={q.error} />;
}

export function Pager({ total, offset, limit, onChange }: { total: number; offset: number; limit: number; onChange: (o: number) => void }) {
  if (total <= limit) return <div className="muted small">{total} item{total === 1 ? "" : "s"}</div>;
  return (
    <div className="row small">
      <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Prev</button>
      <span>{offset + 1}–{Math.min(total, offset + limit)} of {total}</span>
      <button disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  );
}
