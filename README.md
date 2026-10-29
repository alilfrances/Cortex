# Cortex

Cortex is a graph-aware, local-first context engine for code agents. It ingests a git repository into a deterministic SQLite store, builds STRUCTURAL, COCHANGE, and HEADING graph layers, ranks context with personalized PageRank, and packs task-focused bundles with symbol skeletons when budgets are tight.

The result is a repo-native context service: MCP tools for live agent queries, CLI commands for reports and exports, and local parser/runtime state. Parser setup downloads only locked artifacts; repository source is never sent during setup. Normal ingest/query is offline after setup.

Current package and plugin metadata is `0.16.0` (`pyproject.toml` and plugin manifests).

## What Cortex Builds

- STRUCTURAL layer: files, imports, definitions, symbol nodes, and contains edges.
- Call graph: `calls` edges for Python, C, C++, JavaScript, TypeScript/TSX, Go, Rust, Swift, Java, and Ruby (plus QML JavaScript). One deterministic, indexed repo-wide pass resolves each call site to a real `symbol:` id, first match wins: `scoped` (`Cls::m`, `pkg.f`, module/namespace aliases), `this` (`this`/`self`/implicit this), `declared_type` (typed locals, parameters and members, including pointers, references, smart pointers and `auto x = T(...)`/`new T`), `inherited` (bounded walk over base classes), `chain` (C/C++ only: `a->b()->c()` is typed by the return type of the resolved inner call `a->b()`, or by a declared member of it, up to four calls long), `same_file`, `same_package` (Go), `import_scoped` (imports/includes, one hop), then `repo_unique`. Resolution never crosses language families. When more than one candidate matches at the deciding tier, the edge keeps its `name:<callee>` target with `confidence: LOW`, `ambiguous: true`, and up to 8 sorted `candidates`. External, built-in, and untyped-receiver calls stay `name:<callee>` and are never guessed. Without the parser runtime, the regex fallback still records name-only `calls` edges (`confidence: LOW`, never resolved). Static and declaration-driven: there is no type checker, compiler, or LSP, so overloads, generics, trait/protocol dispatch, metaprogramming, and re-export barrels are not followed. On the vendored OSS eval fixtures, 23-57% of call edges resolve; the rest are mostly framework/standard-library or untyped calls (see Eval Numbers).
- Upgrades: the index records its graph format version. An index written with an older graph format (before 0.10.0, 0.10.1's parser grammar upgrade, 0.11.0's translation-catalogue change or 0.12.0's Qt class-ownership fixes, or 0.15.0's anonymous C/C++ type labels, graph format 8) is rebuilt in full once, by the next `cortex ingest --update`, `cortex_refresh`, or MCP auto-refresh, and the MCP tools and SessionStart hook report it stale until then. In Claude Code the SessionStart hook starts that rebuild in the background (see Hooks). Without that rebuild, files untouched since the upgrade would keep their old, call-less rows.
- COCHANGE layer: git history coupling between files changed together. History is rename-aware: after a `git mv`, co-change pairs, churn, and commit provenance recorded under the old name are credited to the file's current path, and a name later reused by an unrelated file does not inherit that history. A database built by an earlier version is rebuilt this way on its next refresh.
- HEADING layer: Markdown sections for docs and planning context.
- Ranking: personalized PageRank by default, with BFS available for comparison; optional churn×complexity hotspot boosting is opt-in.
- Hotspots: deterministic per-language complexity and git touch-count analytics persisted with file nodes.
- Packing: full files when they fit; Python skeletons with imports, signatures, spans, and hashes under tight budgets.
- Communities: local graph clustering for reports and architecture overviews.
- Transport: stdio MCP server plus plugin manifests for Claude Code and Codex.

## Install

Requires Python 3.11+ on PATH as `python3`. Normal pip wheel/editable installs include the pinned `tree-sitter==0.26.0` and `tree-sitter-language-pack==1.22.1`. Marketplace/plugin launches bootstrap those parsers into a plugin-owned isolated runtime on first MCP launch, without modifying system Python or the repository. A failed setup is visible and fail-open: Cortex continues in regex/degraded mode.

### Claude Code

Inside Claude Code, use slash commands:

```text
/plugin marketplace add alilfrances/Cortex
/plugin install cortex@cortex
```

From a shell, use the Claude CLI equivalents:

```bash
claude plugin marketplace add alilfrances/Cortex
claude plugin install cortex@cortex
```

That's the full plugin path. It installs the Cortex plugin bundle, which registers the Cortex MCP server (`.mcp.json` launches `bin/cortex-mcp.py`, which self-locates its own `src/`), three narrow model-invoked skills, and the session-start hook. The first MCP launch performs bounded parser setup; subsequent launches are cache-only. Inspect the detailed `cortex_overview` response to confirm parser readiness, then ask Claude to call `cortex_refresh` once to build the index. Source/pip users can also run `cortex runtime status`.

> **Note:** Plugins load at session start. After installing or updating, restart Claude Code (or run `/reload-plugins` if available) — sessions that were already open won't see the MCP tools, skills, or hook.

For local development of the plugin itself:

```bash
claude --plugin-dir /path/to/Cortex
```

### Claude Code exploration agent

The Claude Code plugin ships a read-only `cortex-explorer` agent for multi-step exploration questions such as “where is X handled”, “how does Y flow”, and “what connects to Z”. Use it when the main agent would otherwise need several search, read, and graph calls; keep single lookups direct because a sub-agent round-trip costs more than one Cortex tool call. Its frontmatter explicitly allowlists the plugin-scoped Cortex MCP read/analysis tools plus `Read`, `Grep`, and `Glob` (but no edit, write, shell, or refresh tool). On macOS, Linux and WSL, Claude Code leaves `Glob` and `Grep` out of the default tool set but gives them back to a subagent that lists them and leaves out `Bash`, which is why `cortex-explorer` lists them and not `Bash` (it is read-only). It uses the Cortex MCP loop and returns findings, consulted file/symbol IDs with line spans, and suggested next Cortex calls. Its Cortex calls are recorded in the existing token-savings ledger like any other MCP tool call, so no extra plumbing is needed. The agent pins a small, fast model in its `model` frontmatter key; the parent can pass `model` on the Agent call for a harder question.

