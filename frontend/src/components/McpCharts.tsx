import { useRef, useState, type ReactNode } from "react";
import type { McpBucket } from "../types";
import { bucketLabel, fmtInt, fmtMs, niceScale } from "../mcpFormat";

/** Hover card that follows the pointer inside a chart. */
function Tip({ x, y, w, children }: { x: number; y: number; w: number; children: ReactNode }) {
  const left = Math.min(Math.max(x + 12, 0), Math.max(w - 200, 0));
  return <div className="mcpc-tip" style={{ left, top: Math.max(y - 12, 0) }}>{children}</div>;
}

function useHover() {
  const box = useRef<HTMLDivElement>(null);
  const [h, setH] = useState<{ i: number; x: number; y: number } | null>(null);
  const at = (e: React.MouseEvent, i: number) => {
    const r = box.current?.getBoundingClientRect();
    if (r) setH({ i, x: e.clientX - r.left, y: e.clientY - r.top });
  };
  return { box, h, at, clear: () => setH(null), width: box.current?.clientWidth ?? 600 };
}

const L = 44, R = 8, T = 10;

/** Calls per time bucket, stacked by outcome (answered, miss, error), with refused / rate-limited requests in the tooltip. */
export function OutcomeBars({ series, bucket, onPick }: { series: McpBucket[]; bucket: number; onPick?: (b: McpBucket) => void }) {
  const W = 760, H = 220, B = H - 26;
  const hv = useHover();
  const totals = series.map((b) => b.ok + b.miss + b.error);
  const { max, step } = niceScale(Math.max(1, ...totals));
  const bw = (W - L - R) / Math.max(series.length, 1);
  const y = (v: number) => B - (v / max) * (B - T);
  const every = Math.max(1, Math.ceil(series.length / 7));
  const hb = hv.h ? series[hv.h.i] : null;
  return (
    <div ref={hv.box} className="mcpc" onMouseLeave={hv.clear}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Tool calls per time bucket by outcome">
        {Array.from({ length: Math.round(max / step) + 1 }, (_, i) => i * step).map((v) => (
          <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="mcpc-grid" /><text x={L - 6} y={y(v) + 4} textAnchor="end">{fmtInt(v)}</text></g>
        ))}
        {series.map((b, i) => {
          const x = L + i * bw + Math.min(1.5, bw * 0.15), w = Math.max(bw - Math.min(3, bw * 0.3), 1);
          let top = B;
          const segs = ([["ok", b.ok], ["miss", b.miss], ["error", b.error]] as const).filter(([, v]) => v > 0).map(([k, v]) => {
            const h = (v / max) * (B - T);
            top -= h;
            return <rect key={k} x={x} y={top + 1} width={w} height={Math.max(h - 2, 1)} rx={Math.min(3, w / 2)} className={`mcpc-${k}`} />;
          });
          return (
            <g key={b.t} onMouseMove={(e) => hv.at(e, i)} onClick={() => onPick?.(b)} style={{ cursor: onPick ? "pointer" : undefined }}>
              <rect x={L + i * bw} y={T} width={bw} height={B - T} fill="transparent" />
              {hv.h?.i === i && <rect x={L + i * bw} y={T} width={bw} height={B - T} className="mcpc-hover" />}
              {segs}
              {i % every === 0 && <text x={L + i * bw + bw / 2} y={B + 17} textAnchor="middle">{bucketLabel(b.t, bucket, false, true)}</text>}
            </g>
          );
        })}
        <line x1={L} x2={W - R} y1={B} y2={B} className="mcpc-axis" />
      </svg>
      {hv.h && hb && (
        <Tip x={hv.h.x} y={hv.h.y} w={hv.width}>
          <b>{bucketLabel(hb.t, bucket, true)}</b>
          <div><i className="mcpc-key mcpc-ok" />Answered <span>{fmtInt(hb.ok)}</span></div>
          <div><i className="mcpc-key mcpc-miss" />Miss <span>{fmtInt(hb.miss)}</span></div>
          <div><i className="mcpc-key mcpc-error" />Error <span>{fmtInt(hb.error)}</span></div>
          {hb.refused > 0 && <div className="muted">Refused / rate limited <span>{fmtInt(hb.refused)}</span></div>}
          <div className="muted">p95 <span>{fmtMs(hb.p95_ms)}</span></div>
        </Tip>
      )}
    </div>
  );
}

