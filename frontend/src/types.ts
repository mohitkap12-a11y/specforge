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
export interface SpecSet {
  id: number; name: string; members: any[]; catalog_items: number; scenarios: number; scenarios_approved: number; scenarios_draft: number;
  scenarios_executed: number; links_proposed: number; analyses: number;
}
export interface Scenario {
  id: number; sid: string; origin: string; state: "draft" | "approved" | "rejected" | "observed"; title: string; description: string | null;
  observability: string; predicate: any; provenance: Record<string, any>; items: string[]; source_id: number | null;
  link_counts: Partial<Record<LinkState, number>>;
}
export type LinkState = "observed" | "proposed" | "approved" | "rejected";
/** One field of an executed step: a discrete value (enumerated / flag) as sent. */
export interface ExecField { key: string; name: string; dw: number | string; hi: number; lo: number; value: number; meaning: string; unlisted?: boolean; flag?: boolean; reserved_value?: boolean }
/** A numeric field over the commands of a step. `values` is the distinct values seen, or ">16" when there were more. */
export interface ExecRange { key: string; name: string; dw: number | string; hi: number; lo: number; min: number; max: number; values: number[] | string }
export interface ExecStep {
  command: string | null; name: string; interface: string; queue: string; opcode: number; cs: string | null; tunnel: boolean; count: number;
  first: { test: string; seq: number; row: number | null }; fields: ExecField[]; ranges: ExecRange[];
  reserved_set: { key: string; dw: number | string; hi: number; lo: number; value: number }[];
}
export interface ExecPredicate { observed: true; tests: string[]; test_count: number; commands: number; truncated: boolean; steps: ExecStep[] }
export interface ScenarioLink {
  key: string; state: LinkState; detail: { commands?: number; why?: string; model?: string }; kind: string | null; label: string | null;
  observability: string | null; text?: string; strength?: string;
}
export interface ExecAnalysis {
  source_id: number; created: string; tests: number; commands: number; unknown_commands: number; scenarios: number;
  new?: number; updated?: number; removed?: number; links_reopened?: number;
  observed: Record<string, { observed: number; total: number }>;
  unknown: { interface: string; queue: string; opcode: number; commands: number }[];
}
export interface Counts {
  covered: number; stimulus_covered: number; partial: number; not_covered: number; no_scenario: number; needs_review: number; na: number;
  total: number; denominator: number; pct_covered: number | null; pct_incl_stimulus: number | null;
}
export interface CovSummary {
  run: { id: number; set_id: number; source_id: number; created: string };
  meta: Record<string, any>;
  summary: {
    overall: Counts; by_kind: Record<string, Counts>; by_kind_all?: Record<string, Counts>; by_command: Record<string, Counts>; by_family: Record<string, Counts>;
    execution?: Omit<ExecAnalysis, "source_id" | "created">;
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

export interface MarkdownConversion {
  filename: string; title: string; format: "pdf" | "docx" | "doc"; markdown: string;
  stats: { words: number; headings: number; tables: number; list_items: number; code_blocks: number; characters: number; pages?: number };
  warnings: string[];
}

export interface CompareOption { doc_id: number; family: string; version: string; filename: string | null; pages: number | null; run_id: number | null; parsed: boolean; outdated: boolean }
export type Impact = "breaking" | "new" | "changed" | "clarification" | "editorial";
export interface CompareChapter { chapter: string; title: string; total: number; score: number; breaking: number; new: number; changed: number; clarification: number; editorial: number }
export interface CompareSide { run_id: number; family: string; version: string; sections: number; tables: number; requirements: number }
export interface CompareSummary {
  from: CompareSide; to: CompareSide; total: number; by_impact: Record<Impact, number>; by_change: Record<string, number>;
  roots: { sections_added: number; sections_removed: number; tables_added: number; tables_removed: number };
  matched: { sections: number; tables: number }; normative: { added: number; removed: number; strength_changed: number }; chapters: CompareChapter[];
  entities?: Partial<Record<EntityGroup, { total: number; items: CompareEntity[] }>> | null;
}
export type EntityGroup = "commands_added" | "commands_modified" | "commands_removed" | "structures_added" | "structures_modified" | "structures_removed";
export interface CompareEntity {
  key: string; name: string; opcode: string; impact: Impact; where: string[]; change_count: number; change_ids: number[];
  changes: { id: number; kind: string; number: string | null; impact: Impact; text: string }[];
}
export interface AIPoint { title: string; detail: string; impact: Impact; refs?: { id: number; kind: string; number: string | null; title: string }[] }
export interface CompareAI {
  overview: string; highlights: AIPoint[]; breaking: string[]; upgrade_notes: string[];
  chapters: { chapter: string; title: string; headline: string; points: AIPoint[]; changes: number }[];
  entities?: Partial<Record<EntityGroup, Record<string, string>>>;
  meta: { provider?: string; model?: string; prompt_version?: string }; stats: Record<string, number>;
}
export interface Comparison { id: number; created: string; summary: CompareSummary; overview: string; ai: CompareAI | null }
export interface ComparisonRow { id: number; created: string; has_ai: boolean; from: CompareSide; to: CompareSide; total: number; by_impact: Record<Impact, number> }
export interface SentenceChange { text: string; strength: string | null; impact: Impact }
export interface SentenceEdit { before: string; after: string; delta: { removed: string[]; added: string[] }; impact: Impact; strength: (string | null)[] }
export interface CellChange { column: string; before: string; after: string }
export interface RowChange { key: string; key_before: string; cells: CellChange[]; rekeyed?: boolean }
export interface ChangeDetails {
  how?: string; ratio?: number; excerpt?: string[]; added?: SentenceChange[]; removed?: SentenceChange[]; modified?: SentenceEdit[];
  counts?: { added: number; removed: number; modified: number }; truncated?: boolean;
  header?: string[]; rows?: string[][]; row_count?: number;
  header_before?: string[]; header_after?: string[]; header_changed?: boolean;
  rows_added?: string[][]; rows_removed?: string[][]; rows_changed?: RowChange[]; rows_added_count?: number; rows_removed_count?: number; rows_changed_count?: number;
}
export interface CompareChange {
  id: number; kind: "section" | "table"; change: "added" | "removed" | "modified" | "moved" | "renamed"; impact: Impact; score: number; chapter: string;
  number_from: string | null; number_to: string | null; title: string; title_from?: string | null; nested: boolean; moved?: boolean; normative?: number; details: ChangeDetails;
}
