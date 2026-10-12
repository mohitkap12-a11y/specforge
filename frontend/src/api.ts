import type { CmdSource, CovItem, CovRun, CovSummary, Evidence, Issue, Job, Scenario, SectionData, SpecDoc, SpecSet, TreeNode } from "./types";

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
  buildCatalog: (id: number) => req<{ job: number }>("POST", `/api/sets/${id}/catalog/build`),

  scenarios: (set: number, f: { origin?: string; state?: string; q?: string; offset?: number; limit?: number }) =>
    req<{ total: number; scenarios: Scenario[] }>("GET", `/api/sets/${set}/scenarios${qs(f)}`),
  patchScenario: (id: number, b: Record<string, unknown>) => req<{ ok: boolean; state: string }>("PATCH", `/api/scenarios/${id}`, b),
  bulkScenarios: (ids: number[], state: string) => req<{ ok: boolean; updated: number[]; refused: { id: number; errors: string[] }[] }>("POST", "/api/scenarios/bulk", { ids, state }),
  validate: (set_id: number, predicate: unknown) => req<{ errors: string[] }>("POST", "/api/scenarios/validate", { set_id, predicate }),
  aiGenerate: (set: number, b: { limit?: number }) => req<{ job: number }>("POST", `/api/sets/${set}/scenarios/ai`, b),

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

  jobs: () => req<Job[]>("GET", "/api/jobs"),
  job: (id: number) => req<Job>("GET", `/api/jobs/${id}`),
  cancelJob: (id: number) => req("POST", `/api/jobs/${id}/cancel`),
};
export const fileUrl = (docId: number) => `/api/documents/${docId}/file`;
