# SpecForge — Implementation Plan

SpecForge parses NVMe / NVMe-MI specifications (PDF or DOCX), builds a versioned, cross-referenced model of every requirement, command, field and value, generates test scenarios (deterministic templates + local AI), and measures **submission-level** coverage against a SQLite dump of executed commands. Everything runs fully offline.

---

## 0. Ground rules

1. **Code defines the universe; AI enriches it.** The parser and catalog builder decide *what exists* (sections, tables, fields, values, normative statements). The LLM never decides what to count.
2. **Code decides coverage; AI only drafts rules.** Matching scenarios against commands is deterministic and reproducible. The AI drafts a scenario's match predicate once; a human approves it.
3. **Every item lands in exactly one state.** Nothing is silently dropped: unparsed text, unresolved references, rejected AI output and unobservable requirements are all reported.
4. **Everything is versioned and cached.** Parse results are keyed by spec file hash + parser version. AI output is keyed by input + prompt version + model. Reparse is explicit and diffable.
5. **Offline first.** No CDNs, no runtime downloads. Model weights for the parser are pre-fetched into the workspace.

---

## 1. Architecture

```
┌────────────────────────── React UI (Vite, served by FastAPI) ───────────────────────────┐
│ Specs │ Spec Explorer │ Spec Sets │ Scenarios │ Command DBs │ Coverage │ Reports │ Settings │
└──────────────────────────────────────────┬──────────────────────────────────────────────┘
                                           │ REST + SSE (job progress)
┌──────────────────────────────── FastAPI backend (Python 3.11+) ─────────────────────────┐
│ ingest/   PDF + DOCX → Spec IR      refs/      cross-spec reference resolver            │
│ audit/    completeness checks        catalog/   commands, fields, values, requirements  │
│ llm/      Ollama + OpenCode          scenarios/ templates, AI generation, DSL           │
│ cmddb/    SQLite adapter + decoder   coverage/  matcher, rollups, run diffs             │
│ reports/  xlsx, html, csv, json      jobs/      persistent job queue (SQLite-backed)    │
└───────────┬──────────────────────────────────┬────────────────────────────┬─────────────┘
     workspace/app.db (metadata)        workspace/blobs/ (specs, IR)    user's command DB
     SQLite WAL + sqlite-vec            content-addressed by sha256     (opened read-only)
```

### Stack

| Area | Choice | Reason |
|---|---|---|
| Backend | Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic | Best PDF/DOCX tooling; typed schemas reused for LLM structured output |
| PDF | Docling (layout + TableFormer), PyMuPDF, pdfplumber | Docling for structure; PyMuPDF for bookmarks, link annotations, coordinates; pdfplumber as table fallback |
| DOCX | python-docx + lxml (direct OOXML) | Lossless: native nested tables, merged cells, heading styles, REF fields/bookmarks |
| Jobs | Custom SQLite job table + process pool | No Redis; survives restarts; resumable |
| Embeddings | Ollama embedding model + sqlite-vec | Scenario dedup and "similar requirement" search |
| LLM | Ollama HTTP API; OpenCode (`opencode serve` / `opencode run`) | Both local/org; one provider interface |
| Reports | openpyxl, Jinja2 HTML, CSV/JSON | Offline; HTML prints to PDF from the browser |
| Frontend | React + Vite + TypeScript, Tailwind, shadcn/ui, TanStack Query/Table/Router, TanStack Virtual, Recharts, pdf.js (react-pdf), Monaco (bundled) | Modern, fast with 100k-row tables, fully bundled offline |
| Quality | ruff, mypy, pytest; eslint, vitest, Playwright for UI smoke | |

### Repository layout

