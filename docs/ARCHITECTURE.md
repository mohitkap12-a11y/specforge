# Architecture

```
 spec PDF/DOCX ──► [L1 parse] ──► IR (versioned, stable keys) ──► audit ─┐
                                                                         ▼
 other spec versions ──► spec set ──► [L2 references] ──► [L3 catalog] ─────────────┐ (denominator)
                                                                                    ▼
 command DB (SQLite) ──► [decode] ──► signatures + ordered rows ──► [L4 execution analysis] ──► executed scenarios ─┐
                                                                    (one per test case, observed links;           │
                                                                     AI: description + proposed requirement links)  ▼
                                               [L5 gap suggestions: AI cross-command drafts] ─────────────► [L6 coverage] ──► reports
```

Every layer has its own cache key and can be re-run alone.

| Layer | Module | Input | Output | Reproducible |
|---|---|---|---|---|
| L1 parse | `ingest/pdf.py`, `ingest/docx.py` | file (sha256) | IR nodes, issues, audit | yes |
| L2 references | `refs.py` | IR of a spec set | resolved / ambiguous / external-not-loaded / broken | yes |
| L3 catalog | `catalog.py` | IR + refs | items: command, field, value, reserved, boundary, mi_tunnel, requirement | yes |
| L5 scenarios | `scenarios.py`, `crossgen.py`, `ai.py` | catalog | predicates (AI drafts per section) | AI cached |
| decode | `cmddb.py` | SQLite dump + mapping | signatures + ordered rows | yes |
| L4 execution | `execscen.py` | catalog + decoded rows | executed scenarios, observed links (+ AI descriptions, proposed links) | yes (AI part cached) |
| L6 coverage | `coverage.py` | executed scenarios + reviewed links + approved suggestions + signatures | item states + evidence | yes |

## Parsing

* **Section tree:** PDF bookmarks. Without bookmarks: bold lines starting with a section number.
* **Per page:** PyMuPDF lines and ruled tables (quality scored; pdfplumber replaces low-quality tables). Pages are extracted in a process pool; assembly is sequential, so parallel and serial parses are identical (tested).
* **Assembly:** header/footer detection by repetition, TOC lines excluded, captions bound to the next table (also across a page break), multi-page tables stitched (repeated headers dropped, split rows joined), nested tables detected by containment.
* **Normalisation (`normalize.py`, `semantics.py`):** bit and byte ranges, value ranges (`01h`, `011b`, `04h to 0Fh`), normative sentences (`shall`, `shall not`, `should`, `may` ...), references (`section 5.2`, `Figure 103`, with the nearest named spec attached).
* **Audit (`ingest/audit.py`):** headings found, character accounting (every non-whitespace character on every page must land in a node or an explicit exclusion), dangling figure references, bit-range overlap / gaps, byte-range overlap, duplicate values. Errors make a run not coverage-ready.

## Versioning

* `spec_document` is content-addressed; `parse_run` holds one parse (parser version + config hash); one run per document is *active*.
* Stable keys such as `nvme-base@2.4/fig:101` or `nvme-base@2.4/sec:5.2/req:ab12cd34` survive reparse. Corrections, scenario links, overrides and cross-spec choices attach to stable keys; keys that disappear are reported (`correction_orphaned`), never dropped.
* Bumping `PARSER_VERSION` marks active runs "reparse available"; nothing reparses automatically.

## Catalog items and observability

Command-level items come from opcode tables. Field tables are linked by caption (`Identify – Command Dword 10`, `... NMD0`) with section ancestry as fallback; value tables via the figure referenced in the field description. Each item is `submission` (checkable from the submitted entry), `stimulus` (only the triggering submission is visible) or `unobservable` (listed as N/A, excluded from the percentage). Reviewers can override with `PUT /api/sets/{id}/catalog/override`.

## Executed scenarios (L4, the main flow)

