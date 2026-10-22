# specforge

Scenario and command **coverage for the NVMe / NVMe-MI specifications**, fully offline.

1. Parse spec PDFs / DOCX (Base, NVM Command Set, MI, ...) into a versioned, cross-referenced model.
2. Build the list of everything that can be covered (commands, fields, values, reserved bits, boundaries, normative statements).
3. Import a SQLite dump of the commands your test scripts submitted.
4. **Derive the scenarios from execution:** every test case in the dump becomes one scenario (the commands it sent, in order, named from the catalog with their field values; tests that ran the same flow are merged). Optionally, AI (Ollama or OpenCode) describes each flow and proposes the spec requirements it exercises, for review.
5. Report every catalog item as **covered / partial / not covered / N/A**, with the tests as evidence. For areas the tests do not reach, AI can still draft spec-driven cross-command scenarios (gap suggestions).
6. Export Excel / HTML / CSV / JSON reports.

> **Design rule:** code decides *what exists*, *what was executed* and *what is covered*; AI only describes, proposes and drafts, and never changes a coverage verdict on its own.
> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Status

| Area | State |
|---|---|
| PDF + DOCX parsing, versioning, reparse, diff, corrections, completeness audit | implemented, tested on **synthetic** specs |
| Cross-spec references and spec sets | implemented, tested |
| Catalog (coverage denominator) | implemented, tested |
| Command DB import (SQE + MI decoding) and coverage engine (single step, sequences, variables) | implemented, tested |
| Scenarios derived from execution (one per test case), AI descriptions and requirement links with review | implemented, tested on the synthetic command DB; not yet run on a real command log |
| Ollama / OpenCode providers, AI scenario generation with grounding and validation | implemented, tested against a mock Ollama server and a stub `opencode` binary |
| REST API, CLI, Excel/HTML/CSV/JSON reports | implemented, tested |
| React UI (all pages: Dashboard, Specs, Explorer, Sets, Catalog, Scenarios, Command DBs, Coverage, Reports, Jobs, Settings) | implemented in `frontend/`; checked in a browser against the synthetic demo. Not yet exercised on real specs |
| Real NVMe specs (`docs/specifications/`: Base 2.4, MI 2.2, NVM Command Set 1.3) | run end to end once (parse, references, catalog, cross-command generation, approval, import, coverage, export); parser audit passes with 0 errors on all three, warnings remain (see "Before trusting real-spec results"); not yet run against a real command log or a local model |

## Install

Requires Python 3.11+. Everything runs locally; nothing is downloaded at run time.

```bash
git clone https://github.com/mohitkap12-a11y/specforge && cd specforge
python -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
```

Offline machines: build wheels on a connected machine with `pip download -d wheels -e backend` (or `pip wheel`) and install with `pip install --no-index --find-links wheels ...`.

## Run it: backend, UI, and everything together

Prerequisites: Python 3.11+, and Node 18+ only if you build or develop the UI.

| Goal | Commands | Result |
|---|---|---|
| **Use it (backend + built UI, one process)** | `make ui` once, then `specforge serve` | UI and API on http://127.0.0.1:8000 (API docs at `/docs`) |
| **Backend only (API / CLI)** | `specforge serve [--host 0.0.0.0 --port 9000]` or just use the CLI commands | API on the given host/port |
| **Develop the UI (hot reload)** | terminal 1: `specforge serve`; terminal 2: `make ui-dev` | UI on http://localhost:5173, proxying `/api` to :8000 |
| **Develop the backend** | `make test` and `make lint` | tests + ruff |

`make ui` runs `npm ci && npm run build` and writes `frontend/dist`, which the server finds automatically. Without a built UI, `specforge serve` still serves the API. Data lives in `~/.specforge` (override with `SPECFORGE_HOME`).

### Package it (one installable file)

```bash
make package          # builds the UI, bundles it into the Python package, writes backend/dist/specforge-<version>-py3-none-any.whl
```

On the target machine (Python 3.11+ only, no Node needed):

```bash
python -m venv .venv && source .venv/bin/activate
pip install specforge-0.1.0-py3-none-any.whl     # add --no-index --find-links wheels/ on an offline machine
specforge serve
```

For a fully offline install, collect dependency wheels on a connected machine with `pip download -d wheels specforge-0.1.0-py3-none-any.whl` and copy `wheels/` along with the wheel.

### About page (for presentations)

The **About** button in the top bar opens `/about.html`, a self-contained animated page that explains the tool for non-technical audiences (arrow keys / Space to step through sections, `F` for full screen). Its source is `frontend/public/about.html`; it has no external dependencies and works offline.

## Try it with synthetic data (no real specs needed)

