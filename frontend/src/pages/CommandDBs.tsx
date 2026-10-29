import { Fragment, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ArrowRight, ArrowUp, Database, Folder, FolderOpen, UploadCloud } from "lucide-react";
import { rememberSource } from "../components/ExecScenarios";
import { Badge, Empty, ErrorNote, Loading, PageHeader, Segmented } from "../components/ui";
import { useJobs } from "../components/jobs";
import { useStoredState } from "../hooks";
import { parseOpcode } from "../util";
import type { CmdKind, CmdSource, ScannedDb } from "../types";

const hex = (n: number, w = 2) => `0x${n.toString(16).padStart(w, "0")}`;

const DB_SUFFIXES = [".db", ".sqlite", ".sqlite3", ".db3"];
const isDb = (name: string) => DB_SUFFIXES.some((x) => name.toLowerCase().endsWith(x));
const size = (n: number) => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
type Pick = { file: File; path: string };

/** Files of a drop, folders walked recursively (paths kept relative to what was dropped). */
async function droppedFiles(dt: DataTransfer): Promise<Pick[]> {
  const entries = Array.from(dt.items).map((i) => i.webkitGetAsEntry?.()).filter((e): e is FileSystemEntry => !!e);   // read before any await
  if (!entries.length) return Array.from(dt.files).map((f) => ({ file: f, path: f.name }));
  const out: Pick[] = [];
  const walk = async (e: FileSystemEntry, prefix: string): Promise<void> => {
    if (e.isFile) {
      out.push({ file: await new Promise<File>((res, rej) => (e as FileSystemFileEntry).file(res, rej)), path: prefix + e.name });
    } else if (e.isDirectory) {
      const reader = (e as FileSystemDirectoryEntry).createReader();
      for (;;) {                                   // readEntries answers in batches until it returns none
        const batch = await new Promise<FileSystemEntry[]>((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const c of batch) await walk(c, `${prefix}${e.name}/`);
      }
    }
  };
  for (const e of entries) await walk(e, "");
  return out;
}

function FsBrowser({ start, onPick, onClose }: { start: string; onPick: (path: string) => void; onClose: () => void }) {
  const [path, setPath] = useState(start);
  const q = useQuery({ queryKey: ["fs", path], queryFn: () => api.browseFs(path || undefined), retry: false });
  const d = q.data;
  return (
    <div className="palette-wrap" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }} onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
      <div className="palette fsb" role="dialog" aria-modal="true" aria-label="Browse the server">
        <div className="row fsb-head">
          <button className="sm" disabled={!d || d.parent === null} onClick={() => setPath(d?.parent ?? "")} title="Up one folder"><ArrowUp size={14} /></button>
          <input className="grow mono small" value={path} onChange={(e) => setPath(e.target.value)} placeholder="Server folder" aria-label="Folder" />
        </div>
        {q.error ? <div className="fsb-body"><ErrorNote error={q.error} /><button className="sm" onClick={() => setPath("")}>Start again</button></div> : (
          <ul className="fsb-body" role="listbox">
            {q.isLoading && <li className="muted small">Loading…</li>}
            {d?.dirs.map((x) => <li key={x.path} className="opt" onClick={() => setPath(x.path)}><Folder size={14} /> {x.name}</li>)}
            {d?.databases.map((x) => (
              <li key={x.path} className="opt" onClick={() => onPick(x.path)} title="Use this database"><Database size={14} /> {x.name} <span className="muted small">{size(x.size)}</span></li>
            ))}
            {d && d.path && !d.dirs.length && !d.databases.length && <li className="muted small">No folders or databases here.</li>}
          </ul>
        )}
        <div className="row fsb-foot">
          <span className="muted small grow">Click a database to use it, or open a folder and use the whole folder.</span>
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!d?.path} onClick={() => onPick(d!.path)}>Use this folder</button>
        </div>
      </div>
    </div>
  );
}

const KIND_LABEL: Record<CmdKind, string> = { nvme: "NVMe", mi: "NVMe-MI" };

