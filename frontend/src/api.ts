import type { McpCallRow, McpFilters, McpInfo, McpIssue, McpIssueDetail, McpIssueStatus, McpMetrics, McpSettings, McpStatus } from "./types";
import type { AppUser, Features, Me, CompareChange, CompareOption, Comparison, ComparisonRow, ConversionEntry, ConvertEngine, MarkdownConversion, LogEvent, ParserEngine, CatalogItem, CmdKind, CmdSource, DbScan, DecodedCommand, FsListing, TestCov, ExecAnalysis, LinkState, RefResult, CovItem, CovRun, CovSummary, CrossSelection, CrossUnit, SectionRow, SpecInSet, Evidence, Issue, Job, ResetPreview, ResetScope, Scenario, ScenarioLink, SectionData, SpecDoc, SpecSet, TreeNode } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

/** FastAPI errors are `{detail: string | {errors: string[]} | [{msg}]}`; flatten them to readable text. */
export function errorText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String((d as any).msg) : JSON.stringify(d))).join("; ");
  if (detail && typeof detail === "object") {
    const d = detail as { errors?: string[]; message?: string };
    return [d.message, ...(d.errors ?? [])].filter(Boolean).join("; ") || JSON.stringify(detail);
  }
  return "request failed";
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.headers = { "Content-Type": "application/json" }; init.body = JSON.stringify(body); }
  const r = await fetch(path, init);
  if (r.status === 401 && !path.startsWith("/api/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED));   // session ended: show the sign-in page
  if (!r.ok) {
    let detail: unknown = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* non-JSON error body */ }
    throw new ApiError(r.status, errorText(detail));
  }
  return (r.status === 204 ? undefined : await r.json()) as T;
}

/** Fired when any API call answers 401 (session expired, signed out elsewhere, account disabled). */
export const UNAUTHORIZED = "specforge:unauthorized";