/** p50 and p95 latency per bucket on one axis, with the 2 s slow-call threshold. */
export function LatencyLines({ series, bucket, slowMs = 2000 }: { series: McpBucket[]; bucket: number; slowMs?: number }) {
  const W = 560, H = 220, B = H - 26;
  const hv = useHover();
  const top = Math.max(slowMs * 1.15, ...series.map((b) => b.p95_ms ?? 0));
  const { max, step } = niceScale(top);
  const n = series.length;
  const x = (i: number) => L + (n <= 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (n - 1));
  const y = (v: number) => B - (v / max) * (B - T);
  const path = (k: "p50_ms" | "p95_ms") => {
    let d = "", pen = false;
    series.forEach((b, i) => {
      const v = b[k];
      if (v === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const dots = (k: "p50_ms" | "p95_ms") => series.map((b, i) => {      // a lone value between gaps still shows
    const v = b[k], prev = series[i - 1]?.[k], next = series[i + 1]?.[k];
    return v !== null && (prev === null || prev === undefined) && (next === null || next === undefined)
      ? <circle key={`${k}${i}`} cx={x(i)} cy={y(v)} r={3} className={k === "p50_ms" ? "mcpc-l1f" : "mcpc-l2f"} /> : null;
  });
  const every = Math.max(1, Math.ceil(n / 5));
  const hb = hv.h ? series[hv.h.i] : null;
  return (
    <div ref={hv.box} className="mcpc" onMouseLeave={hv.clear}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Latency p50 and p95 over time">
        {Array.from({ length: Math.round(max / step) + 1 }, (_, i) => i * step).map((v) => (
          <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} className="mcpc-grid" /><text x={L - 6} y={y(v) + 4} textAnchor="end">{fmtMs(v)}</text></g>
        ))}
        <line x1={L} x2={W - R} y1={y(slowMs)} y2={y(slowMs)} className="mcpc-threshold" />
        <path d={path("p95_ms")} className="mcpc-l2" />
        <path d={path("p50_ms")} className="mcpc-l1" />
        {dots("p95_ms")}{dots("p50_ms")}
        {series.map((b, i) => (
          <g key={b.t}>
            {i % every === 0 && <text x={x(i)} y={B + 17} textAnchor="middle">{bucketLabel(b.t, bucket, false, true)}</text>}
            <rect x={x(i) - (W - L - R) / Math.max(n, 1) / 2} y={T} width={(W - L - R) / Math.max(n, 1)} height={B - T} fill="transparent" onMouseMove={(e) => hv.at(e, i)} />
          </g>
        ))}
        {hb && hv.h && <line x1={x(hv.h.i)} x2={x(hv.h.i)} y1={T} y2={B} className="mcpc-cross" />}
        <line x1={L} x2={W - R} y1={B} y2={B} className="mcpc-axis" />
      </svg>
      {hv.h && hb && (
        <Tip x={hv.h.x} y={hv.h.y} w={hv.width}>
          <b>{bucketLabel(hb.t, bucket, true)}</b>
          <div><i className="mcpc-key mcpc-l1f" />p50 <span>{fmtMs(hb.p50_ms)}</span></div>
          <div><i className="mcpc-key mcpc-l2f" />p95 <span>{fmtMs(hb.p95_ms)}</span></div>
          <div className="muted">calls <span>{fmtInt(hb.ok + hb.miss + hb.error)}</span></div>
        </Tip>
      )}
    </div>
  );
}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Calls by weekday and hour (viewer's local time), one blue ramp. */
export function WeekHeatmap({ grid }: { grid: number[][] }) {
  const hv = useHover();
  const max = Math.max(1, ...grid.flat());
  const cw = 26, ch = 22, LX = 36;
  const level = (v: number) => (v === 0 ? 0 : 1 + Math.min(4, Math.floor((v / max) * 5 - 1e-9)));
  const h = hv.h ? { d: Math.floor(hv.h.i / 24), hr: hv.h.i % 24 } : null;
  return (
    <div ref={hv.box} className="mcpc" onMouseLeave={hv.clear}>
      <svg viewBox={`0 0 ${LX + 24 * cw} ${7 * ch + 46}`} width="100%" role="img" aria-label="Calls by weekday and hour">
        {grid.map((row, d) => (
          <g key={d}>
            <text x={LX - 6} y={d * ch + ch / 2 + 4} textAnchor="end">{DAYS[d]}</text>
            {row.map((v, hr) => <rect key={hr} x={LX + hr * cw} y={d * ch} width={cw - 2} height={ch - 2} rx={3} className={`mcpc-h${level(v)}`} onMouseMove={(e) => hv.at(e, d * 24 + hr)} />)}
          </g>
        ))}
        {[0, 6, 12, 18, 23].map((hr) => <text key={hr} x={LX + hr * cw + cw / 2} y={7 * ch + 14} textAnchor="middle">{`${hr}:00`}</text>)}
        <text x={LX} y={7 * ch + 38}>fewer</text>
        {[0, 1, 2, 3, 4, 5].map((l) => <rect key={l} x={LX + 40 + l * 18} y={7 * ch + 28} width={16} height={11} rx={2} className={`mcpc-h${l}`} />)}
        <text x={LX + 40 + 6 * 18 + 4} y={7 * ch + 38}>more</text>
      </svg>
      {hv.h && h && <Tip x={hv.h.x} y={hv.h.y} w={hv.width}><b>{DAYS[h.d]} {h.hr}:00–{h.hr + 1}:00</b><div>{fmtInt(grid[h.d][h.hr])} calls</div></Tip>}
    </div>
  );
}

/** A horizontal bar inside a table cell: the share of the largest value. */
export function CellBar({ v, max }: { v: number; max: number }) {
  return <div className="mcpc-cellbar"><span style={{ width: `${max > 0 ? Math.max(2, (100 * v) / max) : 0}%` }} /></div>;
}

/** Outcome mix of one row: answered / miss / error shares, with a 2px gap between segments. */
export function OutcomeMix({ ok, miss, error }: { ok: number; miss: number; error: number }) {
  const n = ok + miss + error || 1;
  return (
    <div className="mcpc-mix" title={`${ok} answered · ${miss} miss · ${error} error`}>
      {ok > 0 && <span className="mcpc-ok" style={{ flexGrow: ok / n }} />}
      {miss > 0 && <span className="mcpc-miss" style={{ flexGrow: miss / n }} />}
      {error > 0 && <span className="mcpc-error" style={{ flexGrow: error / n }} />}
    </div>
  );
}

/** Occurrences per day of one group of misses or errors. */
export function DayBars({ days }: { days: { day: string; count: number }[] }) {
  const max = Math.max(1, ...days.map((d) => d.count));
  return (
    <div className="mcpc-days" role="img" aria-label="Occurrences per day">
      {days.map((d) => <span key={d.day} title={`${d.day}: ${d.count}`} style={{ height: `${Math.max(8, (100 * d.count) / max)}%` }} />)}
    </div>
  );
}
