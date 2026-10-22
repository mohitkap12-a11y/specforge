import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ArrowRight, Database } from "lucide-react";
import { rememberSource } from "../components/ExecScenarios";
import { Badge, Empty, ErrorNote, Loading, PageHeader } from "../components/ui";
import { useJobs } from "../components/jobs";
import { useStoredState } from "../hooks";
import { parseOpcode } from "../util";
import type { CmdSource } from "../types";

const hex = (n: number, w = 2) => `0x${n.toString(16).padStart(w, "0")}`;

function Add() {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [auto, setAuto] = useState(true);
  const add = useMutation({
    mutationFn: async () => {
      const p = path.trim();
      const sug = await api.suggestMapping(p);
      const { id } = await api.addSource(p, sug.mapping);
      const job = auto ? await api.importSource(id) : null;
      return { id, job, warnings: (sug.warnings ?? []) as string[] };
    },
    onSuccess: (r) => { setPath(""); qc.invalidateQueries({ queryKey: ["sources"] }); if (r.job) track(r.job.job, "Import command DB"); },
  });
  return (
    <div className="card">
      <h2>Add a command database</h2>
      <div className="row">
        <input className="grow" placeholder="Path to a SQLite file on the server, e.g. /data/run.db" value={path} onChange={(e) => setPath(e.target.value)} />
        <label className="small"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> import now</label>
        <button className="primary" disabled={!path.trim() || add.isPending} onClick={() => add.mutate()}>Add</button>
      </div>
      <div className="muted small">The file is opened read-only by the machine running <code>specforge serve</code>. Column mapping is guessed from the schema; review it below before relying on it.</div>
      <ErrorNote error={add.error} />
      {add.data?.warnings.map((w, i) => <div key={i} className="toast bad">Mapping warning: {w}</div>)}
    </div>
  );
}

function Mapping({ src }: { src: CmdSource }) {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [text, setText] = useState(JSON.stringify(src.mapping, null, 2));
  useEffect(() => setText(JSON.stringify(src.mapping, null, 2)), [src.id, src.mapping]);
  let parsed: unknown, err: string | null = null;
  try { parsed = JSON.parse(text); } catch (e) { err = (e as Error).message; }
  const save = useMutation({ mutationFn: () => api.putMapping(src.id, parsed), onSuccess: () => qc.invalidateQueries({ queryKey: ["sources"] }) });
  const imp = useMutation({ mutationFn: () => api.importSource(src.id), onSuccess: (r) => track(r.job, `Import ${src.name || src.path}`) });
  const dirty = !err && JSON.stringify(parsed) !== JSON.stringify(src.mapping);
  return (
    <>
      <p className="muted small">Maps columns of the source table to command fields. Saving resets the source to “new”; import again to decode with the new mapping.</p>
      <textarea rows={14} spellCheck={false} value={text} onChange={(e) => setText(e.target.value)} aria-label="Column mapping JSON" />
      {err && <div className="toast bad">Invalid JSON: {err}</div>}
      <ErrorNote error={save.error ?? imp.error} />
      <div className="row" style={{ marginTop: 8 }}>
        <button disabled={!dirty || save.isPending} onClick={() => save.mutate()}>Save mapping</button>
        <button className="primary" disabled={dirty || !!err || imp.isPending} onClick={() => imp.mutate()} title={dirty ? "Save the mapping first" : ""}>
          {src.status === "imported" ? "Re-import" : "Import"}
        </button>
      </div>
    </>
  );
}