Scenarios describe what the test scripts actually did. Decoded rows are read per test case in submission order; each distinct signature is decoded once against the catalog (command key from interface / queue / command set / opcode; every described field read from its dword and bit range; CID and data pointers skipped). A run of consecutive commands of one command with equal enumerated / flag values is a step; numeric fields are summarised as min / max / distinct values. The flow identity is a hash of the step keys (command, enumerated values, reserved bits set, MI tunnelling), so tests that differ only in addresses, lengths or repeat counts merge into one scenario. Scenario ids derive from that hash (`exec:<source>:<hash>`), so re-analysis keeps descriptions and reviews.

Links: `scenario_item.state` is `observed` for items read from the log (command, value, boundary, reserved, mi_tunnel, and the field itself), `proposed` / `approved` / `rejected` for requirement links the AI suggested. Coverage counts observed and approved links; proposed links make an item `needs_review`; after an analysis, observable items no test exercised are `not_covered` (not `no_scenario`). Executed scenarios belong to their command DB: another DB's never count in a run.

The AI describer sees the flow (steps, values with their meanings) and at most 40 observable requirements of the flow's commands, ranked by the fields it set; it may only cite those (ids are checked) and never changes a step or an observed link.

## Gap suggestions: cross-command scenarios (section-driven)

The tool's scenarios are cross-command: behaviour that needs several commands (set up, exercise, observe; create before use, release after). They are generated per selected command or section (`crossgen.py`, API `/api/sets/{id}/scenarios/cross`, UI Scenarios page), never for a whole spec in one go. Nothing in it is specific to one specification family: it works from the parsed structure, the reference graph and the catalog.

Each selected section is one unit and one model call. The model reads the section (sub-sections, tables, normative statements) plus the passages connected to it anywhere in the spec set: references the section makes, references elsewhere that point into it, other commands the section names, normative statements elsewhere naming its commands, and the other sections selected in the same request. `plan` shows exactly what would be read before any model call. Opcodes, bit ranges and values come from the catalog slice for the commands involved; a draft must pass schema, verbatim-quote grounding, predicate validation, involve at least two different commands, and de-duplicate, then waits as a draft for approval. Regenerating a section replaces its unreviewed drafts only. A scenario is linked to the commands it drives and the requirements it quotes.

How many scenarios a section yields: spec text rarely states a multi-command flow outright (in the Read, Write, Write Uncorrectable and Write Zeroes sections of the NVM Command Set not one sentence names two commands), but every field description says what the device does with data, state or ordering, and that is where a test developer finds scenarios. So each section is read in several **passes** (`ANGLES` in `crossgen.py`, `passes` in the API, "Depth" in the UI), each looking for a different kind of scenario: flows the text states; each field's behaviour paired with a partner command that can observe it; misuse of preconditions and limits; value and boundary variants. The prompt shows numbered sentences and the model cites ids (`evidence`), so grounding is exact by construction (a verbatim `quotes` list still works). A field the model marks `vary` is expanded deterministically from the catalog's values for that field, one variant per value. Results of all passes are de-duplicated and capped per section (`MAX_PER_SECTION`).

Coverage is judged on commands and requirements (`DIRECT_KINDS`); fields, values and reserved bits remain in the catalog as the vocabulary scenarios are validated against.

## Scenario predicates

JSON (see `scenarios.py`): steps with `interface/queue/cs/opcode`, bit-range field conditions (`eq ne in range mask absent`), `capture`/`{"var": ...}` binding between steps, `order`, `scope` (`global|test|script`), `within`. The validator checks opcode against the catalog, value widths, and that every field lies inside a described bit range. AI output must additionally pass schema validation, verbatim quote grounding and de-duplication; the model supplies catalog command keys only, opcode/queue come from the catalog.

## Coverage engine

Single-step scenarios are evaluated over distinct command *signatures* (CID and data pointers excluded), so millions of commands collapse to a small table while evidence still points to every original row. Sequences scan ordered rows per scope with backtracking for variable binding. Drafts never count. Results are deterministic (tested: repeated runs are identical, and an independent byte-level oracle agrees with the matcher).

## Storage

`~/.specforge` (override with `SPECFORGE_HOME`): `app.db` (SQLite, WAL) and `specs/` (content-addressed copies). The command DB is never copied or modified.