function Add() {
  const { track } = useJobs();
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [kind, setKind] = useStoredState<CmdKind | "both">("specforge.cmdkind", "nvme");
  const [auto, setAuto] = useState(true);
  const [browsing, setBrowsing] = useState(false);
  const [over, setOver] = useState(false);
  const folderInput = useRef<HTMLInputElement>(null);
  const filesInput = useRef<HTMLInputElement>(null);
  const kinds: CmdKind[] = kind === "both" ? ["nvme", "mi"] : [kind];
  const scan = useMutation({ mutationFn: (p: string) => api.scanDbs(p, kinds[0]) });
  const run = (p: string) => { const t = p.trim(); if (t) { setPath(t); scan.mutate(t); } };
  useEffect(() => { if (scan.data) scan.mutate(scan.data.path); }, [kind]);   // eslint-disable-line react-hooks/exhaustive-deps
  const upload = useMutation({
    mutationFn: async (files: Pick[] | Promise<Pick[]>) => {
      const dbs = (await files).filter((f) => isDb(f.path));
      if (!dbs.length) throw new Error(`No SQLite databases (${DB_SUFFIXES.join(", ")}) in what was dropped.`);
      return api.uploadDbs(dbs);
    },
    onSuccess: (r) => run(r.path),
  });
  const add = useMutation({
    mutationFn: async () => {
      const p = scan.data!.path;
      const out: { job: { job: number } | null; label: string; warnings: string[] }[] = [];
      for (const k of scan.data!.format === "command_history" ? kinds : [undefined]) {
        if (k && scan.data!.kinds && !scan.data!.kinds.includes(k)) continue;
        const sug = await api.suggestMapping(p, k);
        const { id } = await api.addSource(p, sug.mapping, k);
        out.push({ job: auto ? await api.importSource(id) : null, label: k ? KIND_LABEL[k] : "", warnings: (sug.warnings ?? []) as string[] });
      }
      return out;
    },
    onSuccess: (r) => {
      setPath(""); scan.reset();
      qc.invalidateQueries({ queryKey: ["sources"] });
      r.forEach((x) => { if (x.job) track(x.job.job, `Import command DB${x.label ? ` (${x.label})` : ""}`); });
    },
  });
  const onPicked = (list: FileList | null) => {
    if (list?.length) upload.mutate(Array.from(list).map((f) => ({ file: f, path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name })));
    if (folderInput.current) folderInput.current.value = "";
    if (filesInput.current) filesInput.current.value = "";
  };
  const sc = scan.data;
  const history = sc?.format === "command_history";
  const counted = (d: ScannedDb, k: CmdKind) => d.commands?.[k];
  const usable = sc ? sc.databases.filter((d) => !d.error && (!history || d.commands)).length : 0;
  return (
    <div className="card">
      <h2>Add command databases</h2>
      <div className="row" style={{ marginBottom: 8 }}>
        <Segmented label="Commands to check" value={kind} onChange={setKind}
          options={[{ value: "nvme", label: "NVMe commands" }, { value: "mi", label: "NVMe-MI commands" }, { value: "both", label: "Both" }]} />
        <span className="muted small">Command-history databases: NVMe reads <code>CommandHistoryTable</code>, NVMe-MI reads <code>MICommandHistoryTable</code>.</span>
      </div>
      <div className="row">
        <input className="grow" placeholder="Path on the server to a database or a folder of databases, e.g. D:\Log\CommandHistory" value={path}
          onChange={(e) => setPath(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") run(path); }} aria-label="Database or folder path" />
        <button onClick={() => setBrowsing(true)}><FolderOpen size={14} /> Browse…</button>
        <button className="primary" disabled={!path.trim() || scan.isPending} onClick={() => run(path)}>{scan.isPending ? "Scanning…" : "Find databases"}</button>
      </div>
      <div className={`dropzone ${over ? "over" : ""}`} style={{ marginTop: 10, marginBottom: 0 }}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); upload.mutate(droppedFiles(e.dataTransfer)); }}>
        <div className="ico">{upload.isPending ? <span className="spinner" /> : <UploadCloud size={20} />}</div>
        <div className="grow">
          <strong>{upload.isPending ? "Copying to the server…" : "Drop a folder or database files here"}</strong>
          <div className="muted small">Sub-folders are included. The databases are copied to the server, then listed below.</div>
        </div>
        <input ref={folderInput} type="file" className="sr-only" tabIndex={-1} aria-label="Folder of databases" onChange={(e) => onPicked(e.target.files)}
          {...({ webkitdirectory: "", directory: "" } as Record<string, string>)} />
        <input ref={filesInput} type="file" multiple accept={DB_SUFFIXES.join(",")} className="sr-only" tabIndex={-1} aria-label="Database files" onChange={(e) => onPicked(e.target.files)} />
        <button disabled={upload.isPending} onClick={() => folderInput.current?.click()}>Upload folder</button>
        <button disabled={upload.isPending} onClick={() => filesInput.current?.click()}>Upload files</button>
      </div>
      <ErrorNote error={scan.error ?? upload.error ?? add.error} />
      {browsing && <FsBrowser start={path.trim()} onClose={() => setBrowsing(false)} onPick={(p) => { setBrowsing(false); run(p); }} />}
      {sc && (
        <div style={{ marginTop: 12 }}>
          <div className="row" style={{ marginBottom: 6 }}>
            <strong className="grow">{sc.count} database{sc.count === 1 ? "" : "s"} {sc.folder ? "in this folder" : ""}{history ? " · command history" : ""}</strong>
            <label className="small"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> import now</label>
            <button className="primary" disabled={!sc.mapping || !usable || add.isPending} onClick={() => add.mutate()}>
              {sc.folder ? `Add the folder (${usable} test${usable === 1 ? "" : "s"})` : "Add"}{kind === "both" && history ? " for NVMe and NVMe-MI" : ""}
            </button>
          </div>
          <div className="muted small" style={{ marginBottom: 6 }}>
            Each database is one test script: coverage is reported for every test on its own as well as for all of them together.
            The test name is the database's path in the folder{sc.mapping?.test && typeof sc.mapping.test === "string" ? ` (or its ${sc.mapping.test} column)` : ""}.
          </div>
          {sc.error && <div className="toast bad">{sc.error}</div>}
          {sc.warnings?.map((w, i) => <div key={i} className="toast bad">Mapping warning: {w}</div>)}
          {kind === "both" && history && sc.kinds && sc.kinds.length < 2 && <div className="toast bad">These databases have no {sc.kinds.includes("mi") ? "CommandHistoryTable" : "MICommandHistoryTable"}: only {KIND_LABEL[sc.kinds[0]]} commands will be added.</div>}
          <div className="scroll" style={{ maxHeight: 320 }}>
            <table className="t">
              <thead><tr><th>Test</th><th>File</th>{history ? <><th>NVMe cmds</th><th>NVMe-MI cmds</th></> : <th>Tables</th>}<th>Size</th></tr></thead>
              <tbody>{sc.databases.map((d) => (
                <tr key={d.path}>
                  <td>{d.test}</td><td className="mono small">{d.rel}</td>
                  {d.error ? <td colSpan={history ? 2 : 1} className="bad small">{d.error}</td>
                    : history ? (d.commands ? <><td className={kinds.includes("nvme") ? "" : "muted"}>{counted(d, "nvme") ?? "–"}</td><td className={kinds.includes("mi") ? "" : "muted"}>{counted(d, "mi") ?? "–"}</td></>
                      : <td colSpan={2} className="muted small">not a command-history database ({d.tables?.join(", ") || "no tables"}): skipped</td>)
                      : <td className="small">{d.tables?.join(", ")}</td>}
                  <td className="small muted">{size(d.size)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {sc.truncated && <div className="muted small">Showing the first {sc.databases.length} of {sc.count}.</div>}
        </div>
      )}
    </div>
  );
}

function Databases({ src }: { src: CmdSource }) {
  const files = src.unknown?.files ?? [];
  if (!files.length) return <div className="muted">{src.status === "imported" ? "No databases were found." : "Import the source to list its databases."}</div>;
  return (
    <>
      <p className="muted small">{files.length} database{files.length === 1 ? "" : "s"}, one test each. Re-import to pick up databases added to the folder since.</p>
      <div className="scroll" style={{ maxHeight: 420 }}>
        <table className="t"><thead><tr><th>Test</th><th>File</th><th>Rows</th><th>Decoded</th><th /></tr></thead>
          <tbody>{files.map((f) => (
            <tr key={f.rel}><td>{f.test}</td><td className="mono small">{f.rel}</td><td>{f.rows}</td><td>{f.decoded}</td>
              <td className="small">{f.error ? <span className="bad">skipped: {f.error}</span> : f.decoded < f.rows ? <span className="bad">{f.rows - f.decoded} undecoded</span> : ""}</td></tr>
          ))}</tbody></table>
      </div>
    </>
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
  const [tab, setTab] = useState<"commands" | "databases" | "mapping" | "problems">("commands");
  const list = sources.data ?? [];
  const src = list.find((s) => s.id === id) ?? list[0];
  return (
    <>
      <PageHeader title="Command DBs" description="SQLite databases of the commands your test scripts submitted: one database, or a folder of them (one per test script). Review the column mapping, import, browse what was decoded, then analyse it on the Scenarios page and run coverage, per test and overall." />
      <Add />
      <Loading q={sources} />
      {sources.data && list.length === 0 && <Empty icon={Database} title="No command databases yet">Add a database or a folder of databases above.</Empty>}
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
            {(["commands", "databases", "mapping", "problems"] as const).map((t) => (
              <button key={t} className={tab === t ? "active" : ""} onClick={() => setTab(t)}>
                {t === "commands" ? "Decoded commands" : t === "databases" ? `Databases${src.unknown?.files ? ` (${src.unknown.files.length}${src.unknown.failed_files ? `, ${src.unknown.failed_files} skipped` : ""})` : ""}` : t === "mapping" ? "Column mapping" : `Undecoded rows${src.unknown?.undecodable_rows ? ` (${src.unknown.undecodable_rows})` : ""}`}
              </button>
            ))}
          </div>
          {tab === "commands" && <Browser src={src} />}
          {tab === "databases" && <Databases src={src} />}
          {tab === "mapping" && <Mapping src={src} />}
          {tab === "problems" && (
            src.unknown?.errors?.length
              ? <div className="scroll" style={{ maxHeight: 360 }}>{src.unknown.errors.length < (src.unknown.undecodable_rows ?? 0) && <div className="muted small">Showing the first {src.unknown.errors.length} of {src.unknown.undecodable_rows} failures.</div>}{src.unknown.errors.map((e, i) => <div key={i} className="mono small">{e.file ? `${e.file} ` : ""}row {typeof e.row === "object" ? JSON.stringify(e.row) : String(e.row)}: {e.error}</div>)}</div>
              : <div className="muted">{src.status === "imported" ? "Every row decoded." : "Import the source to see decoding problems."}</div>
          )}
        </div>
      )}
    </>
  );
}