```bash
specforge make-demo ./demo            # synthetic Base / NVM CS / MI specs (PDF + DOCX) and a command DB
specforge ingest demo/nvme-base-2.4.pdf demo/nvme-nvm-cs-1.3.pdf demo/nvme-mi-2.0.pdf --set demo
specforge catalog 1                   # build the catalog for spec set #1
specforge analyze 1 demo/demo-commands.db               # one scenario per executed test case
specforge coverage 1 demo/demo-commands.db --out ./reports --format xlsx
make ui                               # build the web UI once (needs Node 18+); FastAPI serves frontend/dist
specforge serve                       # UI + API on http://127.0.0.1:8000  (API docs at /docs)
```

The demo files imitate the *layout* of the NVMe specs; they are **not** the real specifications.

## Using your real specs

```bash
specforge ingest NVM-Express-Base-Specification-Revision-2.4-Ratified-2026.07.31.pdf \
                 NVM-Express-NVM-Command-Set-Specification-Revision-1.3-Ratified-2026.07.31.pdf --set nvme-2.4
```

Family and version are detected from the title page (override with `--family nvme-base --version 2.4`). Each file is stored by content hash and parsed once; the result is reused by every spec set and coverage run.

* **Reparse:** `specforge reparse <document-id> --previous <old-run-id> [--activate]`. The old run is kept, a diff is printed, and the active run does not change unless you pass `--activate` (or activate it in the API: `POST /api/runs/{id}/activate`). Table corrections you made survive every reparse.
* **Parse audit:** every run records an audit (section coverage, character accounting, figure references, bit-range / value-table checks). A spec is *coverage-ready* only with zero errors. Issues are listed per run: `GET /api/runs/{id}/issues`.

### Before trusting real-spec results

The parser was built on synthetic documents and then run once on the three real PDFs in `docs/specifications/` (870, 214 and 177 pages). That run fixed merged-cell tables, opcode-table detection, 64-bit fields, doubled hyperlink text and completion-layout tables. All three now audit with 0 errors (coverage-ready). Remaining known gaps are warnings: `low_table_quality` for many tables, about 350 (Base) tables without a caption that are not linked to a command, and `duplicate_figure_label` where a figure continued over pages with a changing column layout (e.g. Identify Controller, Figure 338) is stored as several parts. On any new document:

1. Check `specforge ingest` output: `errors=0`, then read the warnings (`GET /api/runs/{id}/issues`).
2. Compare 10-20 tables by eye with the PDF (cells, multi-page tables, Figure numbers). Fix any wrong table with a correction (`PUT /api/runs/{id}/tables/correction`), then reparse.
3. Verify the **NVMe-MI header layout** against your MI revision (`backend/specforge/cmddb.py`, `MI_DEFAULT_LAYOUT`) and override in settings (`mi_layout`) if needed. It is a default, not verified against the specification text.
4. Check `specforge catalog` warnings (`command_without_section`, `unlinked_field_table`): they point to opcode names or captions the heuristics did not recognise.

## Command database

Any SQLite file with one row per submitted command. The tool opens it **read-only** and suggests a column mapping (`GET /api/cmddb/suggest?path=...`):

* **Layout A:** a 64-byte `sqe` BLOB and/or an `mi_msg` BLOB, plus `test_name`, `seq`, `interface` (`pcie`|`mi`), `queue` (`admin`|`io`).
* **Layout B:** integer columns `cdw0 ... cdw15`.

Map `test` (the test-case name) whenever the dump has one: scenarios are built per test case (falling back to `script`, then to the whole log as one test).

Mapping values are a column name or `{"const": "..."}`. Set `cs` (command set, default `nvm`) when your I/O commands are not NVM-set commands. No completions are needed. Items that need a completion to verify are reported as *stimulus-only* and never counted as fully covered; controller-internal requirements are *N/A* and excluded from the percentage (listed in the report).

## Scenarios from execution

`POST /api/sets/{id}/exec/analyze {"source_id": N}` (UI: Scenarios → Executed → *Analyse executed tests*; CLI: `specforge analyze`). For each test case of an imported command DB:

* every command is named from the catalog (`cmd:admin:06` = Identify) and its described fields are decoded; opcodes the catalog does not know are listed;
* consecutive commands of the same command with the same enumerated / flag values are one **step** (×count); numeric fields (SLBA, NLB, ...) are shown as the min..max range used;
* tests whose steps are the same (same commands, same enumerated values, same order) are **one scenario** listing all of them;
* the catalog items the scenario exercised are linked as *observed*: commands, values, min/max boundaries, reserved bits set to non-zero, Admin commands tunnelled over NVMe-MI. They count toward coverage as they are.

Re-analysing keeps a flow's id, its AI or edited description and the review of its requirement links; if the flow no longer exercises everything it did (other numeric values), its approved requirement links go back to *proposed*. Every coverage run re-analyses first, so results always match the current catalog and dump.