```
specforge/
├── backend/
│   ├── pyproject.toml
│   ├── specforge/
│   │   ├── api/            # FastAPI routers (specs, sets, scenarios, cmddb, coverage, reports, settings, jobs)
│   │   ├── core/           # config, workspace, db session, logging, hashing
│   │   ├── jobs/           # job table, worker pool, progress events
│   │   ├── ingest/
│   │   │   ├── pdf/        # docling adapter, pymupdf outline/links, pdfplumber fallback
│   │   │   ├── docx/       # OOXML walker, numbering resolver, field/bookmark resolver
│   │   │   ├── ir/         # IR models, builders, stable keys
│   │   │   └── normalize/  # table stitching, bit-range parsing, sentence splitting
│   │   ├── audit/          # completeness + schema validators → parse_issue
│   │   ├── refs/           # reference extraction + resolution (intra- and cross-spec)
│   │   ├── catalog/        # command/field/value/requirement catalogs (the coverage denominator)
│   │   ├── llm/            # provider interface, ollama.py, opencode.py, cache, prompts/
│   │   ├── scenarios/      # dsl/, templates/, ai/, review
│   │   ├── cmddb/          # source adapters, column mapping, SQE + MI decoders
│   │   ├── coverage/       # matcher, rollups, diff
│   │   ├── reports/
│   │   └── cli.py          # `specforge serve|ingest|reparse|coverage|prefetch-models`
│   └── tests/
│       ├── fixtures/       # synthetic spec PDFs/DOCX + synthetic command DBs (generated)
│       └── golden/         # hand-checked expected IR for sample sections
├── frontend/
├── docs/
└── Makefile                # dev, test, lint, build, bundle-offline
```

The real NVMe specs are **not** committed. Tests use synthetic fixtures generated with reportlab/python-docx that copy the hard structures: multi-page tables, nested tables, merged cells, "Figure N (continued)", and section and cross-spec references.

---

## 2. Spec registry, versioning and reparse

### Identity

- **Spec family** — `nvme-base`, `nvme-nvm-cs`, `nvme-zns-cs`, `nvme-kv-cs`, `nvme-pcie`, `nvme-tcp`, `nvme-rdma`, `nvme-mi`, … (user-extensible).
- **Spec version** — family + version string (`2.1`, `2.0e`, `1.2c`) + optional ratification date. It is detected from the title page, and the user confirms it on upload.
- **Spec document** — the actual file (`sha256`, format pdf/docx). The same version can have several files (e.g. PDF and DOCX); one is marked primary.

### Layered, independently versioned processing

| Layer | Input | Cache key | Re-runnable on its own |
|---|---|---|---|
| L1 Parse → IR | file | `sha256 + parser_version + parse_config_hash` | **Reparse** |
| L2 Reference resolution | IR + spec set | `ir_id + set membership` | **Re-resolve** (e.g. after adding the Base spec, MI references to it resolve) |
| L3 Catalog (denominator) | IR + refs | `ir_id + catalog_version` | Rebuild catalog |
| L4 AI enrichment | catalog item + context | `item_hash + prompt_version + provider + model` | Re-enrich (all, by section, or failed only) |
| L5 Scenarios | catalog + enrichment | template version / AI key | Regenerate |
| L6 Coverage | scenarios + decoded commands | run id | Re-run |

- Parsing a spec version once makes it reusable for every project and coverage run.
- **Reparse** creates a new `parse_run`. The previous one is kept, and the UI shows a diff (nodes added, removed or changed). The user chooses which run is active.
- Changing the parser version flags existing parses as "reparse available". Reparse is never automatic.

### Stable keys (what survives reparse)

Database IDs change on reparse; stable keys don't. Every IR node gets a semantic key:

```
nvme-base@2.1/sec:5.1.13                       section
nvme-base@2.1/fig:312                          figure/table
nvme-base@2.1/fig:312/f:cdw10[07:00]:cns       field (figure + bit range + normalized name)
nvme-base@2.1/fig:313/v:cns=01h                enumerated value
nvme-base@2.1/sec:5.1.13/req:7f3a9c21          requirement (hash of normalized sentence)
```

Scenarios, approvals, manual table corrections and N/A decisions attach to **stable keys**. After a reparse they re-attach automatically. Keys that no longer exist are reported as **orphaned**, never dropped silently.

### Corrections layer

If the parser gets a table wrong, the user can fix it in the UI (edit cells, split or merge rows, mark a row as a continuation). The correction is stored as an overlay keyed by stable key and re-applied on every reparse. If a correction no longer applies cleanly after a reparse, it is reported as a parse issue.

---

## 3. Spec parsing (L1) in detail

### 3.1 PDF pipeline

