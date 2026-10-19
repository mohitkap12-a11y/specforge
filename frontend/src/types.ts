export interface RunStats { [k: string]: unknown }
export interface Audit { score?: number; coverage_ready?: boolean; errors?: number; warnings?: number; [k: string]: unknown }
export interface ParseRun {
  id: number; parser_version: string; status: string; active: number; stats: RunStats; audit: Audit;
  error: string | null; created: string; outdated: boolean;
}
export interface SpecDoc {
  id: number; family: string; version: string; title: string | null; filename: string | null;
  format: string; pages: number | null; runs: ParseRun[];
}
export interface Job {
  id: number; type: string; state: "queued" | "running" | "done" | "failed" | "cancelled";
  progress: number; total: number; message: string | null; error: string | null; result: Record<string, any> | null; created?: string;
}
export interface TreeNode { key: string; number: string | null; title: string; parent_key: string | null; page_from: number | null; page_to: number | null }
export interface SectionData {
  section: { key: string; number: string | null; title: string; text: string | null; page_from: number | null; attrs: Record<string, unknown> };
  tables: { key: string; number: string | null; title: string; page_from: number | null; header?: string[]; rows?: (string | null)[][] }[];
  requirements: { key: string; text: string; page_from: number | null; strength?: string }[];
  references: { raw: string; kind: string; target_family: string | null; target_label: string | null }[];
}
export interface Issue { id: number; severity: string; kind: string; node_key: string | null; page: number | null; message: string; state: string; note: string | null }
export interface SpecSet { id: number; name: string; members: any[]; catalog_items: number; scenarios: number; scenarios_approved: number; scenarios_draft: number }
export interface Scenario {
  id: number; sid: string; origin: string; state: "draft" | "approved" | "rejected"; title: string; description: string | null;
  observability: string; predicate: any; provenance: Record<string, any>; items: string[];
}
export interface Counts {
  covered: number; stimulus_covered: number; partial: number; not_covered: number; no_scenario: number; needs_review: number; na: number;
  total: number; denominator: number; pct_covered: number | null; pct_incl_stimulus: number | null;
}
export interface CovSummary {
  run: { id: number; set_id: number; source_id: number; created: string };
  meta: Record<string, any>;
  summary: {
    overall: Counts; by_kind: Record<string, Counts>; by_command: Record<string, Counts>; by_family: Record<string, Counts>;
    unknown_commands: { interface: string; queue: string; opcode: number; commands: number }[];
    scenarios: { state: string; n: number }[]; scenario_errors: number;
  };
}
export interface CovRun { id: number; set_id: number; source_id: number; created: string; overall: Counts | null }
export interface CovItem {
  key: string; kind: string; parent_key: string | null; label: string; observability: string; na_reason: string | null;
  state: string; covered: number; total: number; detail: Record<string, any>; attrs: Record<string, any>;
}
export interface Evidence { id: number; sid: string; title: string; state: string; observability: string; result: string; match_count: number; evidence: any[] }
export interface CmdSource {
  id: number; name: string; path: string; row_count: number; decoded_count: number; status: string; imported_at: string | null;
  mapping: Record<string, any>; unknown: { undecodable_rows?: number; errors?: { row: unknown; error: string }[] };
}
export interface CatalogItem {
  id: number; key: string; kind: string; parent_key: string | null; label: string; observability: string; na_reason: string | null; attrs: Record<string, any>;
}
export interface RefResult {
  src_key: string; raw: string; kind: string; target_family: string | null; target_label: string | null; from_family: string; from_version: string;
  state: "resolved" | "ambiguous" | "broken" | "external-not-loaded"; target_key: string | null; candidates: string[]; chosen?: boolean;
}
export interface DecodedCommand {
  id: number; src_row_id: number | null; test_id: string | null; seq: number | null; interface: string; queue: string; cs: string | null;
  opcode: number; nsid: number | null; dwords: Record<string, number>;
}

export type LogLevel = "debug" | "info" | "warn" | "error";
export interface LogEvent { id: number; ts: string; level: LogLevel; source: string; job_id: number | null; message: string; data: Record<string, any> }

export interface SectionRow { key: string; family: string; version: string; number: string; title: string; level: number; chars: number; commands: { key: string; name: string }[] }
export interface CrossSelection { section_keys: string[]; command_keys: string[] }
export interface CrossUnit {
  section_key: string; title: string; family: string; version: string; primary_chars: number; own_commands: string[]; commands: string[];
  related: { key: string; title: string; why: string; chars: number }[];
}
export interface SpecInSet { family: string; version: string; sections: number }
