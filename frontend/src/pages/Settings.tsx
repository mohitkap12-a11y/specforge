import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { api } from "../api";
import { ErrorNote, Field, Loading, PageHeader, Segmented, Switch } from "../components/ui";
import { useToast } from "../components/Toasts";
import { useTheme, type ThemePref } from "../theme";
import { setIn } from "../util";

type S = Record<string, any>;
const TASKS = [["classify", "Classify", "Labels items (reserved for future use)"], ["generate", "Draft scenarios", "Reads each normative statement and proposes test scenarios"], ["embed", "Embeddings", "Semantic search (reserved for future use)"]] as const;
const SECTIONS = [["ai", "AI providers"], ["models", "Models"], ["parser", "Parser"], ["logging", "Logging"], ["mi", "NVMe-MI layout"], ["appearance", "Appearance"], ["reset", "Reset"], ["about", "About"]] as const;

function Section({ id, title, description, children }: { id: string; title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <section id={id} className="card">
      <h2>{title}</h2>
      {description && <p className="muted">{description}</p>}
      {children}
    </section>
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
  const [theme, setTheme] = useTheme();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
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
  const reset = useMutation({
    mutationFn: () => api.resetWorkspace(),
    onSuccess: () => { qc.invalidateQueries(); toast.push({ title: "Workspace reset", tone: "ok" }); },
  });
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
        <nav aria-label="Settings sections">
          {SECTIONS.map(([id, label]) => <Link key={id} to={{ hash: `#${id}` }} className={hash === id ? "on" : ""}>{label}</Link>)}
        </nav>
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
            <div className="row" style={{ marginTop: 16, flexWrap: "nowrap", alignItems: "flex-start" }}>
              <Switch label="Print full prompts to the server log" checked={s.logging?.print_prompts ?? true} onChange={(v) => set(["logging", "print_prompts"], v)} />
              <div><strong>Print full prompts to the server log</strong><div className="muted small">Every prompt sent to a model and every reply, in full (not shortened), is printed in the terminal running <code>specforge serve</code> and appended to <code>logs/llm-prompts.log</code> in the workspace.</div></div>
            </div>
          </Section>

          <Section id="mi" title="NVMe-MI message layout" description={<>Overrides merged over the built-in default used to decode NVMe-MI messages in command databases. <strong>Verify against the MI revision under test</strong>, then re-import the command DB. Only the keys you list are overridden, e.g. <code>{`{"mi": {"opcode_byte": 4}}`}</code>.</>}>
            <textarea rows={10} spellCheck={false} value={layout} onChange={(e) => setLayout(e.target.value)} aria-label="MI layout overrides" />
            {layoutErr && <div className="toast bad">Invalid: {layoutErr}</div>}
          </Section>

          <Section id="appearance" title="Appearance">
            <Segmented<ThemePref> label="Theme" value={theme} onChange={setTheme} options={[{ value: "system", label: "System" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" }]} />
            <p className="muted small" style={{ marginTop: 8 }}>Stored in this browser only. Tip: press <span className="kbd">⌘ K</span> / <span className="kbd">Ctrl K</span> anywhere to jump to a page.</p>
          </Section>

          <Section id="reset" title="Reset workspace" description="Start over: permanently deletes all uploaded specs and parse runs, spec sets, catalogs, scenarios, command databases, coverage runs, jobs, the activity log and the AI cache. Your settings above are kept.">
            <button className="danger" disabled={reset.isPending} onClick={() => { if (confirm("Delete everything created so far (specs, sets, scenarios, command DBs, coverage runs, logs)? This cannot be undone. Settings are kept.")) reset.mutate(); }}>
              {reset.isPending && <span className="spinner" />}Reset everything
            </button>
            <ErrorNote error={reset.error} />
          </Section>

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
