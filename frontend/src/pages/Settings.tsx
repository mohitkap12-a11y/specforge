import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { api, type BackupItem } from "../api";
import { useJobs } from "../components/jobs";
import type { ResetScope } from "../types";
import { ErrorNote, Field, Loading, PageHeader, Segmented, Switch } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useAuth } from "../components/auth";
import { useTheme, type ThemePref } from "../theme";
import { setIn } from "../util";

type S = Record<string, any>;
const TASKS = [["classify", "Classify", "Labels items (reserved for future use)"], ["generate", "Draft scenarios", "Reads each normative statement and proposes test scenarios"], ["embed", "Embeddings", "Semantic search (reserved for future use)"]] as const;
const SECTIONS = [["ai", "AI providers"], ["models", "Models"], ["offline", "Offline models"], ["parser", "Parser"], ["logging", "Logging"], ["mi", "NVMe-MI layout"], ["rollout", "Feature rollout"], ["backup", "Backup / Export-Import"], ["appearance", "Appearance"], ["reset", "Reset"], ["about", "About"]] as const;
/** What a non-admin sees: the rest are server-wide and only an admin can change them. */
const PERSONAL_SECTIONS = SECTIONS.filter(([id]) => id === "appearance" || id === "about");

function Section({ id, title, description, children }: { id: string; title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} className="card">
      <h2>{title}</h2>
      {description && <p className="muted">{description}</p>}
      <fieldset className="plain">{children}</fieldset>
    </section>
  );
}

function SectionNav({ sections, hash }: { sections: readonly (readonly [string, string])[]; hash: string }) {
  return (
    <nav aria-label="Settings sections">
      {sections.map(([id, label]) => <Link key={id} to={{ hash: `#${id}` }} className={hash === id ? "on" : ""}>{label}</Link>)}
    </nav>
  );
}

