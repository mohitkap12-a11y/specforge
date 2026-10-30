# specforge

**Offline tools for NVMe / NVMe-MI specifications**, each with its own place in the app (sidebar groups and the Home page):

| Tool | What it does | Where |
|---|---|---|
| **Spec Converter** | Any PDF / DOC / DOCX into one clean Markdown file | UI *To Markdown*, `specforge to-md`, `POST /api/convert/markdown` |
| **Spec Compare** | Pick two versions of a spec from your library (e.g. NVMe 2.3 and 2.4): every new / removed / modified section, table and requirement, ranked by impact, plus an optional AI summary | UI *Compare versions*, `specforge compare`, `/api/compare` |
| **Coverage Tool** | Which commands, fields and requirements your tests cover (everything below) | UI *Coverage Tool* group (Workflow, Specs, Explorer, ... Reports) |
| **Spec MCP** | Coding agents (OpenCode, Roo Code, Claude Code ...) look up the parsed specs: Command Dword fields, data structures, sections, figures, and what changed between versions | `http://<server>/mcp`, UI *MCP for agents*, `specforge mcp` |

Compare, Coverage and the MCP endpoint share the library of specs uploaded and parsed on the **Specs** page; the Converter is stateless.

## Coverage tool

Scenario and command **coverage for the NVMe / NVMe-MI specifications**, fully offline.

1. Parse spec PDFs / DOCX (Base, NVM Command Set, MI, ...) into a versioned, cross-referenced model.
2. Build the list of everything that can be covered (commands, fields, values, reserved bits, boundaries, normative statements).
3. Import the SQLite databases of the commands your test scripts submitted: one database or a whole folder (one per test script).
4. **Derive the scenarios from execution:** every test case in the dump becomes one scenario (the commands it sent, in order, named from the catalog with their field values; tests that ran the same flow are merged). Optionally, AI (Ollama or OpenCode) describes each flow and proposes the spec requirements it exercises, for review.
5. Report every catalog item as **covered / partial / not covered / N/A**, with the tests as evidence, overall and per test script. For areas the tests do not reach, AI can still draft spec-driven cross-command scenarios (gap suggestions).
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
| MCP endpoint for coding agents (`/mcp`, `/mcp/sse`, `specforge mcp`), guide and admin status pages | implemented, tested with the MCP Python SDK client over Streamable HTTP, SSE and stdio, and queried against the real Base 2.4 / MI 2.2 / NVM Command Set 1.3 parsed with the built-in engine; not yet tried from OpenCode / Roo Code / Claude Code against a Docling-parsed library |
| Real NVMe specs (`docs/specifications/`: Base 2.4, MI 2.2, NVM Command Set 1.3) | run end to end once (parse, references, catalog, cross-command generation, approval, import, coverage, export); parser audit passes with 0 errors on all three, warnings remain (see "Before trusting real-spec results"); not yet run against a real command log or a local model |

## Convert a spec to Markdown

Any PDF, DOC or DOCX can be turned into one clean `.md` file, independent of the coverage workflow (nothing is stored in the workspace):

```bash
specforge to-md "NVM Express Base 2.4.pdf" notes.docx --out ./md     # writes ./md/NVM Express Base 2.4.md, ./md/notes.md
```

