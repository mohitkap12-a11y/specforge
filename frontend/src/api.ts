import type { LogEvent, CatalogItem, CmdSource, DecodedCommand, RefResult, CovItem, CovRun, CovSummary, CrossSelection, CrossUnit, SectionRow, Evidence, Issue, Job, Scenario, SectionData, SpecDoc, SpecSet, TreeNode } from "./types";

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
  if (!r.ok) {
    let detail: unknown = r.statusText;
    try { detail = (await r.json()).detail ?? detail; } catch { /* non-JSON error body */ }
    throw new ApiError(r.status, errorText(detail));
  }
  return (r.status === 204 ? undefined : await r.json()) as T;
}

export const qs = (o: Record<string, string | number | undefined | null>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  specs: () => req<SpecDoc[]>("GET", "/api/specs"),
  upload: (files: File[]) => { const f = new FormData(); files.forEach((x) => f.append("files", x)); return req<SpecDoc[]>("POST", "/api/specs/upload", f); },
  patchSpec: (id: number, b: { family?: string; version?: string }) => req("PATCH", `/api/specs/${id}`, b),
  deleteSpec: (id: number) => req("DELETE", `/api/specs/${id}`),
  parse: (id: number, activate = false) => req<{ job: number }>("POST", `/api/specs/${id}/parse${qs({ activate: activate ? "true" : undefined })}`),
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
  catalog: (id: number, f: { kind?: string; command?: string; observability?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; items: CatalogItem[] }>("GET", `/api/sets/${id}/catalog${qs(f)}`),
  override: (id: number, key: string, observability: string | null, na_reason: string | null) =>
    req("PUT", `/api/sets/${id}/catalog/override`, { key, observability, na_reason }),
  correctTable: (run: number, key: string, header: string[], rows: string[][]) => req<{ note: string }>("PUT", `/api/runs/${run}/tables/correction`, { key, header, rows }),
  settings: () => req<Record<string, any>>("GET", "/api/settings"),
  saveSettings: (b: Record<string, any>) => req<Record<string, any>>("PUT", "/api/settings", b),
  providerModels: (name: string) => req<{ ok: boolean; error?: string; models: { id: string; provider?: string }[]; health?: unknown }>("GET", `/api/providers/${name}/models`),
  health: () => req<{ ok: boolean; version: string; workspace: string; parser_version: string; reparse_available: unknown[] }>("GET", "/api/health"),
  putMapping: (id: number, mapping: unknown) => req("PUT", `/api/cmddb/${id}/mapping`, mapping),
  cmdCommands: (id: number, f: { opcode?: number; queue?: string; interface?: string; test?: string; offset?: number; limit?: number }) =>
    req<DecodedCommand[]>("GET", `/api/cmddb/${id}/commands${qs(f)}`),
  buildCatalog: (id: number) => req<{ job: number }>("POST", `/api/sets/${id}/catalog/build`),

  scenarios: (set: number, f: { origin?: string; state?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; scenarios: Scenario[] }>("GET", `/api/sets/${set}/scenarios${qs(f)}`),
  patchScenario: (id: number, b: Record<string, unknown>) => req<{ ok: boolean; state: string }>("PATCH", `/api/scenarios/${id}`, b),
  bulkScenarios: (ids: number[], state: string) => req<{ ok: boolean; updated: number[]; refused: { id: number; errors: string[] }[] }>("POST", "/api/scenarios/bulk", { ids, state }),
  validate: (set_id: number, predicate: unknown) => req<{ errors: string[] }>("POST", "/api/scenarios/validate", { set_id, predicate }),
  sections: (set: number, f: { q?: string; limit?: number }) => req<{ total: number; sections: SectionRow[] }>("GET", `/api/sets/${set}/sections${qs(f)}`),
  crossPlan: (set: number, b: CrossSelection) => req<{ units: CrossUnit[] }>("POST", `/api/sets/${set}/scenarios/cross/plan`, b),
  crossGenerate: (set: number, b: CrossSelection & { focus?: string; max_per_section?: number }) => req<{ job: number }>("POST", `/api/sets/${set}/scenarios/cross`, b),

  sources: () => req<CmdSource[]>("GET", "/api/cmddb"),
  suggestMapping: (path: string) => req<Record<string, any>>("GET", `/api/cmddb/suggest${qs({ path })}`),
  addSource: (path: string, mapping?: unknown) => req<{ id: number }>("POST", "/api/cmddb", { path, mapping }),
  importSource: (id: number) => req<{ job: number }>("POST", `/api/cmddb/${id}/import`),
  runCoverage: (set_id: number, source_id: number) => req<{ job: number }>("POST", "/api/coverage/run", { set_id, source_id }),
  covRuns: (set?: number) => req<CovRun[]>("GET", `/api/coverage/runs${qs({ set_id: set })}`),
  covSummary: (id: number) => req<CovSummary>("GET", `/api/coverage/${id}`),
  covItems: (id: number, f: { state?: string; kind?: string; command?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; items: CovItem[] }>("GET", `/api/coverage/${id}/items${qs(f)}`),
  covEvidence: (id: number, item: string) => req<Evidence[]>("GET", `/api/coverage/${id}/evidence${qs({ item })}`),
  covDiff: (a: number, b: number) => req<{ gained: string[]; lost: string[]; summary: Record<string, number> }>("GET", `/api/coverage/${a}/diff/${b}`),
  exportUrl: (id: number, format: string, which = "gaps") => `/api/coverage/${id}/export${qs({ format, which })}`,

  logs: (f: { after_id?: number; before_id?: number; limit?: number; level?: string; source?: string; job_id?: number; q?: string }) =>
    req<{ items: LogEvent[]; newest_id: number; has_older: boolean; sources: string[] }>("GET", `/api/logs${qs(f)}`),
  resetWorkspace: () => req<{ ok: boolean }>("POST", "/api/reset"),
  clearLogs: () => req<{ deleted: number }>("DELETE", "/api/logs"),
  activeJobs: () => req<Job[]>("GET", "/api/jobs?state=active"),
  jobs: () => req<Job[]>("GET", "/api/jobs"),
  job: (id: number) => req<Job>("GET", `/api/jobs/${id}`),
  cancelJob: (id: number) => req("POST", `/api/jobs/${id}/cancel`),
};
export const fileUrl = (docId: number) => `/api/documents/${docId}/file`;

export const logStreamUrl = (f: { after_id?: number; level?: string; source?: string; job_id?: number }) => `/api/logs/stream${qs(f)}`;