function Appearance() {
  const [theme, setTheme] = useTheme();
  return (
    <Section id="appearance" title="Appearance">
      <Segmented<ThemePref> label="Theme" value={theme} onChange={setTheme} options={[{ value: "system", label: "System" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />
      <p className="muted small" style={{ marginTop: 8 }}>Stored in this browser only. Tip: press <span className="kbd">⌘ K</span> / <span className="kbd">Ctrl K</span> anywhere to jump to a page.</p>
    </Section>
  );
}

function About() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  return (
    <Section id="about" title="About">
      {health.data && (
        <dl className="logdetail kv" style={{ margin: 0, border: 0, padding: 0, background: "none" }}>
          <dt>Version</dt><dd>{health.data.version}</dd>
          <dt>Parser version</dt><dd>{health.data.parser_version}</dd>
          <dt>Workspace</dt><dd className="mono">{health.data.workspace}</dd>
          <dt>Reparse available</dt><dd>{health.data.reparse_available.length ? `${health.data.reparse_available.length} document(s)` : "none"}</dd>
        </dl>
      )}
    </Section>
  );
}

/** Every item the given ones need (an export always includes them; the server adds them too). */
function withRequirements(ids: string[], all: BackupItem[]): Set<string> {
  const byId = new Map(all.map((o) => [o.id, o]));
  const out = new Set<string>();
  const todo = [...ids];
  while (todo.length) {
    const id = todo.pop()!;
    if (out.has(id)) continue;
    out.add(id);
    todo.push(...(byId.get(id)?.requires ?? []));
  }
  return out;
}

function BackupChecklist({ items, selected, onChange }: { items: BackupItem[]; selected: Set<string>; onChange: (s: Set<string>) => void }) {
  const label = new Map(items.map((o) => [o.id, o.label]));
  const toggle = (id: string) => {
    if (selected.has(id)) {
      // also untick what needs it: it cannot be exported or imported on its own
      const keep = [...selected].filter((k) => k !== id && !withRequirements([k], items).has(id));
      onChange(new Set(keep));
    } else onChange(withRequirements([...selected, id], items));
  };
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {items.map((o) => (
        <label key={o.id} className="row" style={{ alignItems: "center", gap: 8 }}>
          <input type="checkbox" checked={selected.has(o.id)} onChange={() => toggle(o.id)} />
          <span>{o.label}</span>
          {o.requires.length > 0 && <span className="muted small">needs {o.requires.map((r) => label.get(r) ?? r).join(", ")}</span>}
        </label>
      ))}
    </div>
  );
}

function BackupPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const opts = useQuery({ queryKey: ["backup-options"], queryFn: api.backupOptions });
  const items = opts.data?.items ?? [];
  const [exportSel, setExportSel] = useState<Set<string>>(new Set());
  const [includeSecrets, setIncludeSecrets] = useState(false);
  // large items (the Docling models) are offered but not ticked
  useEffect(() => { if (opts.data) setExportSel(new Set(opts.data.items.filter((o) => o.default !== false).map((o) => o.id))); }, [opts.data]);

  const [file, setFile] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);          // bumping it remounts the file input, which clears it
  const [chooseItems, setChooseItems] = useState(false);
  const [importSel, setImportSel] = useState<Set<string>>(new Set());
  const [replace, setReplace] = useState(false);

  const exp = useMutation({
    mutationFn: () => api.exportBackup([...exportSel], includeSecrets),
    onSuccess: () => toast.push({ title: "Backup downloaded", tone: "ok" }),
  });
  const imp = useMutation({
    mutationFn: () => {
      if (!file) throw new Error("Select a backup zip to import");
      return api.importBackup(file, chooseItems ? [...importSel] : undefined, replace);
    },
    onSuccess: (r) => {
      qc.invalidateQueries();
      const what = r.items.map((id) => items.find((o) => o.id === id)?.label ?? id).join(", ");
      toast.push({ title: "Backup imported", description: `${what}: ${Object.values(r.rows).reduce((a, b) => a + b, 0)} rows, ${r.files} files`
        + (r.warnings.length ? `. ${r.warnings.join(" ")}` : ""), tone: r.warnings.length ? "info" : "ok" });
      setFile(null);
      setFileKey((k) => k + 1);
    },
  });
  const startImport = () => {
    if (replace && !window.confirm("Replace existing data? What this workspace has for the imported items is deleted first "
      + "(user accounts: every one but yours). This cannot be undone.")) return;
    imp.mutate();
  };

  if (opts.isLoading || opts.error) return <Loading q={opts} />;
  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div>
        <h3 style={{ marginTop: 0 }}>Export</h3>
        <p className="muted small">Download a zip of the selected data, e.g. to move this server to a new machine. Items it depends on are included.
          With User accounts the zip holds password hashes: keep it somewhere safe.</p>
        <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 8 }}>
          <button className="sm" type="button" onClick={() => setExportSel(new Set(items.map((o) => o.id)))}>Select all</button>
          <button className="sm" type="button" onClick={() => setExportSel(new Set())}>Clear</button>
        </div>
        <BackupChecklist items={items} selected={exportSel} onChange={setExportSel} />
        <label className="row" style={{ alignItems: "center", gap: 8, marginTop: 8 }}>
          <Switch label="Include secrets such as API keys in the settings" checked={includeSecrets} onChange={setIncludeSecrets} />
          <span className="small">Include secrets (API keys) in the settings</span>
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="primary" onClick={() => exp.mutate()} disabled={exportSel.size === 0 || exp.isPending}>
            {exp.isPending && <span className="spinner" />}Export selected as zip</button>
        </div>
        <ErrorNote error={exp.error} />
      </div>
      <div className="divider" />
      <div>
        <h3 style={{ marginTop: 0 }}>Import</h3>
        <p className="muted small">Import a backup exported by this version of SpecForge. It is applied completely or not at all,
          and is refused while jobs are running. Paths into the old workspace are moved to this one; the MCP search index is rebuilt.</p>
        <div className="fields" style={{ alignItems: "center" }}>
          <Field label="Backup zip file"><input key={fileKey} type="file" accept=".zip,application/zip" onChange={(e) => setFile(e.target.files?.[0] || null)} /></Field>
        </div>
        <label className="row" style={{ alignItems: "center", gap: 8, marginTop: 8 }}>
          <Switch label="Import only some items" checked={chooseItems} onChange={(v) => { setChooseItems(v); if (v && importSel.size === 0) setImportSel(new Set(items.map((o) => o.id))); }} />
          <span className="small">{chooseItems ? "Import only the items ticked below" : "Import everything the backup holds"}</span>
        </label>
        {chooseItems && <div style={{ marginTop: 8 }}><BackupChecklist items={items} selected={importSel} onChange={setImportSel} /></div>}
        <label className="row" style={{ alignItems: "center", gap: 8, marginTop: 8 }}>
          <Switch label="Replace existing data" checked={replace} onChange={setReplace} />
          <span className="small">Replace existing data {replace ? "(deleted first)" : "(off: merge; refused where this workspace already has specs, sets, comparisons, coverage or jobs)"}</span>
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          <button className={replace ? "danger" : "primary"} onClick={startImport} disabled={!file || (chooseItems && importSel.size === 0) || imp.isPending}>
            {imp.isPending && <span className="spinner" />}{replace ? "Replace and import" : "Import"}</button>
        </div>
        <ErrorNote error={imp.error} />
      </div>
    </div>
  );
}