1. **Outline (PyMuPDF).** Read the PDF bookmarks to build the authoritative section tree: number, title, page and y-offset. NVMe PDFs have complete bookmarks. When a PDF has none, we fall back to heading detection by font size and weight plus number patterns.
2. **Link annotations (PyMuPDF).** Internal hyperlinks ("Figure 312", "section 5.2") carry exact destinations. We record each link's source rectangle and target position. This resolves most intra-document references exactly, with no regex guessing.
3. **Layout and tables (Docling).** Docling runs per page range in a process pool (e.g. 50-page chunks). OCR is off, because the specs are born-digital. Output: text blocks, captions, and tables with cell spans.
4. **Table quality gate.** For each table:
   - column count is consistent across rows
   - a header is detected
   - the bit/byte column parses (`31:16`, `07`, `Bytes 03:00`, `63:32`)
   - field bit ranges in a dword are non-overlapping and cover the dword (reserved fields fill the gaps)

   If a check fails, we re-extract the table with pdfplumber (lattice, then stream) and keep the better-scoring result. If both fail, a `parse_issue` is raised.
5. **Multi-page table stitching.** A table whose caption is "Figure N … (continued)", or which sits at the top of a page with the same column signature as the table ending the previous page, is merged. Repeated header rows are dropped. Rows split across a page break (a row whose first cell is empty) are joined.
6. **Nested tables.** Cells containing sub-tables become child table nodes linked to the parent row and cell. For example, a field description with a value/definition table inside it.
7. **Text assignment.** Every text block goes to the deepest section whose range contains it. Headers, footers, page numbers and the TOC are detected (text repeated across pages) and excluded explicitly, which is recorded so the audit can account for them.

### 3.2 DOCX pipeline

- Walk `document.xml` in order. Headings come from paragraph styles plus `numbering.xml`; section numbers are computed by replaying the numbering counters, because Word does not store the numbers.
- Tables are read natively. `gridSpan` gives horizontal merges, `vMerge` gives vertical merges, and nested `w:tbl` elements become child tables.
- Captions come from the `Caption` style or `SEQ Figure` fields. Cross-references come from `REF`/`PAGEREF` fields pointing to bookmarks, so they resolve exactly.
- The output is the **same IR** as the PDF path, so everything downstream doesn't care about the format.

### 3.3 Normalization (into the IR)

- **Bit and byte ranges** → `{dword?, byte_lo, byte_hi, bit_lo, bit_hi}`.
- **Field rows** → `Field{name, abbreviation (e.g. CNS), range, description, reserved?, type hints}`.
- **Value enumerations.** These come from in-cell value lists ("00h = …, 01h = …"), sub-tables, or a referenced figure ("refer to Figure 313"). The reference is followed in L2.
- **Normative statements.** Sentences are split with abbreviation handling ("e.g.", "i.e.", "Fig."). Any sentence containing `shall`, `shall not`, `should`, `should not`, `may`, `is required`, `is prohibited` becomes a `Requirement` with its strength (mandatory / recommended / optional / prohibited). Lists introduced by a normative sentence stay attached as sub-clauses. Normative text inside table cells counts too.
- **Definitions and acronyms** come from the definitions section, so the AI context can include them.
- **Notes and informative text** are kept but flagged as non-normative.

### 3.4 Completeness audit (the "no gaps" proof for parsing)

Each parse run produces an audit report:

| Check | Pass condition |
|---|---|
| Section coverage | Every bookmark/heading maps to a Section node |
| Text accounting | Every text character on every page is assigned to a node, or to an explicit exclusion (header, footer, TOC, page number) |
| Figure accounting | Every "Figure N" caption maps to a table/figure node; figure numbers are contiguous; every referenced figure exists |
| Table schema | Bit ranges are valid, don't overlap and cover each dword; enumerated values in a table are unique |
| Opcode tables | Every opcode listed has a command section, and every command section has an opcode |
| References | Every reference is resolved, or flagged unresolved with a reason |

Failures become `parse_issue` rows with severity, page and bounding box, and a link to the PDF location. The user can mark each one *fixed via correction*, *accepted (not relevant)*, or leave it open. A spec version is **"coverage-ready"** only when it has no open errors.

---

## 4. Cross-references and spec sets (L2)

- **Spec set** — the unit you measure coverage against, e.g. *Base 2.1 + NVM CS 1.1 + ZNS 1.2 + PCIe 1.1 + MI 2.0*. It contains the selected parse runs.
- **Reference extraction.** Sources are PDF link annotations, DOCX REF fields, and regex over text for anything not linked:
  - `section 5.2.1`, `Figure 312`
  - `refer to the NVM Express Base Specification`
  - `(refer to the NVM Command Set Specification, section 4.1)`
  - `as defined in the NVMe Base Specification`
  - MI → Base references such as "Admin commands as defined in …"