or use the **To Markdown** page in the UI (preview, copy, download; your converted files stay listed for 24 hours), or the API: `POST /api/convert/markdown` (multipart `file`; `?front_matter=false`, `?download=true`, `?engine=docling|builtin`) answers when the file is converted; for large specs use `POST /api/convert/markdown/jobs` (same parameters, returns `{job}`), follow `/api/jobs/{job}` and fetch `GET /api/convert/markdown/jobs/{job}` (`?download=true`) when it is done. `GET /api/convert/markdown/jobs` lists your queued and running conversions and those that ended in the last 24 hours; `DELETE /api/convert/markdown/jobs/{job}` removes one (cancelling it if needed) and deletes its files. Cancelling a queued file takes effect at once; a running Docling conversion cannot be interrupted, so it stops when the current file is done. The UI uses these endpoints, so the To Markdown page keeps showing your conversions (running, done or failed) when you leave it, refresh or open another tab. Conversions run one at a time; a result is visible to the user who started it (and admins) and is deleted after 24 hours. See [Shared server: the job queue](#shared-server-the-job-queue) for how several users' conversions and parses are queued.

**Engine: [Docling](https://github.com/docling-project/docling)** (the default). Its layout model and TableFormer rebuild every table from the page image, so register / data-structure figures keep their columns aligned (multi-row headers, merged cells, empty cells, tables continued across pages), and headings, lists and paragraphs come out in reading order. PDF and DOCX go straight to Docling; legacy `.doc` is converted to DOCX with LibreOffice (`soffice`) first. Docling runs on the CPU (a GPU is used when torch finds one); expect roughly 3-7 s per page, so a full 800-page base spec takes a while.

* **Models.** The first conversion downloads Docling's model weights (layout, TableFormer, OCR) from Hugging Face into its cache. For offline machines run `specforge prefetch-models --out ./docling-models` on a connected machine, copy the folder and set `DOCLING_ARTIFACTS_PATH=/path/to/docling-models` on the offline one.
* **OCR** is on by default (Docling's default; it also handles scanned pages). The NVMe specs are born-digital, so `SPECFORGE_DOCLING_OCR=off` roughly halves the time with practically the same output.
* **Built-in engine** (`--engine builtin`, the UI drop-down, or `SPECFORGE_CONVERT_ENGINE=builtin` to make it the default): the earlier PyMuPDF / python-docx converter. No models, much faster, but tables in complex layouts can come out misaligned. It is also used, with a warning, if Docling is not installed or its models cannot be loaded (e.g. an offline machine without `DOCLING_ARTIFACTS_PATH`; loading is retried after 10 minutes). The UI's engine drop-down defaults to the server's engine. It keeps bold / italic, hyperlinks and footnotes, drops running headers / footers and TOC dot leaders, rejects scanned PDFs (no OCR) and reads multi-column layouts in PyMuPDF's block order.

The YAML header records `engine: docling` or `engine: builtin`; images are left out (Docling writes an `<!-- image -->` marker where a figure was).

## Compare two spec versions

Upload and parse both versions on the **Specs** page, then open **Compare versions**, choose the specification and the old / new version from the drop-downs and press Compare (or `specforge compare nvme-base 2.3 2.4 --out diff.md`).

How it works: both versions are compared from their *parsed model*, not as flat text, because revisions renumber sections and figures constantly.

1. Sections are matched by their title path first (a renumbered section is "moved", not "removed + added"), then by number and by near-identical title; tables by caption inside matched sections, then by caption and header.
2. Matched sections are compared sentence by sentence (with the exact words that changed); matched tables row by row, keyed by the first column (bit range, opcode, value).
3. Every change gets an impact: **breaking** (a *shall* weakened or removed, a row removed, a field becoming reserved), **new**, **changed**, **clarification**, **editorial** (renumbering). Normative statements (*shall / should / may*) are tracked.
4. **Commands and data structures**: four tables, *new commands*, *commands modified*, *new data structures* and *data structures modified* (plus removed ones when there are any). Commands come from the opcode tables and from sections titled "... command"; a command owns its section, its sub-sections and its "Command Dword" / value tables. Structures are byte-layout tables and tables or sections titled as a data structure, log page, descriptor, entry or header. Each row lists the changes it is built from; select it in the UI to see them.
5. Optional **AI summary**: per chapter the model sees the top detected changes (with ids) and writes a headline and points citing those ids; a second call writes the overall summary, breaking changes and what to review, and one call per table writes a one-line description of each command / structure row. Ids the model was not shown are dropped, so it cannot invent a change. It uses the same `generate` model as the rest of the AI features (Settings); without AI you still get the full change list and a factual overview.

The result is stored (filter, search and page it in the UI) and exports to a Markdown report. Limits: tested on synthetic specs, not yet on the real NVMe versions; a section whose title was rewritten *and* renumbered is reported as removed + added.

## Spec MCP: the specs for coding agents

`specforge serve` also serves an [MCP](https://modelcontextprotocol.io) endpoint, so a coding agent can ask the parsed specifications instead of answering from memory, e.g. *"what is in Command Dword 10 of Identify in NVMe Base 2.4?"* or *"what changed in Identify since the previous version?"*. Answers are Markdown citing the spec, version, section or figure and page.

* **Endpoint:** `http://<server>:<port>/mcp` (Streamable HTTP, stateless, JSON responses) and `http://<server>:<port>/mcp/sse` for clients that only speak the older HTTP+SSE transport. No sign-in and no key: it is open to everyone who can reach the server and only reads the parsed specs (the web UI and `/api/` still need sign-in).
* **Setup:** the *MCP for agents* page (sidebar group *Spec MCP*) has ready-to-paste configuration for OpenCode (`opencode.json`, `"type": "remote"`), Roo Code (`mcp_settings.json` / `.roo/mcp.json`, `"type": "streamable-http"`) and Claude Code (`claude mcp add --transport http --scope user specforge http://<server>:<port>/mcp`), a *Test the endpoint* button, and a rule to add to AGENTS.md / CLAUDE.md. Admins set the URL it shows (*Public URL*) when users reach the server by a name the server does not see.
* **Tools** (all read-only):

  | Tool | Answers |
  |---|---|
  | `list_specs` | every parsed spec and version, and how to name them |
  | `search_spec` | keyword search over sections, figures and table rows (NVMe abbreviations expanded: CDW10, CNS, NSID, LBAF ...) |
  | `get_section`, `get_table` | a section (with its tables, optionally its sub-sections) or a figure |
  | `list_commands`, `get_command` | commands with opcode, section and Command Dword tables, and where other specs extend them |
  | `get_command_field` | one Command Dword (`10`, `CDW10`, `NMD0`) or field (`CNS`) with the value tables its description refers to |
  | `get_structure` | a data structure / log page / descriptor, optionally the row of one byte offset or field (`Identify Controller`, `MDTS`) |
  | `resolve_reference` | "Figure 312", "section 3.1 of the NVM Express NVM Command Set Specification" |
  | `list_changes`, `diff` | what changed between two versions (default: latest vs the one before), overall or for one command, structure, section or figure |
  | `read_raw` | the Docling Markdown of a document (also converted-but-not-parsed files), paged or around a phrase |

  Resources: `specforge://specs` and `specforge://<family>@<version>/sec:<n>` / `fig:<n>`.
* **Versions:** a spec is named by id (`nvme-base`), short name (`base`, `mi`, `nvm`, `zns` ...) or title; without a version the latest parsed one is used and the answer says so; `previous` is the version before it.
* **Comparisons** an agent asks for are computed once through the job queue (type `compare`, owner `mcp`; one job however many agents ask at the same time) and kept; a tool waits up to *compare wait* seconds (default 60), then tells the agent to retry.
* **Long answers** are paged at about 30k tokens (`page=2` ...); a referenced table over 80 rows is named with its link rather than printed.
* **Built from the parsed model**, not the flat Markdown: tables the PDF parser split over pages are read as one table (Identify Controller is one 325-row table), empty spacer columns are dropped, and a figure number that the parser gave to a caption fragment as well resolves to the real table. Parser warnings on a section or table are shown with the answer.
* **Admins** (*MCP for agents → Status*): turn the endpoint on / off, rate limit per client address (default 120 requests per minute; `0` = none), one Activity-log line per call (source `mcp`), calls / errors / not-found / slow calls per tool, the client addresses (no names are recorded), and the search index per spec version (built at startup and on first use; *Rebuild* re-indexes). Usage figures are kept in memory since the server started.
* **Local / testing:** `specforge mcp` serves the same tools over stdio against this machine's workspace.

## Install

Requires Python 3.11+. Everything runs locally; nothing is downloaded at run time.

```bash
git clone https://github.com/mohitkap12-a11y/specforge && cd specforge
python -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
```

Offline machines: build wheels on a connected machine with `pip download -d wheels -e backend` (or `pip wheel`) and install with `pip install --no-index --find-links wheels ...`; also run `specforge prefetch-models --out docling-models` there and copy the folder (see *Convert a spec to Markdown*). Docling pulls in PyTorch; for a CPU-only machine install `torch torchvision` from `https://download.pytorch.org/whl/cpu` first to avoid the multi-GB CUDA wheels.

## Run it: backend, UI, and everything together

Prerequisites: Python 3.11+, and Node 18+ only if you build or develop the UI.

| Goal | Commands | Result |
|---|---|---|
| **Use it (backend + built UI, one process)** | `make ui` once, `specforge user create <name> --admin` once, then `specforge serve` | UI and API on http://127.0.0.1:8000 (API docs at `/docs`) |
| **Backend only (API / CLI)** | `specforge serve [--host 0.0.0.0 --port 9000]` or just use the CLI commands | API on the given host/port |
| **Develop the UI (hot reload)** | terminal 1: `specforge serve`; terminal 2: `make ui-dev` | UI on http://localhost:5173, proxying `/api` to :8000 |
| **Develop the backend** | `make test` and `make lint` | tests + ruff |

Sign-in is on by default: create the first admin before starting the server (`specforge user create <name> --admin`). For a single user on one machine, `specforge serve --no-auth` skips sign-in (allowed only with a localhost `--host`). See [Sharing it on the network](#sharing-it-on-the-network-users-and-sign-in).

`make ui` runs `npm ci && npm run build` and writes `frontend/dist`, which the server finds automatically. Without a built UI, `specforge serve` still serves the API. Data lives in `~/.specforge` (override with `SPECFORGE_HOME`).

### Sharing it on the network: users and sign-in

One SpecForge server can serve a team on the local network. Everyone signs in with a local account, and all approved users share one workspace: the same specs, sets, command DBs and coverage runs. The activity log records who did what.

```bash
specforge user create alice --admin --name "Alice"     # first admin (prompts for the password)
specforge serve --host 0.0.0.0 --port 8000              # then open http://<server-ip>:8000 from other machines
```

Open the port in the server's firewall, and run `serve` as a service (systemd, NSSM) so it survives logouts.

* **New users** pick *Request access* on the sign-in page. The request stays *pending* until an admin approves it on the **Users** page (sidebar, admins only; a badge counts waiting requests).
* **Roles:** *admin* manages users (approve / reject requests, add users, change roles, disable or delete accounts, reset passwords), changes the global **Settings**, resets the workspace and clears the activity log. *User* can do everything else. Settings are read-only for users.
* **Passwords:** at least 8 characters, stored as salted scrypt hashes in `app.db`. Users change their own password under *My account* (top bar). There is no e-mail, so a forgotten password is reset by an admin. Disabling an account, changing its role or resetting its password signs that user out everywhere at once.
* **Sessions:** HttpOnly, SameSite=Strict cookie. A session expires after 7 days without use. Cross-origin writes are refused. After 10 failed sign-ins for one name from one address, further attempts are refused for 10 minutes.
* **Safeguards:** the server always keeps at least one active admin, and admins cannot demote, disable or delete themselves.
* **Reset (admins, *Settings > Reset*):** one area at a time, after showing what it deletes and typing `reset`. *Comparisons*: saved spec comparisons. *Coverage*: what came from test executions (imported command DBs, the scenarios derived from them, coverage runs); specs, sets, catalogs and drafted scenarios stay. *Specs*: specs, parse runs, sets, catalogs and scenarios, and with them the comparisons and coverage runs built on them; the To Markdown library and imported command DBs stay. *Everything*: all of that plus command DBs, the To Markdown library, job history, the activity log and the AI cache. Settings and accounts are always kept. A reset is refused while a job of that area is queued or running (`GET /api/reset?scope=…` previews it, `POST /api/reset {"scope": …}` runs it).
* **Jobs:** each user sees and is notified about their own jobs (the bell in the top bar); admins see everyone's. Only the user who started a job, or an admin, can cancel it.
* **Recovery on the server:** `specforge user list`, `specforge user passwd <name>`, `specforge user set <name> --status active|disabled|rejected --role admin|user`, `specforge user delete <name>`. These work while the server is running.

Plain HTTP is fine on a trusted LAN. Anywhere else, put a reverse proxy (nginx, Caddy) with HTTPS in front and keep SpecForge on `127.0.0.1`. The proxy must forward the original `Host` (nginx: `proxy_set_header Host $host;`) or set `X-Forwarded-Host`, and pass `X-Forwarded-Proto` so the cookie is marked `Secure`. Note that command DBs are registered by a path *on the server*, so put the dumps where the server can read them.


### Shared server: the job queue

Work runs on the server, so it keeps going when the user who asked for it closes the page; the **bell** in the top bar lists your jobs in progress (an admin's lists everyone's, with who asked for each) and those that finished since the page was opened, and a short notice says when one of yours is queued, finished or failed.

* **Docling conversions are first come, first served, one at a time.** Docling uses every CPU core it can get, so running several at once on one machine is not faster overall. A To Markdown conversion and a Specs parse that must convert its file first wait in the same queue; a queued job shows how many are ahead of it and what is running (*2 ahead of it in the queue, behind Convert NVMe Base 2.4.pdf (ann)*).
* **Parses that need no conversion do not wait in that queue** (the built-in parser, or a file already converted with Docling): they run on the shared job pool (4 at a time) next to catalog builds, imports and coverage runs.
* **The same work is done once.** Asking for a file that is already queued or being converted, or for a parse of a spec already queued with the same engine, joins that job instead of queuing another: you follow it and are told when it is done.
* **A restart keeps the queue.** Conversions and parses that were queued (or running) when the server stopped are queued again, in their old order, when it starts; other unfinished jobs are marked failed.

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

A command DB source is **one SQLite database or a folder of them**. A folder is scanned recursively (hidden folders skipped) and every SQLite database in it is **one test script**: its test name is its path inside the folder without the extension (e.g. `suiteA/2026_10_30/TC_Identify`), followed by `:<name>` when the database has a test-name column (so the same name in two databases stays two tests). Coverage is reported for all of them together and for **every test script on its own**. Re-importing a folder picks up databases added since; a database that cannot be read or lacks the command table is listed as skipped and the others are still imported. The files are opened **read-only**.

Adding sources (UI *Command DBs*): type a server path to a database or folder, pick it with **Browse…** (folders and databases on the server; admins only), or **drop a folder / database files** on the page (or use *Upload folder* / *Upload files*): dropped files are copied to the server (`~/.specforge/cmddb-uploads/`, sub-folders kept; at most 4 GB per upload, `SPECFORGE_CMDDB_UPLOAD_MB`). *Find databases* lists every database found with its test name and command counts before you add it.

**Command-history databases** (written by the test framework, one per test script) are recognised automatically. Choose which commands to check:

| Choice | Table read | Columns used |
|---|---|---|
| **NVMe** (default) | `CommandHistoryTable` | `SN` (order), `CmdType` (Admin / I/O queue), `OP`, `NS`, `FUSE`, `PSDT`, `CMDID`, `DWORD02`, `DWORD03`, `DWORD10`-`DWORD15` |
| **NVMe-MI** | `MICommandHistoryTable` | `CmdType`, `OP`, `DWORD0` / `DWORD1` (NMD0 / NMD1) |

*Both* adds two sources, one per table. Values may be hex text (`0x3ff0005`) or integers. Timing, status and vendor columns are not used (only submissions count; see below).

Other databases need one row per submitted command, and the tool suggests a column mapping (`GET /api/cmddb/suggest?path=...&kind=nvme|mi`):

* **Layout A:** a 64-byte `sqe` BLOB and/or an `mi_msg` BLOB, plus `test_name`, `seq`, `interface` (`pcie`|`mi`), `queue` (`admin`|`io`).
* **Layout B:** integer (or hex text) columns `cdw0 ... cdw15`; or the SQE field by field with `"fields": {"opcode", "nsid", "fuse", "psdt", "cid"}` plus `cdw` for the other dwords.

Map `test` (the test-case name) whenever the dump has one: scenarios are built per test case (falling back to `script`, then to the whole log as one test). `{"from": "file"}` names the test after the database.

Mapping values are a column name or `{"const": "..."}`. Set `cs` (command set, default `nvm`) when your I/O commands are not NVM-set commands. No completions are needed. Items that need a completion to verify are reported as *stimulus-only* and never counted as fully covered; controller-internal requirements are *N/A* and excluded from the percentage (listed in the report).

API: `GET /api/cmddb/scan?path=...&kind=...` (the databases of a file or folder), `GET /api/fs/browse?path=...`, `POST /api/cmddb/upload` (multipart `files` with matching `paths`), `POST /api/cmddb {"path", "kind"}`. CLI: `specforge coverage <set> <file-or-folder> [--kind mi]` prints the coverage of each test.

### Coverage per test script

Every coverage run also records what **each test on its own** exercised: commands sent, commands / values / boundaries / reserved bits / requirements exercised out of the catalog's, and its share of the catalog (commands and requirements, N/A excluded; requirements count once their link is approved). UI: *Coverage* → **By test script** (click a test to list only its items); API `GET /api/coverage/{run}/tests` and `GET /api/coverage/{run}/items?test=...`; reports: the *By test* sheet (Excel), section (HTML), `tests` in the JSON and `which=tests` for CSV.

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
make test     # 354 tests (one runs real Docling only with SPECFORGE_TEST_DOCLING=1)
make lint
```

CI (`.github/workflows/ci.yml`) runs lint and the test suite on Python 3.11 and 3.13.

## Repository layout

```
backend/specforge/
  ingest/    pdf.py docx.py normalize.py semantics.py audit.py store.py ir.py
  refs.py catalog.py scenarios.py crossgen.py cmddb.py execscen.py coverage.py reports.py jobs.py cli.py
  specquery.py mcp_server.py   spec lookups for agents / the MCP server (/mcp, `specforge mcp`)
  llm/       base.py ollama.py opencode.py service.py
  api/app.py REST + SSE
  devtools/synth.py synthetic spec and command-DB generator
backend/tests/   pytest suite
docs/            ARCHITECTURE.md, IMPLEMENTATION_PLAN.md
```

## Known limitations

* PDF: tables are detected from ruled lines (PyMuPDF, with pdfplumber as a low-quality fallback). Nested tables inside PDF cells are recorded as separate tables with a `nested_table` note; DOCX nesting is exact. Docling is not wired into the spec parser yet (it is used by the Markdown converter).
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