const mb = (b: number) => (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${(b / 1e6).toFixed(0)} MB`);

/** Offline mode and the Docling model files; applies at once, not with the save bar. */
function OfflinePanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const { track } = useJobs();
  const q = useQuery({ queryKey: ["offline"], queryFn: api.offline, refetchInterval: (r) => (r.state.data?.downloading ? 3000 : 15000) });
  const [path, setPath] = useState<string | null>(null);
  const st = q.data;
  const set = useMutation({
    mutationFn: api.setOffline,
    onSuccess: (r, b) => {
      qc.setQueryData(["offline"], r);
      if (b.models_path !== undefined) setPath(null);
      toast.push({ title: b.enabled === undefined ? "Models folder saved" : b.enabled ? "Offline mode is on" : "Offline mode is off: this server may reach the internet", tone: b.enabled === false ? "info" : "ok" });
    },
  });
  const dl = useMutation({
    mutationFn: api.downloadModels,
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["offline"] });
      track(r.job, "Download the Docling models", (j) => {
        qc.invalidateQueries({ queryKey: ["offline"] });
        if (j.state === "done") toast.push({ title: "Docling models downloaded", description: "Turn Offline on again to block internet access.", tone: "ok" });
      });
    },
  });
  if (q.isLoading || q.error || !st) return <Section id="offline" title="Offline models"><Loading q={q} /></Section>;
  const change = (v: boolean) => {
    if (!v && !window.confirm("Turn Offline off? Until you turn it on again this server may contact the internet, e.g. Hugging Face to download or check Docling models.")) return;
    if (v && !st.downloaded && !st.env_override && !window.confirm("The Docling models are not downloaded yet. With Offline on, To Markdown uses the built-in engine (tables may be less accurate). Turn Offline on anyway?")) return;
    set.mutate({ enabled: v });
  };
  const editing = path ?? st.models_path;
  return (
    <Section id="offline" title="Offline models" description={<>Keeps this server off the internet. To Markdown's Docling engine needs model files
      (layout, tables, OCR). Download them once into a folder here and Docling then loads them only from that folder. Your documents are never uploaded,
      even while Offline is off.</>}>
      <div className="row" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
        <Switch label="Offline mode" checked={st.offline} onChange={(v) => !set.isPending && change(v)} />
        <div>
          <strong>Offline</strong> <span className={`badge ${st.offline ? "ok" : "warn"}`}>{st.offline ? "On" : "Off: internet allowed"}</span>
          <div className="muted small">{st.offline
            ? "No Hugging Face downloads or update checks, and the server refuses connections to public internet addresses. Local and LAN addresses (e.g. Ollama) still work."
            : "Docling may download or check models on Hugging Face. Download the models below, then turn Offline on again."}</div>
          {st.guard.blocked_total > 0 && <div className="small" style={{ marginTop: 4 }}>
            Blocked {st.guard.blocked_total} connection attempt(s) since the server started{st.guard.recent[0] ? `, latest to ${st.guard.recent[0].target}` : ""}.</div>}
        </div>
      </div>
      <div className="divider" />
      <div className="fields" style={{ alignItems: "flex-end" }}>
        <Field label="Models folder" hint={`Absolute path on the server; empty: ${st.default_folder}`}>
          <input value={editing} placeholder={st.default_folder} onChange={(e) => setPath(e.target.value)} />
        </Field>
        {path !== null && path !== st.models_path && <div className="row">
          <button className="sm primary" disabled={set.isPending} onClick={() => set.mutate({ models_path: path })}>Save folder</button>
          <button className="sm ghost" onClick={() => setPath(null)}>Cancel</button>
        </div>}
      </div>
      <dl className="logdetail kv" style={{ margin: "12px 0 0", border: 0, padding: 0, background: "none" }}>
        <dt>Folder</dt><dd className="mono">{st.folder}</dd>
        <dt>Models</dt><dd>{st.downloaded
          ? <><span className="badge ok">Downloaded</span> {st.downloaded.downloaded ?? ""} · {st.files} files · {mb(st.bytes)}{st.downloaded.docling_version ? ` · Docling ${st.downloaded.docling_version}` : ""}</>
          : <span className="badge warn">Not downloaded{st.files ? ` (incomplete: ${st.files} files, ${mb(st.bytes)})` : ""}</span>}</dd>
        <dt>Docling loads from</dt><dd className="mono">{st.loading_from ?? (st.offline ? "Hugging Face cache (offline: only files already on this machine)" : "Hugging Face (downloads when needed)")}</dd>
        {st.env_override && <><dt>Override</dt><dd className="small">DOCLING_ARTIFACTS_PATH is set on the server ({st.env_override}) and is used instead of the folder above.</dd></>}
        <dt>Docling</dt><dd>{st.docling_available ? `installed${st.docling_version ? ` (${st.docling_version})` : ""}` : <span className="badge bad">not installed: To Markdown uses the built-in engine</span>}</dd>
      </dl>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="primary" disabled={st.offline || !st.docling_available || st.downloading || dl.isPending || (path !== null && path !== st.models_path)}
          onClick={() => dl.mutate()}>{(st.downloading || dl.isPending) && <span className="spinner" />}{st.downloaded ? "Download again" : "Download models"}</button>
        <span className="muted small">{st.offline ? "Turn Offline off to download." : "Fetches every model To Markdown uses (several hundred MB), then checks they load from the folder."}</span>
      </div>
      <p className="muted small" style={{ marginTop: 8 }}>To move the models to another server, export a backup with <em>Docling models</em> ticked and import it there.</p>
      <ErrorNote error={set.error ?? dl.error} />
    </Section>
  );
}

/** Staged rollout: the Coverage Tool is an admin-only preview until it is released here; applies at once, not with the save bar. */
function Rollout() {
  const qc = useQueryClient();
  const toast = useToast();
  const { me } = useAuth();
  const released = !!me.features?.coverage_released;
  const flip = useMutation({
    mutationFn: (v: boolean) => api.setFeatures({ coverage_released: v }),
    onSuccess: (_, v) => {
      qc.invalidateQueries({ queryKey: ["me"] });
      toast.push({ title: v ? "Coverage Tool released to every user" : "Coverage Tool is admin-only again", tone: "ok" });
    },
  });
  const change = (v: boolean) => {
    if (window.confirm(v ? "Release the Coverage Tool to every user? Its pages appear in everyone's menu right away."
      : "Take the Coverage Tool back to an admin-only preview? Other users see it as coming soon again.")) flip.mutate(v);
  };
  return (
    <Section id="rollout" title="Feature rollout" description="Tools still being tried out internally. Until you release one, only admins can use it; everyone else sees it as coming soon.">
      <div className="row" style={{ flexWrap: "nowrap", alignItems: "flex-start" }}>
        <Switch label="Release the Coverage Tool to every user" checked={released} onChange={(v) => !flip.isPending && change(v)} />
        <div>
          <strong>Coverage Tool</strong> <span className={`badge ${released ? "ok" : "warn"}`}>{released ? "Released" : "Admin preview"}</span>
          <div className="muted small">{released
            ? "Every user sees the Coverage Tool and all its pages in the menu."
            : "Only admins see the Workflow, Sets, Catalog, Command DBs, Scenarios, Coverage and Reports pages. Specs and Explorer stay open to everyone (Spec Compare uses them)."}</div>
        </div>
      </div>
      <ErrorNote error={flip.error} />
    </Section>
  );
}

function Models({ provider }: { provider: string }) {
  const [go, setGo] = useState(false);
  const q = useQuery({ queryKey: ["models", provider], queryFn: () => api.providerModels(provider), enabled: go, staleTime: 0 });
  return (
    <div style={{ marginTop: 12 }}>
      <div className="row">
        <button className="sm" onClick={() => (go ? q.refetch() : setGo(true))} disabled={q.isFetching}>{q.isFetching && <span className="spinner" />}Test connection</button>
        {q.data?.ok && <span className="badge ok">Connected · {q.data.models.length} models</span>}
        {q.data && !q.data.ok && <span className="badge bad">{q.data.error}</span>}
      </div>
      {q.data?.ok && q.data.models.length > 0 && <details style={{ marginTop: 8 }}><summary className="small muted" style={{ cursor: "pointer" }}>Available models</summary><div className="mono small" style={{ marginTop: 6 }}>{q.data.models.map((m) => <div key={m.id}>{m.id}</div>)}</div></details>}
      <ErrorNote error={q.error} />
      <p className="muted small" style={{ marginTop: 8 }}>The test uses the saved settings. Save first if you changed the connection details.</p>
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const loc = useLocation();
  const { isAdmin } = useAuth();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings, enabled: isAdmin });
  const [s, setS] = useState<S | null>(null);
  const [layout, setLayout] = useState("{}");
  useEffect(() => { if (q.data) { setS(q.data); setLayout(JSON.stringify(q.data.mi_layout ?? {}, null, 2)); } }, [q.data]);
  useEffect(() => { if (loc.hash && s) document.getElementById(loc.hash.slice(1))?.scrollIntoView({ block: "start" }); }, [loc.hash, s === null]);   // eslint-disable-line react-hooks/exhaustive-deps

  let layoutObj: unknown, layoutErr: string | null = null;
  try { layoutObj = JSON.parse(layout); if (typeof layoutObj !== "object" || layoutObj === null || Array.isArray(layoutObj)) layoutErr = "must be a JSON object"; } catch (e) { layoutErr = (e as Error).message; }
  const save = useMutation({
    mutationFn: (b: S) => api.saveSettings(b),
    onSuccess: () => { qc.invalidateQueries(); toast.push({ title: "Settings saved", tone: "ok" }); },
  });
  if (!isAdmin) {
    const hash = loc.hash.replace("#", "") || "appearance";
    return (
      <>
        <PageHeader title="Settings" description="Set how the app looks. Everything stays on this machine." />
        <div className="settings">
          <SectionNav sections={PERSONAL_SECTIONS} hash={hash} />
          <div><Appearance /><About /></div>
        </div>
      </>
    );
  }
  if (q.isLoading || q.error || !s) return <><PageHeader title="Settings" /><Loading q={q} /></>;

  const set = (path: string[], v: unknown) => setS((cur) => setIn(cur ?? {}, path, v));
  const num = (path: string[]) => (v: string) => set(path, v === "" ? "" : Number(v));
  const dirty = JSON.stringify({ ...s, mi_layout: layoutObj }) !== JSON.stringify(q.data);
  const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
  const isInt = (v: unknown, min: number) => isNum(v) && Number.isInteger(v) && v >= min;
  const invalidNum = !(isNum(s.ollama?.timeout) && s.ollama.timeout > 0 && isNum(s.opencode?.timeout) && s.opencode.timeout > 0
    && isInt(s.ollama?.num_ctx, 1) && isInt(s.parser?.workers, 0) && isInt(s.llm?.max_concurrency, 1) && isInt(s.llm?.max_retries, 0)
    && isInt(s.logging?.preview_chars, 200));
  const discard = () => { setS(q.data!); setLayout(JSON.stringify(q.data!.mi_layout ?? {}, null, 2)); };
  const hash = loc.hash.replace("#", "") || "ai";

  return (
    <>
      <PageHeader title="Settings" description="Connect your local AI, tune the parser, choose what gets logged, and set how the app looks. Everything stays on this machine." />
      <div className="settings">
        <SectionNav sections={SECTIONS} hash={hash} />
        <div>
          <Section id="ai" title="AI providers" description="Used only to draft scenarios. Providers run locally; nothing is sent to the cloud by SpecForge.">
            <h3 style={{ marginTop: 0 }}>Ollama</h3>
            <div className="fields">
              <Field label="Base URL"><input value={s.ollama?.base_url ?? ""} onChange={(e) => set(["ollama", "base_url"], e.target.value)} /></Field>
              <Field label="Context window" hint="Tokens; match what the model was loaded with"><input type="number" value={s.ollama?.num_ctx ?? ""} onChange={(e) => num(["ollama", "num_ctx"])(e.target.value)} /></Field>
              <Field label="Timeout (seconds)"><input type="number" value={s.ollama?.timeout ?? ""} onChange={(e) => num(["ollama", "timeout"])(e.target.value)} /></Field>
            </div>
            <Models provider="ollama" />
            <div className="divider" />
            <h3 style={{ marginTop: 0 }}>OpenCode</h3>
            <div className="fields">
              <Field label="Binary" hint="Command name or full path"><input value={s.opencode?.binary ?? ""} onChange={(e) => set(["opencode", "binary"], e.target.value)} /></Field>
              <Field label="Timeout (seconds)"><input type="number" value={s.opencode?.timeout ?? ""} onChange={(e) => num(["opencode", "timeout"])(e.target.value)} /></Field>
            </div>
            <Models provider="opencode" />
          </Section>

          <Section id="models" title="Models" description="Which provider and model handles each task.">
            <div className="scroll"><table className="t">
              <thead><tr><th>Task</th><th>Provider</th><th>Model</th><th>Temperature</th></tr></thead>
              <tbody>{TASKS.map(([k, label, hint]) => (
                <tr key={k}>
                  <td><strong>{label}</strong><div className="muted small">{hint}</div></td>
                  <td><select value={s.tasks?.[k]?.provider ?? "ollama"} onChange={(e) => set(["tasks", k, "provider"], e.target.value)} aria-label={`${label} provider`}><option>ollama</option><option>opencode</option></select></td>
                  <td><input value={s.tasks?.[k]?.model ?? ""} onChange={(e) => set(["tasks", k, "model"], e.target.value)} placeholder="model id" aria-label={`${label} model`} /></td>
                  <td>{k === "embed" ? <span className="muted">n/a</span> : <input type="number" step="0.1" min="0" max="2" style={{ width: 90 }} value={s.tasks?.[k]?.temperature ?? 0}
                    onChange={(e) => set(["tasks", k, "temperature"], e.target.value === "" ? 0 : Number(e.target.value))} aria-label={`${label} temperature`} />}</td>
                </tr>
              ))}</tbody>
            </table></div>
            <div className="fields" style={{ marginTop: 16 }}>
              <Field label="Concurrent model calls" hint="Whole number ≥ 1"><input type="number" value={s.llm?.max_concurrency ?? ""} onChange={(e) => num(["llm", "max_concurrency"])(e.target.value)} /></Field>
              <Field label="Retries on invalid output" hint="Whole number ≥ 0; the model is asked to repair its answer"><input type="number" value={s.llm?.max_retries ?? ""} onChange={(e) => num(["llm", "max_retries"])(e.target.value)} /></Field>
            </div>
          </Section>

          <OfflinePanel />

          <Section id="parser" title="Parser" description="Changing parser options affects the next parse. Existing runs are kept.">
            <div className="fields">
              <Field label="Worker processes" hint="0 = CPU count − 1"><input type="number" value={s.parser?.workers ?? ""} onChange={(e) => num(["parser", "workers"])(e.target.value)} /></Field>
              <Field label="Table strategy"><input value={s.parser?.table_strategy ?? ""} onChange={(e) => set(["parser", "table_strategy"], e.target.value)} /></Field>
            </div>
          </Section>

          <Section id="logging" title="Logging" description={<>What the <Link to="/activity">Activity</Link> view records. Applies immediately after saving.</>}>
            <div className="fields">
              <Field label="Minimum level" hint="Debug adds cache hits and progress heartbeats">
                <select value={s.logging?.min_level ?? "info"} onChange={(e) => set(["logging", "min_level"], e.target.value)}>
                  <option value="debug">Debug (everything)</option><option value="info">Info (recommended)</option><option value="warn">Warnings and errors</option><option value="error">Errors only</option>
                </select>
              </Field>
              <Field label="Prompt/reply preview length" hint="Characters kept per prompt message and reply (200 or more)"><input type="number" value={s.logging?.preview_chars ?? ""} onChange={(e) => num(["logging", "preview_chars"])(e.target.value)} /></Field>
            </div>
            <div className="row" style={{ marginTop: 16, flexWrap: "nowrap", alignItems: "flex-start" }}>
              <Switch label="Record prompts and model replies" checked={s.logging?.capture_prompts ?? true} onChange={(v) => set(["logging", "capture_prompts"], v)} />
              <div><strong>Record prompts and model replies</strong><div className="muted small">Lets you see exactly what each AI call was asked and what it answered. They can contain spec text; they stay in your local workspace database.</div></div>
            </div>
          </Section>

          <Section id="mi" title="NVMe-MI message layout" description={<>Overrides merged over the built-in default used to decode NVMe-MI messages in command databases. <strong>Verify against the MI revision under test</strong>, then re-import the command DB. Only the keys you list are overridden, e.g. <code>{`{"mi": {"opcode_byte": 4}}`}</code>.</>}>
            <textarea rows={10} spellCheck={false} value={layout} onChange={(e) => setLayout(e.target.value)} aria-label="MI layout overrides" />
            {layoutErr && <div className="toast bad">Invalid: {layoutErr}</div>}
          </Section>

          <Rollout />

          <Section id="backup" title="Backup / Export-Import" description="Export selected data to move this server to a new machine, or import such a backup. Only admins see this.">
            <BackupPanel />
          </Section>

          <Appearance />

          <Section id="reset" title="Reset" description="Start over in one area. Each reset permanently deletes what it lists; your settings and the user accounts are always kept. Only admins see this.">
            <ResetPanel />
          </Section>

          <About />
        </div>
      </div>

      <ErrorNote error={save.error} />
      {dirty && (
        <div className="savebar" role="region" aria-label="Unsaved changes">
          <span>{layoutErr ? "Fix the MI layout JSON to save." : invalidNum ? "Check the numbers: timeouts positive; context and concurrency ≥ 1; retries, workers ≥ 0; preview ≥ 200." : "You have unsaved changes."}</span>
          <button className="ghost" onClick={discard}>Discard</button>
          <button className="primary" disabled={!!layoutErr || invalidNum || save.isPending} onClick={() => save.mutate({ ...s, mi_layout: layoutObj })}>Save changes</button>
        </div>
      )}
    </>
  );
}

const RESETS: { scope: ResetScope; title: string; text: string }[] = [
  { scope: "comparisons", title: "Comparisons", text: "Saved version comparisons and their AI summaries. Nothing else changes." },
  { scope: "coverage", title: "Coverage", text: "What came from test executions: imported command DBs, the scenarios derived from them, and coverage runs. Specs, sets, catalogs and drafted scenarios are kept." },
  { scope: "specs", title: "Specs", text: "Specs, parse runs, spec sets, catalogs and scenarios. Comparisons and coverage runs are built on the specs, so they are deleted too. The To Markdown library and imported command DBs are kept: a spec added again is not converted again." },
  { scope: "all", title: "Everything", text: "All of the above, plus imported command DBs, the To Markdown library, job history, the activity log and the AI cache." },
];
const COUNT_LABELS: Record<string, string> = {
  specs: "specs", parse_runs: "parse runs", sets: "spec sets", scenarios: "scenarios", comparisons: "comparisons", coverage_runs: "coverage runs",
  command_dbs: "command DBs", executed_scenarios: "scenarios derived from executions", converted_files: "converted files (To Markdown)", jobs: "jobs", log_events: "log events", mcp_calls: "MCP calls recorded",
};
const CONFIRM_WORD = "reset";

/** Pick an area, see what would be deleted (and what is still running there), type the word, reset. */
function ResetPanel() {
  const qc = useQueryClient();
  const toast = useToast();
  const [scope, setScope] = useState<ResetScope>("comparisons");
  const [word, setWord] = useState("");
  const preview = useQuery({ queryKey: ["reset-preview", scope], queryFn: () => api.resetPreview(scope), refetchInterval: 5000 });
  const chosen = RESETS.find((r) => r.scope === scope)!;
  const run = useMutation({
    mutationFn: () => api.resetWorkspace(scope),
    onSuccess: () => { setWord(""); qc.invalidateQueries(); toast.push({ title: `${chosen.title} reset`, tone: "ok" }); },
  });
  const counts = Object.entries(preview.data?.counts ?? {});
  const blocked = preview.data?.blocked_by ?? [];
  const empty = counts.length > 0 && counts.every(([, n]) => n === 0);
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div role="radiogroup" aria-label="What to reset" className="reset-options">
        {RESETS.map((r) => (
          <label key={r.scope} className={`reset-option ${scope === r.scope ? "on" : ""}`}>
            <input type="radio" name="reset-scope" checked={scope === r.scope} onChange={() => { setScope(r.scope); setWord(""); run.reset(); }} />
            <span><strong>{r.title}</strong><span className="muted small">{r.text}</span></span>
          </label>
        ))}
      </div>
      <div className="small">
        {preview.isLoading ? <span className="muted">Counting…</span> : empty ? <span className="muted">Nothing to delete here.</span> : (
          <>Deletes {counts.filter(([, n]) => n > 0).map(([k, n]) => `${n} ${COUNT_LABELS[k] ?? k}`).join(", ")}.</>
        )}
      </div>
      {blocked.length > 0 && (
        <div className="toast bad" role="alert">
          {blocked.length} job{blocked.length > 1 ? "s" : ""} of this area {blocked.length > 1 ? "are" : "is"} still queued or running: {blocked.slice(0, 3).map((j) => `${j.label}${j.user ? ` (${j.user})` : ""}`).join(", ")}{blocked.length > 3 ? "…" : ""}. Cancel {blocked.length > 1 ? "them" : "it"} from the bell or wait, then reset.
        </div>
      )}
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <label className="field" style={{ minWidth: 220 }}>
          <span>Type <span className="kbd">{CONFIRM_WORD}</span> to confirm</span>
          <input value={word} onChange={(e) => setWord(e.target.value)} autoComplete="off" spellCheck={false} aria-label={`Type ${CONFIRM_WORD} to confirm`} />
        </label>
        <button className="danger" style={{ alignSelf: "end" }} disabled={run.isPending || word.trim().toLowerCase() !== CONFIRM_WORD || blocked.length > 0 || empty}
          onClick={() => run.mutate()}>
          {run.isPending && <span className="spinner" />}Reset {chosen.title.toLowerCase()}
        </button>
      </div>
      <ErrorNote error={run.error} />
    </div>
  );
}