The definition ships in both the marketplace-installed plugin and local `claude --plugin-dir` development mode. In Claude Code's agent picker it is plugin-scoped as `cortex:cortex-explorer` (for example, `@agent-cortex:cortex-explorer`). The Codex plugin format used by `.codex-plugin/plugin.json` has no equivalent sub-agent concept, so `cortex-explorer` is Claude Code-only.

### Server instructions and workflow skills

On MCP initialization, Cortex returns concise server instructions that route agents to overview, symbol search, task retrieval, indexed reads, pre-edit context and impact checks, risk review, and `cortex_refresh` when an index is missing or stale. This baseline guidance works in any MCP client, independently of host-specific hooks or skills.

The plugin also ships three narrow, model-invoked skills (`user-invocable: false`) instead of the old catch-all Cortex skill:

- `cortex-exploration` routes “how/where/find” questions to task, symbol, text, indexed-read, relation, and path tools, with multi-step Claude exploration delegated to `cortex-explorer`.
- `cortex-pre-modification` batches known targets through `cortex_context`, then checks impact, references, graph wiring, exact spans, and freshness before edits, refactors, renames, or deletion.
- `cortex-change-review` uses diff risk and conservative dead-code analysis before committing or when reviewing and cleaning up a change.

They are selected automatically from their descriptions; no slash command is required. Plugin installs expose all three from `skills/`. For a manual Codex setup, point Codex at the repository as a plugin directory or copy the three directories under `skills/` to `~/.codex/skills/`.

## Parser runtime and air-gapped setup

```bash
cortex runtime status                 # local/socket-free health
cortex runtime setup [--force]
cortex runtime repair
```

Setup is separate from query latency: expect a one-time download of the
platform wheel and parser cache. Supported automatic targets are CPython 3.11+
on macOS Intel/Apple Silicon, Windows x64/ARM64, and glibc Linux x64/ARM64.
Proxy and custom-CA variables are honored. Corporate mirrors can set
`CORTEX_RUNTIME_ARTIFACT_MIRROR` for locked wheels and
`TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` for the parser manifest; all downloaded
bytes must still match the committed digests. For an air-gapped rollout, an
administrator builds `python3 scripts/build_runtime_bundle.py --platform
current --out runtime-dist`, verifies its `.sha256`/SBOM, distributes the zip,
and users run `CORTEX_RUNTIME_NETWORK=0 cortex runtime setup --offline-bundle
/path/to/bundle.zip --bundle-sha256 <trusted-sha256>`. The digest is mandatory
and must come from the administrator's trusted release channel. Set `CORTEX_RUNTIME_DIR` (or plugin data variables) to
control cache placement; remove the versioned directory to roll back.

See [`docs/qml-support.md`](docs/qml-support.md) and
[`docs/runtime-security.md`](docs/runtime-security.md) for syntax coverage,
resolution boundaries, and the artifact/source-egress distinction.

## Hooks

Cortex provides a lean native Claude Code `SessionStart` hook matched only for `startup|resume|clear`, so context compaction does not repeatedly inject guidance. In a git repository it reports only index state (fresh, stale, or built by an older Cortex), the indexed-file count and a one-clause `cortex-explorer` delegation hint; the tool preference list lives in the MCP server instructions. If no database exists, it emits a one-line hint that `cortex_refresh` can build it. Detailed routing stays in the MCP server instructions and three workflow skills rather than the hook.

The hook is advisory and fail-open: it never runs ingest in its own process, exits quietly on malformed or unreadable databases, and stays silent entirely when the working directory is not inside a git repository. Staleness resolves itself at query time — the MCP read tools auto-refresh incrementally before answering — so the hook only informs. The one exception is an index built by an older Cortex (graph format change): a call deadline makes the tools answer it as-is, so on `startup|resume|clear` the hook starts a detached `cortex refresh --full` through `bin/cortex-cli.py` (parser runtime set up first; no second one is started while another process holds the ingest lock, nor on Windows) and says so. While another process already holds the lock, the hook says a rebuild is already running instead. Tools keep answering from the old index, with `stale: true`, until it finishes. Set `CORTEX_SESSION_START_REBUILD=0` to turn this off; the hook then tells the model to run `cortex_refresh` (it says the same on Windows or when the launcher is missing).

### Codex

```bash
codex plugin marketplace add alilfrances/Cortex
codex plugin add cortex@cortex
```

This is the official Codex marketplace flow: first add the marketplace source (the current repo remote, `alilfrances/Cortex`), then install the `cortex` plugin from the `cortex` marketplace. OpenAI's Codex plugin docs cover [`codex plugin marketplace add`](https://developers.openai.com/codex/plugins/build#add-a-marketplace-from-the-cli) and [`codex plugin add`](https://developers.openai.com/codex/cli/reference#codex-plugin).

Start a new Codex session after installation so the plugin MCP server and skill are loaded.

Alternative: if you only want MCP server registration in Codex and do not want to install the marketplace plugin bundle, use:

```bash
/path/to/Cortex/install.sh --codex
```

`install.sh --codex` writes an absolute `mcp_servers.cortex` entry to `~/.codex/config.toml` (idempotent; no pip), but it does not install the Codex plugin bundle or add a marketplace source. The Codex plugin manifest points to `./skills/` and a Codex-specific `./.codex-mcp.json` whose relative working directory resolves from the installed plugin root. Claude retains its separate `${CLAUDE_PLUGIN_ROOT}` configuration in `.mcp.json`. For manual setup, point Codex at the repo root as a plugin dir or copy the three directories under `skills/` to `~/.codex/skills/`. Manual MCP registration equivalent:

