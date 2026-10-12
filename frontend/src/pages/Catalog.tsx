import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading, Pager } from "../components/ui";
import { SetPicker, useSelectedSet } from "../components/SetPicker";
import { useDebounced } from "../hooks";
import type { CatalogItem } from "../types";

const LIMIT = 100;
const KINDS = ["command", "field", "value", "boundary", "reserved", "mi_tunnel", "requirement"];
const OBS = ["submission", "stimulus", "unobservable"];

function Drawer({ setId, item, onClose }: { setId: number; item: CatalogItem; onClose: () => void }) {
  const qc = useQueryClient();
  const [obs, setObs] = useState(item.observability);
  const [reason, setReason] = useState(item.na_reason ?? "");
  useEffect(() => { setObs(item.observability); setReason(item.na_reason ?? ""); }, [item.key, item.observability, item.na_reason]);
  const save = useMutation({
    mutationFn: (clear: boolean) => (clear ? api.override(setId, item.key, null, null) : api.override(setId, item.key, obs, reason.trim() || null)),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["catalog"] }),
  });
  const a = item.attrs ?? {};
  return (
    <aside className="drawer" aria-label="Catalog item">
      <div className="row"><h2 className="grow">{item.label}</h2><button onClick={onClose}>Close</button></div>
      <div className="row"><span className="badge">{item.kind}</span><span className="badge">{item.observability}</span></div>
      <p className="mono small">{item.key}</p>
      <h3>Observability</h3>
      <p className="muted small">Submission: checkable from the submitted entry. Stimulus: only the triggering submission is visible. Unobservable: listed as N/A and excluded from the percentage.</p>
      <div className="row">
        <select value={obs} onChange={(e) => setObs(e.target.value)} aria-label="Observability">{OBS.map((o) => <option key={o} value={o}>{o}</option>)}</select>
      </div>
      <label>Reason (shown as the N/A reason)<input style={{ width: "100%" }} value={reason} onChange={(e) => setReason(e.target.value)} /></label>
      <div className="row" style={{ margin: "10px 0" }}>
        <button className="primary" disabled={save.isPending || (obs === item.observability && reason === (item.na_reason ?? ""))} onClick={() => save.mutate(false)}>Save override</button>
        <button disabled={save.isPending} onClick={() => save.mutate(true)} title="Removes the override and regenerates the catalog so the generated value returns">Clear override</button>
      </div>
      <p className="muted small">Existing coverage runs keep their own snapshot. Run coverage again to see the change.</p>
      <ErrorNote error={save.error} />
      <h3>Attributes</h3>
      <pre>{JSON.stringify(a, null, 1)}</pre>
    </aside>
  );
}

export default function CatalogPage() {
  const sel = useSelectedSet();
  const [kind, setKind] = useState("");
  const [obs, setObs] = useState("");
  const [command, setCommand] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<CatalogItem | null>(null);
  useEffect(() => { setOffset(0); setOpen(null); }, [sel.setId, kind, obs, command, dq]);
  const list = useQuery({
    queryKey: ["catalog", sel.setId, kind, obs, command, dq, offset],
    enabled: sel.setId !== null,
    queryFn: () => api.catalog(sel.setId!, { kind, observability: obs, command, q: dq, offset, limit: LIMIT }),
  });
  const cmds = useQuery({ queryKey: ["catalog-cmds", sel.setId], enabled: sel.setId !== null, queryFn: () => api.catalog(sel.setId!, { kind: "command", limit: 500 }) });
  // keep the open drawer in sync after an override is saved
  const live = open ? list.data?.items.find((i) => i.key === open.key) ?? open : null;
  return (
    <>
      <h1>Catalog</h1>
      <div className="row" style={{ marginBottom: 12 }}>
        <SetPicker sel={sel} />
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Kind"><option value="">All kinds</option>{KINDS.map((k) => <option key={k}>{k}</option>)}</select>
        <select value={obs} onChange={(e) => setObs(e.target.value)} aria-label="Observability"><option value="">Any observability</option>{OBS.map((k) => <option key={k}>{k}</option>)}</select>
        <select value={command} onChange={(e) => setCommand(e.target.value)} aria-label="Command">
          <option value="">All commands</option>{cmds.data?.items.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select>
        <input placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorNote error={sel.error ?? list.error} />
      {sel.setId !== null && <Loading q={list} />}
      {list.data && list.data.total === 0 && <Empty>No catalog items. Build the catalog for this set on the Sets page (or relax the filters).</Empty>}
      {list.data && list.data.total > 0 && (
        <div className="card">
          <div className="scroll">
            <table className="t">
              <thead><tr><th>Kind</th><th>Item</th><th>Observability</th><th>N/A reason</th></tr></thead>
              <tbody>{list.data.items.map((i) => (
                <tr key={i.key} className={`click ${live?.key === i.key ? "sel" : ""}`} onClick={() => setOpen(i)}>
                  <td>{i.kind}</td><td>{i.label}<div className="mono small muted">{i.key.split("/").pop()}</div></td>
                  <td><Badge value={i.observability === "submission" ? "ok" : i.observability === "stimulus" ? "info" : "warning"} label={i.observability} /></td>
                  <td className="small">{i.na_reason ?? ""}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <Pager total={list.data.total} offset={offset} limit={LIMIT} onChange={setOffset} />
        </div>
      )}
      {live && sel.setId !== null && <Drawer setId={sel.setId} item={live} onClose={() => setOpen(null)} />}
    </>
  );
}
