import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ErrorNote, Loading } from "../components/ui";
import { setIn } from "../util";

type S = Record<string, any>;
const TASKS = [["classify", "Classify"], ["generate", "Generate scenarios"], ["embed", "Embeddings"]] as const;

function Field({ label, value, onChange, type = "text", width = 24 }: { label: string; value: unknown; onChange: (v: string) => void; type?: string; width?: number }) {
  return <label className="row">{label}<input type={type} size={width} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} /></label>;
}

function Models({ provider }: { provider: string }) {
  const [go, setGo] = useState(false);
  const q = useQuery({ queryKey: ["models", provider], queryFn: () => api.providerModels(provider), enabled: go, staleTime: 0 });
  return (
    <div className="small">
      <button onClick={() => (go ? q.refetch() : setGo(true))} disabled={q.isFetching}>Test connection</button>{" "}
      {q.data?.ok && <span className="badge ok">{q.data.models.length} models</span>}
      {q.data && !q.data.ok && <span className="badge bad">{q.data.error}</span>}
      {q.data?.ok && <details><summary>Models</summary><div className="mono small">{q.data.models.map((m) => <div key={m.id}>{m.id}</div>)}</div></details>}
      <ErrorNote error={q.error} />
    </div>
  );
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const [s, setS] = useState<S | null>(null);
  const [layout, setLayout] = useState("{}");
  useEffect(() => { if (q.data) { setS(q.data); setLayout(JSON.stringify(q.data.mi_layout ?? {}, null, 2)); } }, [q.data]);
  let layoutObj: unknown, layoutErr: string | null = null;
  try { layoutObj = JSON.parse(layout); if (typeof layoutObj !== "object" || layoutObj === null || Array.isArray(layoutObj)) layoutErr = "must be a JSON object"; } catch (e) { layoutErr = (e as Error).message; }
  const save = useMutation({ mutationFn: (b: S) => api.saveSettings(b), onSuccess: () => qc.invalidateQueries() });
  if (q.isLoading || q.error || !s) return <><h1>Settings</h1><Loading q={q} /></>;
  const set = (path: string[], v: unknown) => setS((cur) => setIn(cur ?? {}, path, v));
  const num = (path: string[]) => (v: string) => set(path, v === "" ? "" : Number(v));
  const dirty = JSON.stringify({ ...s, mi_layout: layoutObj }) !== JSON.stringify(q.data);
  const invalidNum = [s.ollama?.num_ctx, s.ollama?.timeout, s.opencode?.timeout, s.parser?.workers, s.llm?.max_concurrency, s.llm?.max_retries].some((v) => typeof v !== "number" || Number.isNaN(v));
  return (
    <>
      <h1>Settings</h1>
      {health.data && <p className="muted small">Version {health.data.version} · parser {health.data.parser_version} · workspace <code>{health.data.workspace}</code>
        {health.data.reparse_available.length > 0 && <> · <strong>reparse available</strong> for {health.data.reparse_available.length} document(s)</>}</p>}

      <div className="card">
        <h2>Ollama</h2>
        <div className="row"><Field label="Base URL" value={s.ollama?.base_url} onChange={(v) => set(["ollama", "base_url"], v)} width={30} />
          <Field label="Context" type="number" width={8} value={s.ollama?.num_ctx} onChange={num(["ollama", "num_ctx"])} />
          <Field label="Timeout (s)" type="number" width={6} value={s.ollama?.timeout} onChange={num(["ollama", "timeout"])} /></div>
        <p className="muted small">Test uses the saved settings: save first if you changed the URL.</p>
        <Models provider="ollama" />
      </div>
      <div className="card">
        <h2>OpenCode</h2>
        <div className="row"><Field label="Binary" value={s.opencode?.binary} onChange={(v) => set(["opencode", "binary"], v)} />
          <Field label="Timeout (s)" type="number" width={6} value={s.opencode?.timeout} onChange={num(["opencode", "timeout"])} /></div>
        <Models provider="opencode" />
      </div>
      <div className="card">
        <h2>Models per task</h2>
        <table className="t"><thead><tr><th>Task</th><th>Provider</th><th>Model</th><th>Temperature</th></tr></thead>
          <tbody>{TASKS.map(([k, label]) => (
            <tr key={k}>
              <td>{label}</td>
              <td><select value={s.tasks?.[k]?.provider ?? "ollama"} onChange={(e) => set(["tasks", k, "provider"], e.target.value)}><option>ollama</option><option>opencode</option></select></td>
              <td><input value={s.tasks?.[k]?.model ?? ""} onChange={(e) => set(["tasks", k, "model"], e.target.value)} placeholder="model id" /></td>
              <td>{k === "embed" ? <span className="muted">–</span> : <input type="number" step="0.1" min="0" max="2" size={5} value={s.tasks?.[k]?.temperature ?? 0}
                onChange={(e) => set(["tasks", k, "temperature"], e.target.value === "" ? 0 : Number(e.target.value))} />}</td>
            </tr>
          ))}</tbody></table>
        <div className="row" style={{ marginTop: 8 }}>
          <Field label="Max concurrent LLM calls" type="number" width={4} value={s.llm?.max_concurrency} onChange={num(["llm", "max_concurrency"])} />
          <Field label="Retries" type="number" width={4} value={s.llm?.max_retries} onChange={num(["llm", "max_retries"])} />
        </div>
      </div>
      <div className="card">
        <h2>Parser</h2>
        <div className="row"><Field label="Worker processes (0 = CPU count − 1)" type="number" width={4} value={s.parser?.workers} onChange={num(["parser", "workers"])} />
          <Field label="Table strategy" width={10} value={s.parser?.table_strategy} onChange={(v) => set(["parser", "table_strategy"], v)} /></div>
        <p className="muted small">Changing parser options affects the next parse; existing runs are kept.</p>
      </div>
      <div className="card">
        <h2>NVMe-MI message layout</h2>
        <p className="muted small">Overrides merged over the built-in default used to decode MI messages in command databases. <strong>Verify against the MI revision under test</strong>, then re-import the command DB.
          Only the keys you list are overridden, e.g. <code>{`{"mi": {"opcode_byte": 4}}`}</code>.</p>
        <textarea rows={10} spellCheck={false} value={layout} onChange={(e) => setLayout(e.target.value)} aria-label="MI layout overrides" />
        {layoutErr && <div className="toast bad">Invalid: {layoutErr}</div>}
      </div>
      <ErrorNote error={save.error} />
      {save.isSuccess && !dirty && <div className="toast ok">Saved.</div>}
      {invalidNum && <div className="toast bad">Numeric fields must not be empty.</div>}
      <button className="primary" disabled={!dirty || !!layoutErr || invalidNum || save.isPending} onClick={() => save.mutate({ ...s, mi_layout: layoutObj })}>Save settings</button>
    </>
  );
}