const LIMIT = 100;
function Browser({ src }: { src: CmdSource }) {
  const [opcode, setOpcode] = useState("");
  const [queue, setQueue] = useState("");
  const [iface, setIface] = useState("");
  const [test, setTest] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => { setOffset(0); }, [src.id, opcode, queue, iface, test]);
  const op = parseOpcode(opcode);
  const badOp = opcode.trim() !== "" && op === null;
  // the API has no total for this list, so fetch one extra row to know whether a next page exists
  const q = useQuery({
    queryKey: ["cmds", src.id, op ?? "", queue, iface, test, offset],
    enabled: !badOp && src.decoded_count > 0,
    queryFn: () => api.cmdCommands(src.id, { opcode: op ?? undefined, queue, interface: iface, test, offset, limit: LIMIT + 1 }),
  });
  const rows = (q.data ?? []).slice(0, LIMIT);
  const more = (q.data?.length ?? 0) > LIMIT;
  if (src.decoded_count === 0) return <div className="muted">Nothing decoded yet. Import the source first.</div>;
  return (
    <>
      <div className="row" style={{ marginBottom: 8 }}>
        <input placeholder="Opcode (e.g. 0x06, 02h, 6)" value={opcode} onChange={(e) => setOpcode(e.target.value)} size={20} aria-invalid={badOp} />
        <select value={queue} onChange={(e) => setQueue(e.target.value)} aria-label="Queue"><option value="">Any queue</option><option>admin</option><option>io</option><option>mi</option></select>
        <select value={iface} onChange={(e) => setIface(e.target.value)} aria-label="Interface"><option value="">Any interface</option><option>pcie</option><option>mi</option></select>
        <input placeholder="Test name" value={test} onChange={(e) => setTest(e.target.value)} />
      </div>
      {badOp && <div className="toast bad">Opcode must be a number (decimal, 0x.. or ..h).</div>}
      <Loading q={q} />
      <div className="scroll">
        <table className="t">
          <thead><tr><th>Row</th><th>Test</th><th>Seq</th><th>Interface</th><th>Queue</th><th>Opcode</th><th>NSID</th><th /></tr></thead>
          <tbody>{rows.map((r) => (
            <Fragment key={r.id}>
              <tr className="click" onClick={() => setOpen(open === r.id ? null : r.id)}>
                <td>{r.src_row_id ?? r.id}</td><td>{r.test_id ?? ""}</td><td>{r.seq ?? ""}</td><td>{r.interface}</td><td>{r.queue}</td>
                <td className="mono">{hex(r.opcode)}</td><td>{r.nsid ?? ""}</td><td className="muted">{open === r.id ? "▾" : "▸"}</td>
              </tr>
              {open === r.id && (
                <tr><td colSpan={8}>
                  <div className="mono small dws">{Object.entries(r.dwords).sort((a, b) => Number(a[0]) - Number(b[0])).map(([k, v]) => <span key={k}>DW{k}={hex(v, 8)}</span>)}</div>
                </td></tr>
              )}
            </Fragment>
          ))}</tbody>
        </table>
      </div>
      <div className="row small" style={{ marginTop: 6 }}>
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIMIT))}>Prev</button>
        <span>{rows.length ? `${offset + 1}–${offset + rows.length}` : "no rows"}</span>
        <button disabled={!more} onClick={() => setOffset(offset + LIMIT)}>Next</button>
      </div>
    </>
  );
}

export default function CommandDBsPage() {
  const sources = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const [id, setId] = useStoredState<number | null>("specforge.cmdsrc", null);
  const [tab, setTab] = useState<"commands" | "mapping" | "problems">("commands");
  const list = sources.data ?? [];
  const src = list.find((s) => s.id === id) ?? list[0];
  return (
    <>
      <PageHeader title="Command DBs" description="SQLite dumps of the commands your test scripts submitted. Review the column mapping, import, browse what was decoded, then analyse it on the Scenarios page: one scenario per test case." />
      <Add />
      <Loading q={sources} />
      {sources.data && list.length === 0 && <Empty icon={Database} title="No command databases yet">Add the path to a SQLite file above.</Empty>}
      {src && (
        <div className="card">
          <div className="row" style={{ marginBottom: 8 }}>
            <label>Source{" "}
              <select value={src.id} onChange={(e) => setId(Number(e.target.value))}>
                {list.map((s) => <option key={s.id} value={s.id}>#{s.id} {s.name || s.path}</option>)}
              </select>
            </label>
            <Badge value={src.status === "imported" ? "ok" : "warning"} label={src.status} />
            <span className="muted small">{src.decoded_count} of {src.row_count} rows decoded{src.imported_at ? ` · imported ${src.imported_at}` : ""}</span>
            <span className="grow" />
            {src.status === "imported" && <Link to="/scenarios" onClick={() => rememberSource(src.id)}><button className="primary sm">See what these tests executed <ArrowRight size={13} /></button></Link>}
          </div>
          <div className="mono small muted" style={{ marginBottom: 8, wordBreak: "break-all" }}>{src.path}</div>
          <div className="tabs">
            {(["commands", "mapping", "problems"] as const).map((t) => (
              <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                {t === "commands" ? "Decoded commands" : t === "mapping" ? "Column mapping" : `Undecoded rows${src.unknown?.undecodable_rows ? ` (${src.unknown.undecodable_rows})` : ""}`}
              </button>
            ))}
          </div>
          {tab === "commands" && <Browser src={src} />}
          {tab === "mapping" && <Mapping src={src} />}
          {tab === "problems" && (
            src.unknown?.errors?.length
              ? <div className="scroll" style={{ maxHeight: 360 }}>{src.unknown.errors.length < (src.unknown.undecodable_rows ?? 0) && <div className="muted small">Showing the first {src.unknown.errors.length} of {src.unknown.undecodable_rows} failures.</div>}{src.unknown.errors.map((e, i) => <div key={i} className="mono small">row {typeof e.row === "object" ? JSON.stringify(e.row) : String(e.row)}: {e.error}</div>)}</div>
              : <div className="muted">{src.status === "imported" ? "Every row decoded." : "Import the source to see decoding problems."}</div>
          )}
        </div>
      )}
    </>
  );
}