- **Resolution order:**
  1. exact link target
  2. same document, by label
  3. the named spec family *within the current spec set*, by label
  4. the named family's content by title/heading match, because cross-spec section numbers change between versions
- **States:** `resolved`, `ambiguous` (several candidates, user picks; the choice is stored by stable key), `external-not-loaded` (target family isn't in the set; it resolves automatically when that spec is added), `broken` (target label not found).
- **Inheritance across specs.** Command set specs extend Base commands (e.g. NVM CS defines I/O commands and adds Identify CNS values and features). The catalog merges these into one view per command, keeping the defining spec on each field and value.

---

## 5. Catalog = the coverage denominator (L3)

The catalog is built deterministically from the IR plus resolved references:

| Item kind | Source | Example |
|---|---|---|
| `command` | Opcode tables ("Opcodes for Admin Commands", I/O command set opcode tables, MI command opcode tables) + their section references | Admin `06h` Identify → §5.1.13 |
| `field` | The command's SQE/dword tables (CDW10–15, NSID usage, MI NMD0/NMD1, MI request fields) | Identify CDW10 [07:00] CNS |
| `value` | Enumerations for a field (inline, sub-table, or referenced figure), merged across specs in the set | CNS = 01h (Identify Controller) |
| `reserved` | Reserved bit ranges in command fields | Identify CDW10 [15:08] reserved |
| `boundary` | Numeric fields whose limits are stated (0, max, max+1 where the spec gives max or a size) | NUMD = 0, = max |
| `feature` / `log_page` / `cns` / … | Major identifier tables (Feature IDs, Log Page IDs, MI Data Structure types, …) | FID 07h Number of Queues |
| `requirement` | Normative statements, linked to the commands, fields and values they mention | "The controller shall …" |

**Linking requirements to commands** is deterministic first:
- the section ancestry (the requirement sits under a command's section)
- explicit mentions of a field or value abbreviation (e.g. "CNS", "SEL", "FID 07h")

AI is used only for requirements that have no link or are ambiguous.

**Observability classification** (important because the DB has **submission entries only**):

| Class | Meaning | Reported as |
|---|---|---|
| `submission` | Fully checkable from what was submitted (opcode, fields, values, ordering) | Covered / Partial / Not covered |
| `stimulus` | Needs a completion to verify (e.g. "shall abort with Invalid Field"), but the triggering submission is checkable | **Stimulus covered** (response not verified), shown separately |
| `unobservable` | Controller-internal, timing, or electrical behaviour; nothing in the SQE can show it | N/A — not observable at submission level (counted, listed, excluded from %) |

Deterministic rules classify most items (templates know their class). AI classifies prose requirements, with review.

---

## 6. Command database (submission level)

### 6.1 Provisional schema (until the real one is shared)

The adapter reads the user's DB **read-only** through a **column mapping** (YAML/JSON, editable in the UI). When the real schema arrives, only the mapping changes. Two built-in layouts are supported:

```sql
-- Layout A: raw entries
CREATE TABLE commands (
  id         INTEGER PRIMARY KEY,
  test_name  TEXT,      -- test/script grouping (sequence scope)
  script     TEXT,
  seq        INTEGER,   -- order within test (or use ts)
  ts         TEXT,
  interface  TEXT,      -- 'pcie' | 'mi'
  queue      TEXT,      -- 'admin' | 'io' (pcie only)
  qid        INTEGER,
  sqe        BLOB,      -- 64-byte NVMe submission queue entry
  mi_msg     BLOB       -- raw NVMe-MI request message (MCTP payload)
);
-- Layout B: same grouping columns + integer columns cdw0..cdw15 (and MI fields as columns)
```

### 6.2 Decoding

**NVMe SQE (64 bytes):**

| Bytes | Field | Notes |
|---|---|---|
| 03:00 | CDW0 | OPC [07:00], FUSE [09:08], PSDT [15:14], CID [31:16] |
| 07:04 | NSID | |
| 15:08 | CDW2–3 | command specific |
| 23:16 | MPTR | |
| 39:24 | DPTR | PRP1/PRP2 or SGL descriptor, per PSDT |
| 63:40 | CDW10–15 | command specific |

**NVMe-MI messages:**
- The message header gives the message type, NMIMT (Control Primitive, MI Command, NVMe Admin Command, PCIe Command, …), CSI and ROR.
- MI commands carry an opcode and NMD0/NMD1 plus request data.
- NVMe Admin commands tunnelled over MI carry opcode, flags, Controller ID and the SQE dwords. Their payload is decoded **with the same Base-spec command tables** as PCIe admin commands, which is how MI coverage connects to Base coverage.

**Decoder tables come from the parsed spec catalog.** For example, the Identify CDW10 layout is taken from the parsed figure. A small built-in default (SQE header layout, MI header) only bootstraps decoding. Fields the catalog doesn't know are decoded as raw `cdwN` and listed under "unknown fields" in the report. They usually point to a vendor-specific command or a missing spec.

### 6.3 Storage and performance

- Decoded rows go to `decoded_command(source_id, src_row_id, test_id, seq, interface, queue, opcode, nsid, signature, fields_json)`, with indexes on `(interface, queue, opcode)`.
- **Signature dedup.** `signature = hash(interface, queue, opcode, decoded fields excluding CID/PRP addresses)`. Millions of commands typically collapse to thousands of distinct signatures. Single-command matching runs on signatures, and evidence fans back out to every row.
- Re-import is incremental, keyed by the source file's hash plus its max row id.

---

## 7. Scenarios (L5)

### 7.1 Predicate DSL

Every scenario carries a machine-checkable predicate (JSON, edited as YAML in Monaco):

```yaml
id: SC-ADMIN-06-CNS-01
title: Identify Controller data structure (CNS 01h)
covers: [nvme-base@2.1/fig:313/v:cns=01h]
observability: submission
match:
  steps:
    - { interface: pcie, queue: admin, opcode: 0x06, "cdw10.cns": 0x01 }
---
id: SC-ADMIN-06-RSVD-CDW10-15-8
title: Identify with reserved CDW10[15:08] non-zero
covers: [nvme-base@2.1/fig:312/f:cdw10[15:08]:reserved]
observability: stimulus          # expected Invalid Field — response not in DB
match:
  steps:
    - { opcode: 0x06, "cdw10[15:08]": { ne: 0 } }
---
id: SC-SETGET-FEATURE-SAME-FID
title: Get Features reads back a feature after Set Features
observability: submission
match:
  scope: test                    # steps must occur within the same test_name
  steps:
    - { opcode: 0x09, capture: { fid: "cdw10.fid" } }        # Set Features
    - { opcode: 0x0A, "cdw10.fid": "$fid", "cdw10.sel": 0x0 } # Get Features, current value
  order: strict
```

- **Operators:** `eq` (default), `ne`, `in`, `range`, `mask`, `any`, `absent`, `not`.
- **Sequence features:** `steps`, `order: strict|any`, `scope: test|script|global`, `within: N commands`, `capture`/`$var` binding (e.g. the same NSID across steps).
- **Validation:** every field path must exist in the catalog for that opcode, and values must lie in the field's width and enumeration. Invalid predicates are never stored as approved.
- **Note:** submission-only data can't capture values returned in completions (e.g. the NSID created by Namespace Management, which a later Namespace Attachment would use). Such scenarios match on ordering only, without the binding, and are labelled as a looser match.

### 7.2 Template scenarios (deterministic, no AI)

These are generated straight from the catalog; they are the bulk of the scenarios and exhaustive by construction:

| Template | Generated for |
|---|---|
| Command issued | every command in the set |
| Enumerated value | every value of every enumerated field (CNS, SEL, FID, LID, LSP, SES, ZSA, MI opcodes, NMIMT, data structure types, …) |
| Reserved non-zero | every reserved range in command dwords → `stimulus` |
| Unsupported/reserved value | values in reserved enumeration ranges → `stimulus` |
| Boundary | numeric fields with stated limits (0, max, max+1) |
| Flag combinations | single-bit flags (e.g. FUA, LR, PRINFO bits): each set/clear; pairwise when the spec relates them |
| Fused/PSDT | FUSE values and PRP vs SGL where the command allows them |
| MI tunnelled admin | each Base admin command allowed over MI × NVMe Admin over MI |
| Set → Get pairs | features/properties with both set and get paths |

### 7.3 AI scenarios (prose behaviour)

For each `requirement` not fully represented by templates:
1. **Context pack** (target 6–12k tokens): the requirement, its section path and neighbouring sentences, resolved referenced tables (only the relevant rows), the command's field table, and the definitions used.
2. **Structured output** (Pydantic → JSON schema): `{classification, observability, conditions[], scenarios[{title, steps (natural language), predicate, quotes[]}]}`.
3. **Validation pipeline** (all automatic):
   - JSON schema validation
   - **grounding:** every `quote` must be a verbatim (whitespace-normalized) substring of the context
   - **predicate validation** against the catalog
   - **dedup** against existing scenarios (embedding similarity + predicate equality)
   - on failure: repair retry with the validator errors (max 2), then `needs_review`
4. **Review workflow:** AI scenarios start as `draft`, and only `approved` scenarios count in coverage. Bulk approve/reject is supported with filters (by section, confidence, model).

---

## 8. Coverage engine (L6)

1. **Single-step predicates** compile to SQL over `decoded_command` signatures, using indexes on interface/queue/opcode and JSON1 for field values.
2. **Multi-step predicates** run a per-test scan (pre-filtered by the opcodes involved) with a small NFA for ordered steps, `within` windows and variable binding.
3. **Results per scenario:** `state`, `match_count`, `first/last evidence`, and up to 50 evidence row ids (all rows can be fetched through the API).
4. **Item-level rollup** (catalog items ← scenarios):
   - `Covered`: all linked submission scenarios matched
   - `Partial`: some matched (e.g. 14/32 CNS values)
   - `Stimulus covered`
   - `Not covered`
   - `N/A (unobservable / user-excluded with reason)`
   - `Needs review` (has only draft scenarios)
5. **Higher rollups:** by field → command → section → spec → spec set. Percentages always show their denominator and exclude N/A. N/A counts are shown next to them.
6. **Run diff:** compare two coverage runs (different DB dumps, spec versions or scenario sets) to see what became covered and what regressed.
7. **Optional AI suggestions:** for `Not covered` items, the AI can propose "these tests look related" by searching the decoded commands. Shown as suggestions only; never counted.

---

## 9. LLM provider layer

```python
class LLMProvider(Protocol):
    def list_models(self) -> list[ModelInfo]: ...
    def generate(self, messages, schema: type[BaseModel] | None, *, model, temperature, max_tokens) -> Result: ...
    def embed(self, texts) -> list[list[float]]: ...   # optional; falls back to Ollama or lexical dedup
```

**Ollama**
- `GET /api/tags` lists models.
- `POST /api/chat` with `format: <JSON schema>`, `stream: false`, temperature 0–0.2.
- Always set `options.num_ctx` explicitly (16k–32k). Ollama's default context is small and silently truncates prompts.
- Embeddings come from `/api/embed` (e.g. `nomic-embed-text` or `bge-m3`).
- Parallelism follows `OLLAMA_NUM_PARALLEL`. A health check shows whether the model is GPU-offloaded (`/api/ps`).

**OpenCode**
- `opencode models` lists the org-configured models.
- Generation goes through `opencode serve` (local HTTP server, session API), falling back to `opencode run --model <provider/model>` per call.
- Runs in an isolated empty temp directory with a dedicated **no-tools agent** in a generated OpenCode config. That way it can't read or edit files, and only returns text.
- The response is validated against the same JSON schema. On failure, a repair retry is sent with the errors.
- Exact flags and endpoints are checked against the installed OpenCode version in Phase 5, with an adapter test that runs against the local binary.

**Settings (per task):**
- provider and model for `classify`, `generate`, `review`, `embed`
- temperature, num_ctx, max concurrency, timeout, retry count

**Cache:** `sha256(provider, model, prompt_version, rendered_prompt, schema)` maps to the response. Changing the model or a prompt only re-runs what's affected.

**Throughput on AMD Strix Halo (fully local):**
- Prefer MoE models for bulk work, because few active parameters give good speed on unified memory. With 128 GB: `gpt-oss:120b` or `qwen3:30b-a3b`. Dense ~32B models suit a slower review pass.
- Batch 5–10 requirements from the same section into one call, which shares the context cost.
- Jobs are resumable, checkpoint per item, and can run section-first, so whole specs can run overnight while results stream into the UI.
- Template scenarios and coverage don't depend on the LLM at all, so coverage numbers are available on day one.

---

## 10. Metadata database (workspace/app.db)

```
spec_family(id, key, name)
spec_version(id, family_id, version, date, status)
spec_document(id, version_id, sha256, path, format, primary)
parse_run(id, document_id, parser_version, config_hash, status, stats_json, active, created)
ir_node(id, run_id, stable_key, kind, parent_id, ord, number, label, title, text, page_from, page_to, bbox_json, attrs_json)
ir_ref(id, run_id, src_node_id, raw, kind, target_family, target_label, target_key, resolved_node_id, state)
parse_issue(id, run_id, severity, kind, node_key, page, bbox_json, message, state, note)
correction(id, stable_key, kind, payload_json, author, created)            -- survives reparse
spec_set(id, name) ; spec_set_member(set_id, parse_run_id)
catalog_item(id, set_id, stable_key, kind, source_key, defining_family, attrs_json, observability, na_reason)
enrichment(id, item_key, provider, model, prompt_version, output_json, state)
scenario(id, set_id, stable_id, origin, title, description, observability, predicate_json, state, provenance_json)
scenario_item(scenario_id, item_key)
llm_cache(key, provider, model, response_json, created)
cmd_source(id, path, sha256, mapping_json, imported_at, row_count)
decoded_command(id, source_id, src_row_id, test_id, seq, interface, queue, opcode, nsid, signature, fields_json)
coverage_run(id, set_id, source_id, scenario_filter, created, summary_json)
coverage_result(run_id, scenario_id, state, match_count, evidence_json)
item_result(run_id, item_key, state, covered, total, detail_json)
job(id, type, state, progress, total, message, payload_json, result_json, created, updated)
setting(key, value_json)
```

---

## 11. UI (React)

| Page | Contents |
|---|---|
| **Dashboard** | Coverage tiles per spec set; trend across runs; top gaps; job activity |
| **Specs** | Upload PDF/DOCX (multi-select); detected family/version (editable); parse runs per version with status, audit score, *Reparse* / *Set active* / *Diff runs* |
| **Spec Explorer** | Section tree · content (text, requirements highlighted, tables rendered with bit ranges) · PDF pane jumping to the exact page and bounding box; references are clickable across specs; parse issues inline; table correction editor |
| **Spec Sets** | Pick spec versions into a set; reference resolution summary (resolved, ambiguous, external-not-loaded) with an ambiguity picker |
| **Catalog** | Commands → fields → values; requirements, filterable by observability; N/A marking with reason |
| **Scenarios** | Virtualized table (origin, state, observability, linked items); detail drawer with source quotes and a Monaco predicate editor with live validation; bulk review; *Generate* (templates / AI, scoped by section) |
| **Command DBs** | Add SQLite file; column mapping wizard with preview; decoded command browser (filter by opcode/test, see decoded fields); unknown-fields list |
| **Coverage** | Run selector; sunburst/heatmap by spec → section → command; matrix view (command × field values); gap list; evidence drill-down to DB rows and test names; run diff |
| **Reports** | Export builder: scope (set, sections, commands), format, contents |
| **Settings** | Providers (Ollama URL, OpenCode path), live model lists, per-task model selection, concurrency, workspace path, parser options |
| **Jobs** | Live progress over SSE, logs, cancel/resume |

Light and dark themes. All assets are bundled; no network requests besides the local API.

---

## 12. Reports

- **Excel traceability workbook**, with sheets: Summary · Specs & parse runs · Requirements · Commands · Field values · Scenarios · Coverage evidence · Gaps · Stimulus-only · N/A with reasons · Parse issues · Unknown fields.
- **HTML report**: self-contained single file with charts; prints cleanly to PDF.
- **CSV/JSON**: full machine-readable export, plus a gaps-only CSV.
- **Diff report** between two coverage runs or two spec versions.
- Every report header records: spec versions + parse run ids, command DB sha256, scenario set version, models used, and tool version, so any number can be reproduced.

---

## 13. Delivery phases

Each phase ends with something runnable and with tests.

### Phase 0 — Foundation
- Monorepo scaffold, `make dev` (FastAPI + Vite with proxy), lint/type/test tooling.
- Workspace (`~/.specforge` or configurable), app.db + Alembic, settings API, job runner with SSE.
- `specforge prefetch-models` + offline bundle script (Python wheels, Docling model weights, npm build).
- UI shell: layout, routing, theme, Settings and Jobs pages.

**Done when:** the app runs offline from a clean checkout plus the bundle, and a dummy job streams progress to the UI.

### Phase 1 — Spec ingest and IR
- PDF pipeline (outline, links, Docling, fallback, stitching, nested tables) and DOCX pipeline → common IR.
- Stable keys, parse runs, reparse, run diff, completeness audit, parse issues.
- Synthetic fixture generators and golden tests.
- UI: Specs page and Spec Explorer (tree, content, PDF pane, issues).

**Done when:** on NVMe Base 2.x, NVM CS and MI 2.x:
- every bookmark maps to a section
- every Figure caption is accounted for
- text accounting is ≥ 99.5% with the remainder listed as issues
- 20 hand-checked tables (including multi-page and nested) match the golden output field-for-field
- a 50 MB spec parses within an agreed time budget on the Halo box

### Phase 2 — References, spec sets, catalog
- Reference extraction and resolution (intra- and cross-spec, MI → Base), spec sets, re-resolve.
- Catalog builder: commands, fields, values, reserved ranges, boundaries, requirements, deterministic links, observability rules.
- Corrections layer + table editor in the UI.

**Done when:**
- every opcode table entry links to a command with a field table
- MI tunnelled admin commands resolve to Base command tables
- unresolved references are listed with reasons

### Phase 3 — Command DB ingest and decoding
- SQLite adapter, mapping wizard (layouts A and B), SQE + MI decoders driven by the catalog, signatures, incremental import.
- Synthetic command DB generator for tests.
- UI: Command DBs page and decoded command browser.

**Done when:** a synthetic DB with known contents decodes 100% correctly, and unknown opcodes/fields are reported.

### Phase 4 — Template scenarios and coverage (first real numbers, no AI)
- DSL + validator, template generators, matcher (single and sequence), rollups, run diff.
- UI: Scenarios (templates), Coverage dashboard, gap list, evidence drill-down.
- Reports v1: Excel + CSV.

**Done when:** a synthetic DB with planted commands gives exactly the expected covered/partial/not-covered sets (precision and recall = 100% by construction), and the same coverage run repeated gives byte-identical results.

### Phase 5 — LLM layer and AI enrichment
- Ollama + OpenCode providers, settings with live model lists, cache, prompts v1.
- Requirement classification/linking, AI scenario generation, grounding/predicate validation, repair loop, dedup, review workflow.

**Done when:**
- every AI output is either validated or in `needs_review`, with zero ungrounded quotes accepted
- on a labelled sample of ~100 requirements, classification and linking accuracy is measured and reported per model, so models can be compared on your hardware

### Phase 6 — Reporting and polish
- HTML report, diff reports, dashboard trends, saved filters, keyboard navigation, performance pass (virtualization, pagination, query plans).

### Phase 7 — Hardening and extras
- Spec version migration: map stable keys from version X to Y by section/figure/title similarity, and carry scenarios and approvals over with review.
- Section-range reparse, more spec families, packaging as a single installable (pipx wheel with the frontend embedded).

---

## 14. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Dense NVMe tables mis-extracted | Two extractors + quality scoring; schema validators; link annotations; correction overlay that survives reparse |
| Local model quality varies | AI never defines the denominator or the coverage verdict; grounding + predicate validation; per-model accuracy measured in Phase 5; human approval gate |
| Long AI runtimes on large specs | Templates first; batching; cache; resumable jobs; section prioritization |
| Submission-only data can't prove responses | Explicit `stimulus` and `unobservable` classes, reported separately, so coverage isn't overstated |
| Real DB schema differs | Mapping layer; only the mapping changes |
| Cross-spec version drift (MI 2.0 vs Base 2.1) | Title/heading-based resolution, ambiguity picker stored by stable key, `external-not-loaded` state |
| OpenCode CLI/API changes between versions | Thin adapter, version probe at startup, adapter tests against the installed binary |

---

## 15. Open inputs

1. Real command DB schema and a few sample rows (to finalize the mapping and the MI columns).
2. The exact spec files/versions for the first spec set (to set the Phase 1 golden tables).
3. Halo memory size (64 GB vs 128 GB), which sets the default model recommendations.
4. Whether vendor-specific opcodes should be catalogued from a user-supplied file (optional extension).