```toml
[mcp_servers.cortex]
command = "python3"
args = ["/path/to/Cortex/bin/cortex-mcp.py"]
```

### Optional: CLI + extras (pip)

The `cortex` CLI and optional features need a pip install:

```bash
cd /path/to/Cortex
python3 -m pip install -e .                                 # CLI + pinned parser wheels
python3 -m pip install -e ".[llm,watch,tokens]"             # enrichment, watchdog, exact tokenizer
# Optional static semantic retrieval (does not change the default path):
python3 -m pip install -e ".[semantic]"
```

Initialize a target repo (also available as the `cortex_refresh` MCP tool):

```bash
cd /path/to/your-project
cortex ingest . --commits 50
cortex report .
```

This creates `cortex.db` and `cortex_report.md` under `~/.cortex/data/<repo-path-hash>/` — the target repo itself is never touched. Repos indexed before v0.4.0 keep using their existing in-repo `.cortex/` directory. Set `CORTEX_DATA_DIR` to relocate the central store. Run `cortex gc --prune` to delete data for repos that no longer exist.

The data dir also holds `meta.json` (which repo it belongs to), `ingest.lock`, and, while any Cortex process has the database open, SQLite's `cortex.db-wal`/`cortex.db-shm` files. Several Cortex processes (for example a Claude Code and a Codex MCP server on the same repo) can share one database safely: it runs in WAL mode so readers never wait for an ingest, connections wait up to 30 seconds for a competing writer instead of failing with "database is locked", and every ingest holds an exclusive `ingest.lock`. When an MCP call finds the index stale it takes the lock, re-checks freshness, and skips the refresh if another process already did it; if the lock stays busy (default 120 seconds, or the call's time budget) the call answers from the current index and reports it as stale.

## MCP Tools

| Tool | What it does | Example prompt |
|---|---|---|
| `cortex_query` | Builds a task-focused retrieval bundle under a token budget; pass `hotspot_boost: true` only when churn×complexity should influence ranking. Callees of the best-matching functions that do not fit whole appear as compact entries after the other items (a `calls:` / `override:` route comment, the declaration line and `[body elided] ...`, flagged `"compact": true` in the detailed form); the detailed form's `expansion` block counts them as `callees_compact`, next to `callees_packed` and `callees_dropped` (`too_large`, `budget_exhausted`, ...), and the concise form says `compact N` in `expansion_summary`. | "Use Cortex to find the files and symbols for adding password reset." |
| `cortex_overview` | Returns a compact repo overview, including `top_hotspots`, from stored report analysis. Its `budget` defaults to 2000 tokens; use `response_format: "detailed"` when current semantic/parser capability status is also needed. | "Ask Cortex for an overview before we refactor the API layer." |
| `cortex_impact` | Ranks structural and co-change neighbors for a file or symbol. Cross-file symbol-level edges (resolved calls, `emits`/`connects`, CMake `builds`, ...) are aggregated onto their files, one contribution per distinct caller/callee pair, with up to 5 `symbol: "caller -> callee"` why-entries per neighbor; unresolved `name:` calls count nothing. | "Use Cortex impact on `src/cortex/bundle.py` before changing packing." |
| `cortex_context` | Batches paths and symbols into one compact triage card per target; default budget 2000, with optional `include: ["impact", "cochange", "symbols"]` expansions. Cards include structural/co-change neighbors (resolved callers/callees included), hotspots, spans, and Qt/QML signals, slots, handlers, wiring, and instantiations. If an intentionally tiny budget cannot fit irreducible card metadata, `budget_feasible: false` explains the condition while preserving every original target. | "Before editing several files, call Cortex context once with all their paths and symbols." |
| `cortex_risk` | Runs a local git diff and returns deterministic 0–10 per-file risk plus missing co-change/test/Qt/build-reference directives, and `tests_to_run`: the impacted test files with their `via` tiers (`structural`, `cochange`, `pair`, `changed`), reason and score, plus `tests_run_all`/`tests_run_all_reason` when the selection cannot be vouched for; supports `range`, `staged`, and a response budget. `uncovered_changed_files_total` does not depend on the budget. When test selection runs out of time the result is `partial: true` with `untraced_changed_files_total` (changed files whose tests were not traced) and an empty `uncovered_changed_files`; run the whole suite. | "Check `cortex_risk` before committing this change." |
| `cortex_dead_code` | Finds conservative high/medium/low dead-code candidates from the persisted symbol graph and local grep references, excluding Python entry points and Qt meta-object runtime surfaces; supports an optional response budget and an optional `path` (a repo-relative file or directory: only symbols defined there are reported, references are still searched repo-wide). QML bindings, imports, parameters and ids are never candidates, and methods that override a base-class or framework method (`@Override`, `override`, `virtual`) are reported at low confidence. | "Find dead-code candidates and keep Qt slots, signals, handlers, and QML types out of the report." |
| `cortex_search_symbols` | Searches indexed file and symbol nodes by name. | "Search Cortex symbols for `SessionStore` and related methods." |
| `cortex_read_symbol` | Returns source for one indexed symbol span; `mode` picks `full` (numbered lines, default), `skeleton` (signature + nested member signatures, bodies elided), or `signature` (signature line + span only). | "Read the `generate_bundle` symbol with Cortex instead of opening the whole file." |
| `cortex_read_file` | Direct replacement for the built-in `Read` tool on an indexed file; `mode: "skeleton"` (default) returns imports/includes + every top-level signature with bodies elided, `mode: "full"` returns the exact indexed content. | "Read `src/cortex/bundle.py` with Cortex instead of the raw file." |
| `cortex_relations` | Returns parsed graph edges such as imports, calls (resolved calls point at the defining symbol in every call-graph language above; ambiguous ones keep `name:<callee>` at LOW confidence with sorted `candidates`, shown as `name:<callee> (ambiguous: N candidates)` with the list in `detailed`), inherits, emits, connects, handles, QML `instantiates`/`binds`/`reads`/`writes`/`aliases`, and CMake/QRC `builds`/`registers`/`exports` wiring. | "Show Cortex relations for `generate_bundle` outgoing calls." |
| `cortex_path` | Returns up to three shortest structural graph paths between two symbols, excluding commit/co-change shortcuts; paths run through resolved `calls` as well as imports and Qt/QML `handles`/`connects`/`emits` wiring. | "Show how `generate_bundle` reaches `count_text_tokens`." |
| `cortex_references` | Returns cross-file references from parsed graph edges plus repo grep, bucketed by file type; each hit is `path:line` plus the stripped source line as `snippet` (cut at 160 characters, counted against `budget`); `mode: "writes"` keeps definitions and mutation sites only. | "Find where `_ensure_fresh` is mutated with Cortex." |
| `cortex_search_text` | Full-text body search (FTS5 BM25) across indexed file contents, with line-anchored snippets — a grep replacement over string literals, error messages, comments, and prose that symbol search can't see. | "Search Cortex text for the 'device offline' error message." |
| `cortex_refresh` | Re-ingests the repo and updates freshness metadata (in full once when the index predates the current graph format). | "Refresh Cortex, then query the checkout flow again." |
| `cortex_status` | Reports the server's in-flight requests, how long each has been running, and lifetime per-tool counters. Also reports `queue_depth`, the active `deadline_budget_seconds`, and `fingerprint_ttl_seconds`; a `detailed` call that names `repo_path` adds `resolution`, how many `calls` edges resolve to a symbol, stay `name:` or are `external`, and the same for `inherits` (`symbol` / `name` / `module`), counted from the index without loading it. `calls` reports three figures: `in_repo_symbol_share` (`symbol / (symbol + name - name_external)`), `name_not_in_repo` (unresolved plain calls, with no receiver, scope or receiver type, to a name no symbol of the repository has: `qDebug()`, C library functions, generated code; they cannot resolve) and `resolvable_share` (`symbol / (symbol + name - name_external - name_not_in_repo)`), each also per language family under `by_family`. The target is read per language: C/C++ `resolvable_share` >= 0.6; JS and Java are reported without a target until their resolution is planned. Answered immediately even while another call is mid-scan, so a slow call can be told apart from a wedged server without attaching a debugger. | "Ask Cortex for its server status." |