export const qs = (o: Record<string, string | number | undefined | null>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => req<Me>("GET", "/api/auth/me"),
  login: (username: string, password: string) => req<{ user: AppUser }>("POST", "/api/auth/login", { username, password }),
  logout: () => req("POST", "/api/auth/logout"),
  register: (b: { username: string; password: string; display_name?: string; note?: string }) => req<{ ok: boolean; status: string }>("POST", "/api/auth/register", b),
  changePassword: (current: string, next: string) => req("POST", "/api/auth/password", { current, new: next }),
  users: () => req<AppUser[]>("GET", "/api/users"),
  createUser: (b: { username: string; password: string; role: string; display_name?: string }) => req<AppUser>("POST", "/api/users", b),
  updateUser: (id: number, b: { role?: string; status?: string; display_name?: string }) => req<AppUser>("PATCH", `/api/users/${id}`, b),
  resetUserPassword: (id: number, password: string) => req("POST", `/api/users/${id}/password`, { password }),
  deleteUser: (id: number) => req("DELETE", `/api/users/${id}`),

  specs: () => req<SpecDoc[]>("GET", "/api/specs"),
  upload: (files: File[]) => { const f = new FormData(); files.forEach((x) => f.append("files", x)); return req<SpecDoc[]>("POST", "/api/specs/upload", f); },
  toMarkdown: (file: File, frontMatter = true, engine?: ConvertEngine) => { const f = new FormData(); f.append("file", file); return req<MarkdownConversion>("POST", `/api/convert/markdown${qs({ front_matter: frontMatter ? undefined : "false", engine })}`, f); },
  toMarkdownJob: (file: File, frontMatter = true, engine?: ConvertEngine) => { const f = new FormData(); f.append("file", file); return req<{ job: number; reused?: boolean; joined?: string }>("POST", `/api/convert/markdown/jobs${qs({ front_matter: frontMatter ? undefined : "false", engine })}`, f); },
  markdownResult: (job: number) => req<MarkdownConversion>("GET", `/api/convert/markdown/jobs/${job}`),
  conversions: () => req<ConversionEntry[]>("GET", "/api/convert/markdown/jobs"),
  removeConversion: (job: number) => req<{ ok: boolean; handed_to?: string; left?: boolean }>("DELETE", `/api/convert/markdown/jobs/${job}`),
  conversionSourceUrl: (job: number) => `/api/convert/markdown/jobs/${job}/source`,
  compareOptions: () => req<CompareOption[]>("GET", "/api/compare/options"),
  compare: (from_run: number, to_run: number) => req<Comparison>("POST", "/api/compare", { from_run, to_run }),
  comparisons: () => req<ComparisonRow[]>("GET", "/api/compare"),
  comparison: (id: number) => req<Comparison>("GET", `/api/compare/${id}`),
  compareChanges: (id: number, p: Record<string, string | number | undefined>) => req<{ total: number; items: CompareChange[] }>("GET", `/api/compare/${id}/changes${qs(p)}`),
  compareSummarize: (id: number, redo = false) => req<{ job: number }>("POST", `/api/compare/${id}/summarize`, { redo }),
  deleteComparison: (id: number) => req("DELETE", `/api/compare/${id}`),
  patchSpec: (id: number, b: { family?: string; version?: string }) => req("PATCH", `/api/specs/${id}`, b),
  deleteSpec: (id: number) => req("DELETE", `/api/specs/${id}`),
  parse: (id: number, activate = false, engine?: ParserEngine) => req<{ job: number; joined?: string }>("POST", `/api/specs/${id}/parse${qs({ activate: activate ? "true" : undefined, engine })}`),
  specFromConversion: (job: number) => req<{ document: SpecDoc; job: number }>("POST", `/api/specs/from-conversion/${job}`),
  activateRun: (id: number) => req("POST", `/api/runs/${id}/activate`),
  tree: (run: number) => req<TreeNode[]>("GET", `/api/runs/${run}/tree`),
  section: (run: number, key: string) => req<SectionData>("GET", `/api/runs/${run}/section${qs({ key })}`),
  issues: (run: number) => req<Issue[]>("GET", `/api/runs/${run}/issues`),
  patchIssue: (id: number, b: { state: string; note?: string }) => req("PATCH", `/api/issues/${id}`, b),
  runDiff: (a: number, b: number) => req<Record<string, any>>("GET", `/api/runs/${a}/diff/${b}`),

  sets: () => req<SpecSet[]>("GET", "/api/sets"),
  createSet: (name: string, document_ids: number[]) => req<{ id: number }>("POST", "/api/sets", { name, document_ids }),
  setMembers: (id: number, document_ids: number[]) => req("PUT", `/api/sets/${id}/members`, { document_ids }),
  deleteSet: (id: number) => req("DELETE", `/api/sets/${id}`),
  references: (id: number, state?: string) => req<{ summary: Record<string, number>; references: RefResult[] }>("GET", `/api/sets/${id}/references${qs({ state })}`),
  chooseRef: (id: number, src_key: string, raw: string, chosen_key: string) => req("POST", `/api/sets/${id}/references/choose`, { src_key, raw, chosen_key }),
  catalog: (id: number, f: { kind?: string; command?: string; observability?: string; family?: string; version?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; items: CatalogItem[] }>("GET", `/api/sets/${id}/catalog${qs(f)}`),
  override: (id: number, key: string, observability: string | null, na_reason: string | null) =>
    req("PUT", `/api/sets/${id}/catalog/override`, { key, observability, na_reason }),
  correctTable: (run: number, key: string, header: string[], rows: string[][]) => req<{ note: string }>("PUT", `/api/runs/${run}/tables/correction`, { key, header, rows }),
  settings: () => req<Record<string, any>>("GET", "/api/settings"),
  saveSettings: (b: Record<string, any>) => req<Record<string, any>>("PUT", "/api/settings", b),
  setFeatures: (b: Partial<Features>) => req<Features>("PUT", "/api/features", b),
  providerModels: (name: string) => req<{ ok: boolean; error?: string; models: { id: string; provider?: string }[]; health?: unknown }>("GET", `/api/providers/${name}/models`),
  health: () => req<{ ok: boolean; version: string; workspace: string; parser_version: string; reparse_available: unknown[] }>("GET", "/api/health"),
  putMapping: (id: number, mapping: unknown) => req("PUT", `/api/cmddb/${id}/mapping`, mapping),
  cmdCommands: (id: number, f: { opcode?: number; queue?: string; interface?: string; test?: string; offset?: number; limit?: number }) =>
    req<DecodedCommand[]>("GET", `/api/cmddb/${id}/commands${qs(f)}`),
  buildCatalog: (id: number) => req<{ job: number }>("POST", `/api/sets/${id}/catalog/build`),

  scenarios: (set: number, f: { origin?: string; state?: string; q?: string; source_id?: number; links?: string; offset?: number; limit?: number }) =>
    req<{ total: number; scenarios: Scenario[] }>("GET", `/api/sets/${set}/scenarios${qs(f)}`),
  scenarioLinks: (id: number) => req<ScenarioLink[]>("GET", `/api/scenarios/${id}/links`),
  reviewLink: (id: number, item_key: string, state: Exclude<LinkState, "observed">) => req<{ ok: boolean }>("PUT", `/api/scenarios/${id}/links`, { item_key, state }),
  reviewLinksBulk: (ids: number[], state: Exclude<LinkState, "observed">, from: Exclude<LinkState, "observed"> = "proposed") =>
    req<{ ok: boolean; updated: number }>("POST", "/api/scenarios/links/bulk", { ids, state, from }),
  execAnalyses: (set: number, source_id?: number) => req<ExecAnalysis[]>("GET", `/api/sets/${set}/exec${qs({ source_id })}`),
  execAnalyze: (set: number, source_id: number) => req<{ job: number }>("POST", `/api/sets/${set}/exec/analyze`, { source_id }),
  execDescribe: (set: number, source_id: number, scenario_ids?: number[], redo = false) =>
    req<{ job: number }>("POST", `/api/sets/${set}/exec/describe`, { source_id, scenario_ids, redo }),
  patchScenario: (id: number, b: Record<string, unknown>) => req<{ ok: boolean; state: string }>("PATCH", `/api/scenarios/${id}`, b),
  bulkScenarios: (ids: number[], state: string) => req<{ ok: boolean; updated: number[]; refused: { id: number; errors: string[] }[] }>("POST", "/api/scenarios/bulk", { ids, state }),
  validate: (set_id: number, predicate: unknown) => req<{ errors: string[] }>("POST", "/api/scenarios/validate", { set_id, predicate }),
  sections: (set: number, f: { q?: string; family?: string; version?: string; offset?: number; limit?: number }) => req<{ total: number; sections: SectionRow[]; specs: SpecInSet[] }>("GET", `/api/sets/${set}/sections${qs(f)}`),
  crossPlan: (set: number, b: CrossSelection) => req<{ units: CrossUnit[] }>("POST", `/api/sets/${set}/scenarios/cross/plan`, b),
  crossAngles: () => req<{ default: number; angles: { id: string; title: string; focus: string }[] }>("GET", "/api/cross/angles"),
  crossGenerate: (set: number, b: CrossSelection & { focus?: string; max_per_section?: number; passes?: number }) => req<{ job: number }>("POST", `/api/sets/${set}/scenarios/cross`, b),

  sources: () => req<CmdSource[]>("GET", "/api/cmddb"),
  suggestMapping: (path: string, kind?: CmdKind) => req<Record<string, any>>("GET", `/api/cmddb/suggest${qs({ path, kind })}`),
  addSource: (path: string, mapping?: unknown, kind?: CmdKind) => req<{ id: number }>("POST", "/api/cmddb", { path, mapping, kind }),
  scanDbs: (path: string, kind?: CmdKind) => req<DbScan>("GET", `/api/cmddb/scan${qs({ path, kind })}`),
  browseFs: (path?: string) => req<FsListing>("GET", `/api/fs/browse${qs({ path })}`),
  /** Copy dropped / picked databases to the server; `path` is each file's path inside the dropped folder. */
  uploadDbs: (files: { file: File; path: string }[]) => {
    const f = new FormData();
    files.forEach((x) => { f.append("files", x.file); f.append("paths", x.path); });
    return req<{ path: string; databases: number }>("POST", "/api/cmddb/upload", f);
  },
  importSource: (id: number) => req<{ job: number }>("POST", `/api/cmddb/${id}/import`),
  runCoverage: (set_id: number, source_id: number) => req<{ job: number }>("POST", "/api/coverage/run", { set_id, source_id }),
  covRuns: (set?: number) => req<CovRun[]>("GET", `/api/coverage/runs${qs({ set_id: set })}`),
  covSummary: (id: number) => req<CovSummary>("GET", `/api/coverage/${id}`),
  covTests: (id: number) => req<TestCov[]>("GET", `/api/coverage/${id}/tests`),
  covItems: (id: number, f: { state?: string; kind?: string; command?: string; test?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; items: CovItem[] }>("GET", `/api/coverage/${id}/items${qs(f)}`),
  covEvidence: (id: number, item: string) => req<Evidence[]>("GET", `/api/coverage/${id}/evidence${qs({ item })}`),
  covDiff: (a: number, b: number) => req<{ gained: string[]; lost: string[]; summary: Record<string, number> }>("GET", `/api/coverage/${a}/diff/${b}`),
  exportUrl: (id: number, format: string, which = "gaps") => `/api/coverage/${id}/export${qs({ format, which })}`,

  logs: (f: { after_id?: number; before_id?: number; limit?: number; level?: string; source?: string; job_id?: number; q?: string }) =>
    req<{ items: LogEvent[]; newest_id: number; has_older: boolean; sources: string[] }>("GET", `/api/logs${qs(f)}`),
  resetPreview: (scope: ResetScope) => req<ResetPreview>("GET", `/api/reset${qs({ scope })}`),
  resetWorkspace: (scope: ResetScope) => req<{ ok: boolean; deleted: Record<string, number> }>("POST", "/api/reset", { scope }),
  clearLogs: () => req<{ deleted: number }>("DELETE", "/api/logs"),
  activeJobs: () => req<Job[]>("GET", "/api/jobs?state=active"),
  jobs: () => req<Job[]>("GET", "/api/jobs"),
  job: (id: number) => req<Job>("GET", `/api/jobs/${id}`),
  /** handed_to: the owner cancelled a job others had asked for too; it goes on, owned by the first of them. */
  cancelJob: (id: number) => req<{ ok: boolean; handed_to?: string }>("POST", `/api/jobs/${id}/cancel`),

  mcpInfo: () => req<McpInfo>("GET", "/api/mcp/info"),
  mcpStatus: () => req<McpStatus>("GET", "/api/mcp/status"),
  mcpSettings: (b: Partial<McpSettings>) => req<McpSettings>("PUT", "/api/mcp/settings", b),
  mcpReindex: () => req<{ job: number; reused?: boolean }>("POST", "/api/mcp/reindex"),
  mcpResetStats: () => req<{ ok: boolean }>("POST", "/api/mcp/reset-stats"),
  mcpMetrics: (f: McpFilters) => req<McpMetrics>("GET", `/api/mcp/metrics${mcpQuery(f, { tz: tzOffset() })}`),
  mcpIssues: (f: McpFilters, status: McpIssueStatus | "all") => req<{ items: McpIssue[] }>("GET", `/api/mcp/issues${mcpQuery(f, { status })}`),
  mcpIssue: (fp: string, range: McpFilters["range"]) => req<McpIssueDetail>("GET", `/api/mcp/issues/${encodeURIComponent(fp)}${qs({ range, tz: tzOffset() })}`),
  mcpTriage: (fp: string, b: { status?: McpIssueStatus; note?: string }) => req<{ status: McpIssueStatus; note: string | null }>("PUT", `/api/mcp/issues/${encodeURIComponent(fp)}`, b),
  mcpCalls: (f: McpFilters, before_id?: number) => req<{ total: number; items: McpCallRow[]; next_before: number | null }>("GET", `/api/mcp/calls${mcpQuery(f, { before_id, limit: 100 })}`),
  mcpClearHistory: () => req<{ deleted: number }>("DELETE", "/api/mcp/calls"),
  /** What an agent sees: the endpoint's tool list, asked the way an agent asks it (JSON-RPC over Streamable HTTP). */
  mcpProbe: async () => {
    const r = await fetch("/mcp", { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }) });
    const body = await r.json().catch(() => null);
    if (!r.ok || !body?.result) throw new ApiError(r.status, body?.error?.message ?? `the endpoint answered ${r.status}`);
    return (body.result.tools as { name: string }[]).length;
  },
};
/** The viewer's offset from UTC in minutes (day buckets and the hour-of-week heatmap are drawn in local time). */
export const tzOffset = () => -new Date().getTimezoneOffset();
/** Query string of the MCP history endpoints: the filter (empty values left out) plus extra parameters. */
export const mcpQuery = (f: McpFilters, extra: Record<string, string | number | undefined> = {}) =>
  qs({ range: f.range, tool: f.tool, agent: f.agent, ip: f.ip, outcome: f.outcome, transport: f.transport, spec: f.spec, reason: f.reason,
    fingerprint: f.fingerprint, q: f.q, slow: f.slow ? 1 : undefined, ...extra });
export const mcpCsvUrl = (f: McpFilters) => `/api/mcp/calls.csv${mcpQuery(f)}`;
export const fileUrl = (docId: number) => `/api/documents/${docId}/file`;

export const logStreamUrl = (f: { after_id?: number; level?: string; source?: string; job_id?: number }) => `/api/logs/stream${qs(f)}`;