Requirements cannot be seen in a log of submissions. `POST /api/sets/{id}/exec/describe` (UI: *Describe with AI*) asks the model for a readable title / description per flow and which of the observable requirements of its commands the flow exercises. Those links are *proposed*; they count only after approval (`PUT /api/scenarios/{id}/links`, or in bulk `POST /api/scenarios/links/bulk`). In a coverage run an item is *covered* when a test exercised it, *not covered* when the analysed tests never did, *needs review* when only proposed links exist.

## LLM providers (Settings -> `PUT /api/settings`)

```json
{"tasks": {"generate": {"provider": "ollama", "model": "qwen3:30b-a3b", "temperature": 0.1}},
 "ollama":   {"base_url": "http://127.0.0.1:11434", "num_ctx": 16384},
 "opencode": {"binary": "opencode"}}
```

* `specforge models` lists what Ollama (`/api/tags`) and OpenCode (`opencode models`) offer on this machine.
* OpenCode runs headless in an empty temp directory with an inline config that denies edit / bash / webfetch.
* Gap suggestions (spec-driven drafts): `POST /api/sets/{id}/scenarios/cross`. Drafts have no effect on coverage until approved (`PATCH /api/scenarios/{id}` with `{"state":"approved"}`, which re-validates the predicate).

## Tests and CI

```bash
make test     # 228 tests, ~2.5 min
make lint
```

CI (`.github/workflows/ci.yml`) runs lint and the test suite on Python 3.11 and 3.13.

## Repository layout

```
backend/specforge/
  ingest/    pdf.py docx.py normalize.py semantics.py audit.py store.py ir.py
  refs.py catalog.py scenarios.py crossgen.py cmddb.py execscen.py coverage.py reports.py jobs.py cli.py
  llm/       base.py ollama.py opencode.py service.py
  api/app.py REST + SSE
  devtools/synth.py synthetic spec and command-DB generator
backend/tests/   pytest suite
docs/            ARCHITECTURE.md, IMPLEMENTATION_PLAN.md
```

## Known limitations

* PDF: tables are detected from ruled lines (PyMuPDF, with pdfplumber as a low-quality fallback). Nested tables inside PDF cells are recorded as separate tables with a `nested_table` note; DOCX nesting is exact. Docling is not wired in yet.
* PDF captions are expected *above* their table (carried over page breaks).
* DOCX cross-references use text patterns; native `REF` fields are not resolved yet.
* Cross-version section drift (a MI 2.0 reference to Base section numbers from another revision) is handled by user choice, not by title matching.
* Requirements are covered only through reviewed requirement links (or approved gap suggestions); until then they show as `no_scenario` / `needs_review`.
* Only submissions are read: a test that sent a command is evidence the command was exercised, not that the device behaved correctly.

## Web UI development

```bash
specforge serve            # API on :8000
make ui-dev                # Vite dev server on :5173, proxies /api to :8000
cd frontend && npm test    # unit tests; npm run build also type-checks
```

The sidebar collapses to an icon rail (chevron on its edge, or `Ctrl/⌘ B`; the choice is remembered) and `Ctrl/⌘ K` opens a launcher to jump anywhere. Light and dark themes follow the OS or can be set in Settings.

Pages: **Overview** (a welcome screen with a guided checklist on first run; afterwards coverage per set, trend, top uncovered commands, recent activity), **Specs** (upload, parse, reparse/activate/diff), **Explorer** (section tree, text, normative statements, tables with a correction editor, references, parse issues, jump to PDF page), **Sets** (membership, reference resolution with an ambiguity picker, build catalog), **Catalog** (filter, override observability / N/A reason), **Scenarios** (Executed: analyse a command DB, one scenario per test case with steps, tests and exercised items, AI descriptions, requirement-link review; Gap suggestions: spec-driven AI drafts with bulk approve/reject and live predicate validation), **Command DBs** (add, edit column mapping, import, decoded command browser, undecoded rows), **Coverage** (run, summaries, item list with evidence drill-down, run comparison), **Reports** (xlsx / html / json / csv), **Activity** (live log and jobs) and **Settings** (AI providers with connection test, per-task models, parser, logging, NVMe-MI layout overrides, theme).

### Activity log: what the system and the AI agents are doing

Every job, parser stage, catalog build, command-DB import and coverage run writes structured events, and so does every AI call and every decision of the scenario agent: which requirement it read, what the model was asked and answered (exact prompt and reply, retries, timing), which drafts were rejected and why. Open **Activity** to watch it live (views: All / Processing / AI agents / App, minimum level, text filter, per-job filter, pause, export as NDJSON), or use the API:

```
GET    /api/logs?level=warn&source=agent,llm&job_id=12&q=text&after_id=&before_id=&limit=
GET    /api/logs/stream?after_id=...      # server-sent events, same filters
DELETE /api/logs
```

Settings → Logging sets the minimum level (debug adds cache hits and progress heartbeats) and whether prompts and replies are kept (they can contain spec text and stay in the local workspace database). Logging never blocks work: events are queued and written by a separate thread, and the newest 100,000 are kept.