Claude Code loads five of these tools upfront instead of behind ToolSearch, through `_meta: {"anthropic/alwaysLoad": true}` in their `tools/list` entries: `cortex_search_symbols`, `cortex_references`, `cortex_relations`, `cortex_read_symbol` and `cortex_impact` (about 3.7k characters of schema; the other ten stay deferred). Set `"alwaysLoad": false` on the server entry to defer all of them. Other MCP clients ignore the key.

Tool results include provenance where available. Read/query/analysis tools keep the index fresh automatically: when the repository fingerprint has changed since ingestion, they run an incremental re-ingest (changed, new, and deleted files only) before answering and report the delta under `auto_refreshed`. Set `CORTEX_AUTO_REFRESH=0` to disable and fall back to stale-state hints plus manual `cortex_refresh`. Computing the fingerprint means listing and stat-ing every indexed file, so each MCP server process remembers it per repository for `CORTEX_FINGERPRINT_TTL_SECONDS` (default `2`, `0` disables): a burst of calls pays for one scan. The tradeoff is that an edit made within that window is seen by the first call after it expires. The stored index fingerprint is still read on every call, and a refresh by this process (or a re-check after waiting for another process's ingest) never relies on the remembered value. If no index exists yet, indexed read/query tools still require an explicit `cortex_refresh` first; `cortex_risk` can run a clearly marked partial git-only analysis.

`cortex_query` and `cortex_impact` cache their result under a key derived from the (post-refresh) repo fingerprint, tool name, and call arguments (plus a payload version for `cortex_impact`, so rows written by an older release are bypassed), so a repeated identical call skips PageRank/packing entirely — any file change invalidates the cache automatically, since it changes the fingerprint. `cortex_overview` instead caches a versioned, fingerprint-keyed **analysis** record (graph/report facts and dead-code findings), then reconstructs the response on every call. This deliberately bypasses old overview response-cache entries and invalidates an incompatible analysis shape when its cache version changes. Dynamic detailed-only capability data is never cached. Set `CORTEX_QUERY_CACHE=0` to disable both reading and writing this cache. `cortex gc` prunes cached rows older than 30 days or beyond 200 rows per repo.

Tool arguments are validated: an argument a tool does not define returns `unknown_argument` with the accepted names and a closest-match hint instead of being silently ignored. Common alternative names are accepted (`cortex_path`: `source`/`from` and `target`/`to`; `cortex_references` and `cortex_read_symbol`: `target`/`name`; `cortex_query`: `query`/`prompt`; the search tools: `pattern`/`text`), `format` is read as `response_format`, and every tool tolerates `response_format`. Symbol arguments also accept `path:label`, `path::label`, `Class::method` and `Class.method`; a name that matches nothing returns `symbol_not_found` with `did_you_mean` suggestions rather than loosely related symbols.

The MCP server runs tool calls one at a time but keeps reading input while one runs, so a slow call cannot wedge the connection. `notifications/cancelled` stops a running dead-code analysis or drops a queued request without a response, and `ping` and `cortex_status` are answered immediately. Every call has a wall-clock budget, `CORTEX_TOOL_TIMEOUT_SECONDS` (default `30`, `0` disables). Dead-code analysis in `cortex_overview` and `cortex_dead_code` stops softly at the budget: the result is flagged `dead_code_partial` / `partial` with a `note`, and a partial overview analysis is not cached. Soft stops are counted in `cortex_status` (`total_soft_deadline`, per-tool `soft_deadline_count`). Every other read tool (`cortex_query`, `cortex_risk`, `cortex_impact`, `cortex_path`, `cortex_references`) checks the budget as it works, and a call that overruns returns a `deadline_exceeded` error naming the tool, elapsed time, and budget. `cortex_refresh` is exempt (a half-written index is worse than a slow one) and instead reports progress: its stage (`scan`, `parse`, `resolve`, `cochange`, `write`) and file counts appear under the call's `progress` in `cortex_status`, and a client that sends `params._meta.progressToken` also receives `notifications/progress`. A read tool never rebuilds an index from an older graph format inside its own call while a budget is active: it answers from the existing index, keeps `stale: true` and points to `cortex_refresh`. Each server process also writes `<data dir>/mcp-activity/<pid>.json` with its current tool, request id, queue depth, and completed/cancelled counts (never arguments or repo content), so a slow server can be inspected without a debugger; set `CORTEX_MCP_ACTIVITY=0` to disable it.

### Overview budgets, metadata, and caching

`cortex_overview` accepts `budget` (default `2000`) and returns `budget`, `truncated`, `dead_code_total`, `dead_code_returned`, and `budget_feasible` alongside the rendered report. Its stable report sections are retained; when space is limited, only dead-code candidates are omitted, in deterministic confidence order: `high`, then `medium`, then `low` (with a stable tie-breaker). The report states how many candidates were omitted and directs callers to `cortex_dead_code` for the complete candidate list. A budget too small even for that irreducible report returns the same shape with `budget_feasible: false` and a `budget_note` rather than claiming the boundary was met.

Dead-code findings are conservative static-analysis leads, not proof that deletion is safe. Treat the confidence tier as prioritization for review, and check dynamic/framework usage — especially Qt signals, slots, handlers, invokable methods, properties, and QML types — before removing code. For an exhaustive, unbudgeted report, use `cortex report`; its CLI output preserves all dead-code findings.

In `response_format: "detailed"`, overview additionally reconstructs current `semantic` and `language_runtime` capability metadata on each call (including parser backend counts). That volatile capability detail is intentionally not reused from cache and contributes to the delivered response cost, so request a budget with room for it.

The MCP surface has 15 tools: 13 read/query/analysis tools (everything above except `cortex_refresh` and `cortex_status`) can carry a `_meta` object: `{index_age_seconds, indexed_at, fingerprint_fresh, auto_refreshed?, cached?, saved_tokens?}`. `detailed` responses always include it; `concise` responses include it only when something is worth surfacing — the index is stale, an auto-refresh just ran, the response came from cache (`cached: true`), or the call saved a meaningful number of tokens over a raw file read (`saved_tokens`, exactly the number recorded in the `tool_usage` ledger — see `cortex saved` below) — otherwise it's omitted so a routine concise call costs no extra tokens. A cache hit always reports the *current* index age, never a value frozen from when the cache entry was written.

Every tool definition carries a human-readable `title` and MCP `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). All tools except `cortex_refresh` are marked read-only: they never modify your files (an automatic refresh only updates Cortex's own index), so clients that honour `readOnlyHint` may run them without asking for approval. None are destructive or reach the network. The server negotiates MCP protocol versions `2025-06-18`, `2025-03-26`, and `2024-11-05`: it answers `initialize` with the client's version when supported and with `2025-06-18` otherwise. Responses still use a single text content block (no `structuredContent`).

## CLI Reference

| Command | Purpose |
|---|---|
| `cortex ingest <repo> [--commits 50] [--update]` | Scan source files, git history, graph layers, symbols, and fingerprints into SQLite. |
| `cortex bundle <repo> --task "..." [--budget 4000] [--rank pagerank\|bfs] [--hotspot-boost] [--format md\|json]` | Emit a token-budgeted context bundle. |
| `cortex report <repo> [--out] [--include-test-pairs]` | Write an exhaustive, unbudgeted architecture report with central nodes, hotspots, communities, connections, and every confidence-tiered dead-code finding. |
| `cortex risk [range] [--staged] [--format text|json] [--tests-only] [--db PATH]` | Analyze a committed range (default `HEAD~1..HEAD`) or staged diff with deterministic per-file risk, missing-context directives and impacted tests. It runs local git only; no index is required, but an unindexed/partial result says so explicitly. `--tests-only` prints one impacted test path per line (pipe it into a runner) and prints nothing and exits 2 when the full suite should run. |
| `python3 "${CLAUDE_PLUGIN_ROOT}/bin/cortex-cli.py" <command>` | The same CLI for a Claude Code plugin install without `cortex` on `PATH`: a self-locating launcher (like `bin/cortex-mcp.py`) that sets up the parser runtime first. The SessionStart hook uses it for the background rebuild. |
| `cortex gc [--prune]` | List central data dirs (`--prune` deletes ones whose repo is gone) and prune each repo's query result cache. |
| `cortex enrich <repo> --provider claude\|codex --allow-code-upload [--force]` | Remote LLM enrichment. Sends up to 8,000 characters from each uncached indexed source file to the provider; requires explicit consent and `[llm]`. |
| `cortex semantic setup [--force]` | Explicitly download/cache `minishlab/potion-code-16M` below `CORTEX_DATA_DIR`; parser artifacts are managed separately by `cortex runtime setup`. |
| `cortex semantic status` | Show optional dependency, local-model, and indexed-chunk status without network access. |
| `cortex benchmark <repo> [--budget 4000] [--format text\|json]` | Compare bundle token cost against full-corpus reading. |
| `cortex saved <repo> [--daily] [--format text\|json] [--price-per-mtok in,out]` | Report token savings recorded from MCP tool calls (see Token Savings below). |
| `cortex mcp` | Run the stdio MCP server. |
| `cortex migrate [project_dir]` | Remove old injected v0.1 `## cortex` guidance and point users to plugin setup. |
| `cortex codex status\|uninstall [project_dir]` | Inspect or remove project-local Codex integration files. |
| `cortex claude status\|uninstall [project_dir]` | Inspect or remove project-local Claude integration files. |
| `cortex graph export <repo> --format graphml\|json\|obsidian --out <path>` | Export the stored graph. |
| `cortex graph view <repo> --out cortex-graph.html` | Write a self-contained no-CDN HTML graph viewer. |
| `cortex watch <repo> [--interval 30]` | Refresh on changes using watchdog when installed, polling otherwise. |
| `cortex hook install\|uninstall\|status [project_dir]` | Manage repo-local git hooks that run `cortex refresh`. |

`cortex refresh <repo>` is also available as a convenience command for ingest plus report generation.

### Risk score policy

`cortex risk` uses fixed normalized components: `min(1, churn/100)`, stored hotspot score `/1000`, structural fan-in `/10`, strongest missing COCHANGE weight, and actionable-directive count `/3`. Their weights are respectively `0.30`, `0.20`, `0.15`, `0.15`, and `0.20`; the deterministic score is `round(10 * sum(weight * component), 2)`. Missing COCHANGE advice uses a documented threshold of `0.50`. Ties sort by descending score, then repository-relative path. Risk never runs network commands.

### Query configuration (`.cortex/config.toml`)

The `[query]` table of the repository's `.cortex/config.toml` tunes how a task is matched:

```toml
[query]
# Two-way vocabulary bridge: a task that says "USB" also matches code named after the OS device-event API.
synonyms = { usb = ["devicechange"] }
# Files named `*_win.*`, `*_mac.*`, `*_linux.*`, `*_android.*`, `*_ios.*` (or under a directory of that name) are
# demoted, and their overriders ranked last, unless the task names the platform. A product name listed here counts
# as naming it: a task that says "explorer" is a Windows task. Platforms: win, mac, linux, android, ios.
platform_terms = { explorer = "win", finder = "mac", udev = "linux" }
```

### Impacted tests

`tests_to_run` maps the diff to test files. Changed lines (new side of the `--unified=0` diff, blank lines ignored) are mapped to the innermost indexed symbol whose span contains them; a change outside every symbol, or in a file without symbols, seeds the whole file. From there a bounded reverse walk (depth ≤ 3, edge target → source) over STRUCTURAL `calls`, `imports`, `references`, `inherits`, `instantiates`, `connects`, `handles`, `emits`, `binds`/`reads`/`writes`/`aliases`, `builds` and `registers` edges stops at test files (`structural`, score `1/depth`). The file of a changed symbol is also followed through its build/registration/instantiation edges, the type that owns a changed member through its instantiations (depth +1), and a C/C++ member declared in a header is treated as the same node as its out-of-line definition. A `connect()` written in a test selects that test, and a CMake target or `.qrc` that builds/registers the changed file selects the tests it also builds. Further tiers: a COCHANGE partner test with weight ≥ `0.50` (`cochange`, score = weight), a file-name pair such as `a.py`/`test_a.py` (`pair`, 0.6), and a changed test file (`changed`, 1.0); an entry merges its tiers and keeps the best score. `tests_run_all` is set when the index is unavailable, a build/config file changed (`CMakeLists.txt`, `*.cmake`, `*.qrc`, `*.toml`, `*.yaml`/`*.yml`, `package.json`, `conftest.py`, `setup.cfg`, ...), a changed file is deleted or unindexed, no test is indexed, a changed non-documentation file was reached by no tier (including fixtures and goldens that tests read by path), or a tight budget had to cut the list (it is truncated last, after every other optional field).

## Extras

| Extra | Adds | Notes |
|---|---|---|
| `[llm]` | `anthropic`, `openai` | Enables opt-in remote `cortex enrich`; source upload requires `--allow-code-upload`. Never required for core graphing or MCP. |
| `[languages]` | tree-sitter and language grammars | Adds structural extraction for JS/TS/Go/Rust/Swift/Java/Ruby/C/C++ where grammars import cleanly; regex fallback remains available. |
| `[qml]` | `tree-sitter-language-pack` | Adds QML tree-sitter extraction through the bundled qmljs grammar; kept separate because the pack ships many grammars. |
| `[watch]` | `watchdog` | Improves `cortex watch`; polling fallback is stdlib-only. |
| `[tokens]` | `tiktoken` | Exact o200k_base BPE token counts everywhere Cortex counts/budgets tokens; without it, `count_text_tokens` uses a calibrated stdlib regex-segment heuristic (see Token Counting below). |
| `[semantic]` | `model2vec`, `numpy` | Optional local static embeddings using [`minishlab/potion-code-16M`](https://huggingface.co/minishlab/potion-code-16M); no vectors, model download, or network are used unless explicitly set up. |

The regex fallback is Qt-aware for C++/QML signal, slot, emit, connect, Q_OBJECT, and handler patterns.

### Optional local semantic retrieval

Install the strictly optional extra and explicitly cache the verified Model2Vec
provider model once:

```bash
python3 -m pip install -e ".[semantic]"
cortex semantic setup
cortex semantic status
cortex ingest .
```

`cortex semantic setup` is the only Cortex command allowed to fetch a model
(the verified static model is on the order of tens of MB). It saves the model
under `CORTEX_DATA_DIR/semantic/potion-code-16M`; ingest and
query load only that local directory, set offline provider flags, and never
contact Hugging Face or another network service. If the extra, model, or vector
index is absent or fails, Cortex silently uses its normal deterministic lexical
graph path. Set `CORTEX_SEMANTIC=0` to force that inactive/default behavior even
when a local model is installed. `cortex_overview` detailed responses report `installed`,
`enabled`, `active`, `model_ready`, `indexed_chunks`, and a non-network `reason` under `semantic`.

## Token Counting

`cortex.tokenizer.count_text_tokens(text, kind="code"|"markdown"|"text")` drives every budget decision (bundle packing, skeletons, truncation, MCP response budgets, `cortex saved` baselines). With the optional `[tokens]` extra installed, it returns an exact `tiktoken` o200k_base count. Without it (the default, stdlib-only install), it falls back to a deterministic regex-segment estimate scaled by a per-`kind` `CALIBRATION` factor in `src/cortex/tokenizer.py`, since a plain segment count is a biased estimate of real BPE tokens (differently biased for code vs. prose). Regenerate those factors for your own corpus with:

```bash
pip install tiktoken
python3 evals/calibrate_tokenizer.py [repo_path]
```

The checked-in factors were measured against this repository with `o200k_base` on 2026-07-17: `code=0.74`, `markdown=0.67`, and `text=0.77`. The aggregate estimate for every kind is therefore within 1% of the calibration corpus's exact count; regenerate them when validating against a substantially different corpus. The full measurement table is recorded in `evals/TOKENIZER_CALIBRATION.md`.

## Eval Numbers

Regenerated on 2026-10-17 for 0.10.0 with the dependency-free calibrated tokenizer path:

```bash
python3 evals/run_evals.py --stdlib-tokens
```

`--stdlib-tokens` makes the checked-in default-install rows reproducible even when the developer environment also has the optional `tiktoken` or `regex` packages installed. Omit it to exercise exact token counting when `[tokens]` is active.

The harness creates small git fixture repos (including a C++/Qt + QML fixture) at runtime and runs 17 default/off tasks. Run `python3 evals/perf_ingest.py` separately to enforce the P0-3 warm-refresh contract: one content read and under one second for a single changed file in a synthetic 2,000-file repository. It reports expected-file precision/recall, expected-symbol recall, token cost, and wall latency. Isolated optional vocabulary-gap and Qt click tasks run in explicit semantic-off and semantic-on modes only with `python3 evals/run_evals.py --semantic` when a real local model is already ready; no setup/download is attempted by the harness. Full per-task output is in `evals/RESULTS.md`.

| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens | Avg Latency ms |
|---|---:|---:|---:|---:|---:|---:|
| bfs | 17 | 0.291 | 0.608 | 0.985 | 512 | 16-19 |
| pagerank | 17 | 0.291 | 0.608 | 0.985 | 512 | 17-20 |
| skeleton_off | 17 | 0.613 | 0.608 | 0.865 | 171 | 17-20 |
| skeleton_on | 17 | 0.599 | 0.588 | 0.865 | 173 | 17-20 |

These 17-task precision/recall rows are identical to 0.9.2 (the call graph, impacted tests and rename-aware history did not change them); `tests/test_eval_baseline.py` pins them.

Interpretation: normal-budget PageRank and BFS recover almost all gold files/symbols across these fixtures but include extra files. The tight-budget modes trade some recall for a roughly threefold token reduction. Latency figures are wall-clock on a shared dev machine and fluctuate run to run; treat the ranges as illustrative, not a benchmark result.

These checked-in rows use the default stdlib segmenter with the measured P1-4 calibration factors. When the optional `[tokens]` extra is installed, Cortex uses exact `tiktoken` `o200k_base` counts instead; file selection can differ near a tight budget boundary.

#### OSS fixtures

Since 0.10.0 the harness also scores seven vendored, permissively licensed upstream snapshots under `evals/fixtures/oss/`, one per language family (pinned commits, licenses and prune rules in each `PROVENANCE.md`; 2.35 MB total). Each is copied into a temporary git repository with a fixed synthetic history, entirely offline (`--no-oss` skips them). The rows are reported apart from the historical matrix:

| Fixture | Family | Calls | Resolved ratio | Gold call pairs | Impacted tests: precision / recall | Structural tier only |
|---|---|---:|---:|---:|---:|---:|
| `cpp_qt_sortfilterproxymodel` | C/C++ with Qt | 632 | 0.297 | 4/4 | 1.000 / 0.125 | 1.000 / 0.125 |
| `ts_p_queue` | JS/TS | 4473 | 0.326 | 3/3 | 0.778 / 1.000 | 1.000 / 0.143 |
| `go_mux` | Go | 1998 | 0.258 | 4/4 | 0.182 / 1.000 | 1.000 / 0.500 |
| `rust_semver` | Rust | 741 | 0.574 | 4/4 | 0.500 / 1.000 | 1.000 / 1.000 |
| `swift_splash` | Swift | 666 | 0.228 | 3/3 | 1.000 / 1.000 | 1.000 / 1.000 |
| `java_javapoet` | Java | 6345 | 0.356 | 3/3 | 0.273 / 1.000 | 0.429 / 1.000 |
| `ruby_rack` | Ruby | 11890 | 0.245 | 3/3 | 1.000 / 1.000 | 1.000 / 1.000 |

How to read this: the resolved ratio is the share of all `calls` edges that resolve to a repository symbol. Most unresolved calls go into the standard library or framework (Qt, Foundation, `net/http`, the JDK, Ruby core) or through untyped receivers, which Cortex deliberately leaves as `name:<callee>` instead of guessing. `tests/test_evals.py` pins each ratio at its measured value minus 0.05. The impacted-test columns come from **one** synthetic edit per fixture, compared with a hand-reviewed gold set of the test files that reach the changed symbol. The gold sets were written by reading the tests, not from coverage, after Cortex's selections were visible, so treat the numbers as anecdotes, not per-language accuracy. Full-selection recall leans on the `cochange` tier, and the synthetic history couples many files, which flatters co-change. The structural tier alone is precise on these probes but misses tests reached only at runtime: QML test cases that instantiate a C++ type, and a TS queue class used through a default constructor argument. The OSS retrieval aggregate (14 tasks) is precision 0.39-0.41 and recall 0.80 at the normal budget (0.50-0.55 at the tight skeleton budget); `evals/RESULTS.md` has every row.

On this checkout (978 tracked files, of which 699 are vendored OSS eval fixtures and test goldens), `cortex benchmark . --budget 4000` with exact `tiktoken` counts measured 1,737,094 corpus tokens across 957 indexed sources versus 3,998 tokens per query on average, a 434.5× reduction. That ratio grew mostly because the corpus grew (the 0.9.x figure was 68.3× over 138 sources), not because bundles got smaller. Regenerate it after substantial corpus changes rather than treating it as a fixed product claim.

## Token Savings

Every successful call to a read/analysis tool (`cortex_query`, `cortex_overview`, `cortex_context`, `cortex_impact`, `cortex_search_symbols`, `cortex_read_symbol`, `cortex_read_file`, `cortex_relations`, `cortex_path`, `cortex_references`, `cortex_search_text`, `cortex_risk`, `cortex_dead_code`) is logged to a local `tool_usage` ledger with the response's actual token count and a deterministic baseline estimate of what an agent would have spent without Cortex. Run `cortex saved <repo>` to see it:

```bash
cortex saved . --daily
```

| Metric | Meaning |
|---|---|
| Response tokens | `count_text_tokens(json.dumps(payload))` for the actual MCP response. |
| Baseline tokens | For `cortex_context`, the raw content of every distinct resolved target-card path is counted once (using the stored content and tokenizer kind); neighbor/co-change/impact expansion paths are intentionally excluded because the counterfactual is reading the requested targets. `cortex_references` is priced against grep: one `path:line:<line text>` line per returned location, built from the same snippets. Other file-returning tools price the distinct files they return, while `cortex_read_symbol`'s `skeleton`/`signature` modes and `cortex_read_file`'s `skeleton` mode use the full-file baseline regardless of response size. For `cortex_search_symbols`/`cortex_relations`/`cortex_path`/`cortex_overview`: the token cost of the `detailed` rendering; `cortex_relations` and `cortex_path` also add referenced raw files. |
| Saved tokens | `baseline_tokens - response_tokens`, summed per tool, per day, and overall. |

The baseline policy lives in one auditable function, `_estimate_baseline` in `src/cortex/mcp/tools.py`. It is a proxy, not a measured "tokens not spent": for `cortex_search_symbols`/`cortex_overview` it only captures response-format savings (there's no single raw file backing an index/graph summary), and for `cortex_query`/`cortex_impact` it only prices the files Cortex actually returned, not the rest of the corpus an agent would otherwise have had to search through. Ledger writes are best-effort — a locked or missing database never surfaces as an MCP tool error. Add `--price-per-mtok <input>,<output>` to render dollar figures at your model's own rates (no prices are hardcoded); the ledger's baseline/response tokens are both priced at the input rate since they represent context read into an agent's own model, not model-generated output.

### How `_meta.saved_tokens` is computed

`_meta.saved_tokens` is `baseline_tokens - response_tokens` for that one call, and it is present only when positive. The baseline is the cost of the realistic alternative without Cortex: reading the referenced files in full for the file-returning tools, the `detailed` rendering of the same call for the structure-only tools, and `grep -rn` output (one `path:line:<line text>` line per returned location) for `cortex_references`. A references call whose JSON is as large as its grep output therefore reports no saving. The full policy is the docstring of `_estimate_baseline`.

## Cortex vs. The Field

- Serena: Cortex is not an LSP replacement; it focuses on durable repo graph memory, COCHANGE history, and token-budgeted retrieval over MCP.
- claude-context: Cortex does not require embeddings, a vector DB, or network services. The core is deterministic and SQLite local-first.
- repomix: Cortex is selective and graph-aware rather than whole-repo packing. It ranks files/symbols and can skeletonize code when the budget is tight.
- CodeGraph, GitNexus, Repowise: these lead with cross-file call graphs and "affected"/impacted-test commands. Since 0.10.0 Cortex has both, for Python, C/C++, JS/TS, Go, Rust, Swift, Java, and Ruby. Its call graph is resolved by a deterministic, declaration-driven resolver, and its impacted tests (`cortex_risk` `tests_to_run`) combine structural reachability with co-change history and file-name pairing, saying explicitly (`tests_run_all`) when it cannot vouch for a selection. Cortex still deliberately lacks a type checker or compiler integration (so resolution rates are modest on framework-heavy code), a language server, a file watcher inside the MCP server, and multi-repo workspaces.
- Cortex-specific edge: the COCHANGE temporal layer makes git history a first-class retrieval signal alongside structure and docs. History is rename-aware, and Qt/QML wiring (signals, slots, `emit`, `connect`, QML handlers, CMake/QRC registration) is part of the same graph as calls. Several Cortex processes can safely share one index.

## Stacking With tokenslim

Cortex and tokenslim solve different parts of the context problem. Cortex proactively selects the right repo context before or during a task; tokenslim reactively compresses large tool outputs after they happen. Prefer Cortex MCP tools for repo retrieval when possible because MCP results are structured and avoid shell-output truncation paths.

## Migrating From v0.1

The old `cortex claude install` and `cortex codex install` string-injection commands were removed. Use plugin manifests and MCP registration instead.

For repos that previously used injected `AGENTS.md` or `CLAUDE.md` sections:

```bash
cortex migrate .
```

Then configure the plugin/MCP setup for your host and run:

```bash
cortex refresh .
```

## Development

```bash
python3 -m pytest tests/ -q
python3 evals/run_evals.py
python3 -m build
```

The core package has zero required runtime dependencies. Keep new eval fixtures small enough that the eval suite runs in seconds.
