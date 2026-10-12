# Architecture

```
 spec PDF/DOCX ──► [L1 parse] ──► IR (versioned, stable keys) ──► audit ─┐
                                                                         ▼
 other spec versions ──► spec set ──► [L2 references] ──► [L3 catalog] ──► [L5 scenarios] ─┐
                                                              (denominator)   templates + AI drafts │
 command DB (SQLite) ──► [decode] ──► signatures ───────────────────────────────────────► [L6 coverage] ──► reports
```

Every layer has its own cache key and can be re-run alone.

| Layer | Module | Input | Output | Reproducible |
|---|---|---|---|---|
| L1 parse | `ingest/pdf.py`, `ingest/docx.py` | file (sha256) | IR nodes, issues, audit | yes |
| L2 references | `refs.py` | IR of a spec set | resolved / ambiguous / external-not-loaded / broken | yes |
| L3 catalog | `catalog.py` | IR + refs | items: command, field, value, reserved, boundary, mi_tunnel, workflow, requirement | yes |
| L5 scenarios | `scenarios.py`, `workflows.py`, `ai.py` | catalog | predicates (templates: deterministic; AI: drafts) | templates yes; AI cached |
| decode | `cmddb.py` | SQLite dump + mapping | signatures + ordered rows | yes |
| L6 coverage | `coverage.py` | approved scenarios + signatures | item states + evidence | yes |

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

## Cross-command scenarios

Single-command scenarios only show that a command was submitted. Functional tests chain commands, so the catalog also holds `workflow` items (`workflows.py`): recipes such as *Set Features FID=x then Get Features FID=x (SEL=current)*, *Create I/O CQ, Create I/O SQ, Write, Read, Delete SQ, Delete CQ*, *namespace create, attach, identify, write, read, detach, delete*, *Write then Read of the same range*, *SMART log before and after I/O*, *reserved field set, then Error Information log*, and NVMe-MI / in-band parity. Recipes name commands and field abbreviations only; opcodes, bit ranges and enumerated values (every Feature Identifier, CNS, SEL, LID, DTYP) are resolved from the catalog, steps are linked with `capture` / `{"var": ...}`, each predicate is validated, and a recipe whose commands or fields are missing is skipped with a `workflow_skipped` catalog issue. Each workflow is its own coverage item (observability `stimulus`) linked only to its own scenario, so it never changes the verdict of the single-command items.

For AI drafting, the catalog records which other commands a requirement names (`mentions`); the model is given those commands' fields and values and may draft a multi-step scenario across them (steps carry a `note` with their role). Statements that relate commands are treated as `stimulus` rather than `unobservable`.

## Scenario predicates

JSON (see `scenarios.py`): steps with `interface/queue/cs/opcode`, bit-range field conditions (`eq ne in range mask absent`), `capture`/`{"var": ...}` binding between steps, `order`, `scope` (`global|test|script`), `within`. The validator checks opcode against the catalog, value widths, and that every field lies inside a described bit range. AI output must additionally pass schema validation, verbatim quote grounding and de-duplication; the model supplies catalog command keys only, opcode/queue come from the catalog.

## Coverage engine

Single-step scenarios are evaluated over distinct command *signatures* (CID and data pointers excluded), so millions of commands collapse to a small table while evidence still points to every original row. Sequences scan ordered rows per scope with backtracking for variable binding. Drafts never count. Results are deterministic (tested: repeated runs are identical, and an independent byte-level oracle agrees with the matcher).

## Storage

`~/.specforge` (override with `SPECFORGE_HOME`): `app.db` (SQLite, WAL) and `specs/` (content-addressed copies). The command DB is never copied or modified.
