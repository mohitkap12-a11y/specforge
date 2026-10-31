/** Formatting for the admin MCP Metrics, Misses & errors and Call log tabs (kept free of React so it can be unit tested). */

export const fmtInt = (n: number | null | undefined) => (n === null || n === undefined ? "–" : Math.round(n).toLocaleString("en-US"));

export const fmtPct = (n: number | null | undefined) => (n === null || n === undefined ? "–" : `${n.toFixed(1)}%`);

export function fmtMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  if (ms >= 10_000) return `${(ms / 1000).toFixed(0)} s`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}

export function fmtChars(n: number | null | undefined): string {
  if (n === null || n === undefined) return "–";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)} M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)} k`;
  return String(n);
}

/** About 4 characters per token for English and Markdown tables. */
export const approxTokens = (chars: number | null | undefined) => (chars === null || chars === undefined ? null : Math.round(chars / 4));

export function fmtDuration(s: number | null | undefined): string {
  if (s === null || s === undefined) return "–";
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} m`;
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "—";
  const s = Math.max(0, now - ts);
  return s < 60 ? `${Math.round(s)} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}

export const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** Axis / tooltip label of a time bucket: time of day for buckets under a day, the date otherwise. */
export function bucketLabel(t: number, size: number, withDay = false, axis = false): string {
  const d = new Date(t * 1000);
  if (size >= 86400) return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  if (axis && size >= 3 * 3600) return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });     // the tooltip has the time
  const hm = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return withDay || size >= 3 * 3600 ? `${d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })} ${hm}` : hm;
}

export interface Delta { text: string; tone: "up" | "down" | "flat" }

/** Change against the previous period. `lowerIsBetter`: a fall is good news (errors, latency). */
export function delta(cur: number | null | undefined, prev: number | null | undefined, opts: { lowerIsBetter?: boolean; points?: boolean } = {}): Delta | null {
  if (cur === null || cur === undefined || prev === null || prev === undefined) return null;
  const diff = cur - prev;
  if (Math.abs(diff) < 1e-9) return { text: "no change", tone: "flat" };
  const arrow = diff > 0 ? "▲" : "▼";
  const text = opts.points ? `${arrow} ${Math.abs(diff).toFixed(1)} pt` : prev === 0 ? `${arrow} new` : Math.abs(diff / prev) > 9.99 ? `${arrow} >999%` : `${arrow} ${Math.abs((100 * diff) / prev).toFixed(0)}%`;
  const good = opts.lowerIsBetter ? diff < 0 : diff > 0;
  return { text, tone: good ? "up" : "down" };
}

/** A round axis maximum at or above `v` (1, 2, 2.5, 5 x 10^n) and its tick step. */
export function niceScale(v: number, ticks = 4): { max: number; step: number } {
  if (!(v > 0)) return { max: ticks, step: 1 };
  const raw = v / (ticks + 1);      // up to ticks + 1 steps, so 1060 gets 0–1250 rather than 0–1500
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  return { max: step * Math.ceil(v / step), step };
}

/** Arguments as a one-line call: get_section(section="99.9", spec="base"). */
export function callText(tool: string, args: Record<string, unknown>): string {
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === "string" ? JSON.stringify(v) : String(v)}`);
  return `${tool}(${parts.join(", ")})`;
}

export const OUTCOME_LABEL: Record<string, string> = { ok: "answered", miss: "miss", error: "error", rate_limited: "rate limited", refused: "refused" };
export const OUTCOME_TONE: Record<string, string> = { ok: "ok", miss: "warn", error: "bad", rate_limited: "warn", refused: "" };
export const STATUS_TONE: Record<string, string> = { open: "bad", investigating: "info", fixed: "ok", ignored: "" };
