import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { FileBarChart } from "lucide-react";
import { Empty, ErrorNote, Loading, PageHeader, Pct } from "../components/ui";
import { SetPicker, useSelectedSet } from "../components/SetPicker";

const FORMATS = [
  { id: "xlsx", label: "Excel workbook", note: "Full traceability workbook: summary, requirements, commands, scenarios, evidence, gaps, N/A with reasons." },
  { id: "html", label: "HTML report", note: "Self-contained single file; prints cleanly to PDF." },
  { id: "json", label: "JSON", note: "Complete machine-readable dataset, including the reproducibility header." },
  { id: "csv", label: "CSV", note: "Flat item list. Choose gaps only or every item." },
] as const;

export default function ReportsPage() {
  const sel = useSelectedSet();
  const runs = useQuery({ queryKey: ["covruns", sel.setId], enabled: sel.setId !== null, queryFn: () => api.covRuns(sel.setId!) });
  const [picked, setPicked] = useState<number | null>(null);
  const [which, setWhich] = useState<"gaps" | "all">("gaps");
  const list = runs.data ?? [];
  const runId = list.some((r) => r.id === picked) ? picked : list[0]?.id ?? null;
  const run = list.find((r) => r.id === runId);
  return (
    <>
      <PageHeader title="Reports" description="Export a coverage run as Excel, HTML, JSON or CSV. Every report records the spec versions, command DB checksum and tool version so numbers can be reproduced." />
      <div className="card row">
        <SetPicker sel={sel} />
        {list.length > 0 && (
          <label>Coverage run{" "}
            <select value={runId ?? ""} onChange={(e) => setPicked(Number(e.target.value))}>
              {list.map((r) => <option key={r.id} value={r.id}>#{r.id} · {r.created}</option>)}
            </select>
          </label>
        )}
        {run && <span className="muted">Covered <strong><Pct v={run.overall?.pct_covered} /></strong> · incl. stimulus <strong><Pct v={run.overall?.pct_incl_stimulus} /></strong></span>}
      </div>
      <ErrorNote error={sel.error ?? runs.error} />
      {sel.setId !== null && <Loading q={runs} />}
      {runs.data && list.length === 0 && <Empty icon={FileBarChart} title="No coverage runs yet">Run coverage first, then export it here.</Empty>}
      {runId !== null && (
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))" }}>
          {FORMATS.map((f) => (
            <div key={f.id} className="card">
              <h2>{f.label}</h2>
              <p className="muted small">{f.note}</p>
              {f.id === "csv" && (
                <label className="row small">Rows
                  <select value={which} onChange={(e) => setWhich(e.target.value as "gaps" | "all")}>
                    <option value="gaps">Gaps only</option><option value="all">All items</option>
                  </select>
                </label>
              )}
              <a href={api.exportUrl(runId, f.id, which)} download><button className="primary">Download .{f.id}</button></a>
            </div>
          ))}
        </div>
      )}
      <p className="muted small">Every report header records the spec versions, parse run ids, command DB checksum, models used and tool version, so any number can be reproduced.</p>
    </>
  );
}
