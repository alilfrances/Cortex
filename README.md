# Cortex

Cortex is a graph-aware, local-first context engine for code agents. It ingests a git repository into a deterministic SQLite store, builds STRUCTURAL, COCHANGE, and HEADING graph layers, ranks context with personalized PageRank, and packs task-focused bundles with symbol skeletons when budgets are tight.

The result is a repo-native context service: MCP tools for live agent queries, CLI commands for reports and exports, and no required network, embedding, vector DB, or LLM dependency.

Current package and plugin metadata is `0.7.5` (`pyproject.toml` and `.codex-plugin/plugin.json`). Use that as the shipped docs version.

## What Cortex Builds

- STRUCTURAL layer: files, imports, definitions, symbol nodes, and contains edges.
- COCHANGE layer: git history coupling between files changed together.
- HEADING layer: Markdown sections for docs and planning context.
- Ranking: personalized PageRank by default, with BFS available for comparison; optional churn×complexity hotspot boosting is opt-in.
- Hotspots: deterministic per-language complexity and git touch-count analytics persisted with file nodes.
- Packing: full files when they fit; Python skeletons with imports, signatures, spans, and hashes under tight budgets.
- Communities: local graph clustering for reports and architecture overviews.
- Transport: stdio MCP server plus plugin manifests for Claude Code and Codex.

## Install

Requires Python 3.11+ on PATH as `python3`. Nothing else — the core is stdlib-only and the plugin runs straight from its install directory, no `pip install` needed.

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

That's the full plugin path. It installs the Cortex plugin bundle, which registers the Cortex MCP server (`.mcp.json` launches `bin/cortex-mcp.py`, which self-locates its own `src/`), the `cortex` skill, and the session-start hook. In a project, ask Claude to call `cortex_refresh` once to build the index (or run `cortex ingest .` if you installed the CLI).

> **Note:** Plugins load at session start. After installing or updating, restart Claude Code (or run `/reload-plugins` if available) — sessions that were already open won't see the MCP tools, skill, or hook.

For local development of the plugin itself:

```bash
claude --plugin-dir /path/to/Cortex
```

## Hooks

Cortex ports graphify's agent-context behavior as a native Claude Code `SessionStart` hook. When a project has a Cortex index (legacy `.cortex/cortex.db` in-repo, or the central store under `~/.cortex/data/`), the hook quickly compares the stored repo fingerprint with the current `compute_repo_fingerprint` value and injects short context saying whether the index is fresh or stale, how many files are indexed, and to prefer `cortex_context` (one batch of paths/symbols before editing several files), `cortex_query`, `cortex_search_symbols`, and `cortex_impact` before raw grep-style exploration. If no database exists, it emits a one-line hint that `cortex_refresh` can build it.

The hook is advisory and fail-open: it never runs ingest, exits quietly on malformed or unreadable databases, and stays silent entirely when the working directory is not inside a git repository. Staleness resolves itself at query time — the MCP read tools auto-refresh incrementally before answering — so the hook only informs.

### Codex

```bash
codex plugin marketplace add alilfrances/Cortex
codex plugin add cortex@cortex
```

This is the official Codex marketplace flow: first add the marketplace source (the current repo remote, `alilfrances/Cortex`), then install the `cortex` plugin from the `cortex` marketplace. OpenAI's Codex plugin docs cover [`codex plugin marketplace add`](https://developers.openai.com/codex/plugins/build#add-a-marketplace-from-the-cli) and [`codex plugin add`](https://developers.openai.com/codex/cli/reference#codex-plugin).

Start a new Codex session after installation so the plugin MCP server, skill, and hook are loaded.

Alternative: if you only want MCP server registration in Codex and do not want to install the marketplace plugin bundle, use:

```bash
/path/to/Cortex/install.sh --codex
```

`install.sh --codex` writes an absolute `mcp_servers.cortex` entry to `~/.codex/config.toml` (idempotent; no pip), but it does not install the Codex plugin bundle or add a marketplace source. The Codex plugin manifest in this repo points to `./skills/` and `./.mcp.json`; for manual setup, point Codex at the repo root as a plugin dir or copy `skills/cortex` to `~/.codex/skills/`. Manual MCP registration equivalent:

```toml
[mcp_servers.cortex]
command = "python3"
args = ["/path/to/Cortex/bin/cortex-mcp.py"]
```

### Optional: CLI + extras (pip)

The `cortex` CLI and optional features need a pip install:

```bash
cd /path/to/Cortex
python3 -m pip install -e .                                 # cortex CLI
python3 -m pip install -e ".[llm,languages,watch,tokens]"   # enrichment, tree-sitter, watchdog, exact tokenizer
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

## MCP Tools

| Tool | What it does | Example prompt |
|---|---|---|
| `cortex_query` | Builds a task-focused retrieval bundle under a token budget; pass `hotspot_boost: true` only when churn×complexity should influence ranking. | "Use Cortex to find the files and symbols for adding password reset." |
| `cortex_overview` | Returns a compact repo overview, including `top_hotspots`, from the stored graph and report data. | "Ask Cortex for an overview before we refactor the API layer." |
| `cortex_impact` | Ranks structural and co-change neighbors for a file or symbol. | "Use Cortex impact on `src/cortex/bundle.py` before changing packing." |
| `cortex_context` | Batches paths and symbols into one compact triage card per target; default budget 2000, with optional `include: ["impact", "cochange", "symbols"]` expansions. Cards include structural/co-change neighbors, hotspots, spans, and Qt/QML signals, slots, handlers, wiring, and instantiations. If an intentionally tiny budget cannot fit irreducible card metadata, `budget_feasible: false` explains the condition while preserving every original target. | "Before editing several files, call Cortex context once with all their paths and symbols." |
| `cortex_risk` | Runs a local git diff and returns deterministic 0–10 per-file risk plus missing co-change/test/Qt/build-reference directives; supports `range`, `staged`, and a response budget. | "Check `cortex_risk` before committing this change." |
| `cortex_dead_code` | Finds conservative high/medium/low dead-code candidates from the persisted symbol graph and local grep references, excluding Python entry points and Qt meta-object runtime surfaces; supports an optional response budget. | "Find dead-code candidates and keep Qt slots, signals, handlers, and QML types out of the report." |
| `cortex_search_symbols` | Searches indexed file and symbol nodes by name. | "Search Cortex symbols for `SessionStore` and related methods." |
| `cortex_read_symbol` | Returns source for one indexed symbol span; `mode` picks `full` (numbered lines, default), `skeleton` (signature + nested member signatures, bodies elided), or `signature` (signature line + span only). | "Read the `generate_bundle` symbol with Cortex instead of opening the whole file." |
| `cortex_read_file` | Direct replacement for the built-in `Read` tool on an indexed file; `mode: "skeleton"` (default) returns imports/includes + every top-level signature with bodies elided, `mode: "full"` returns the exact indexed content. | "Read `src/cortex/bundle.py` with Cortex instead of the raw file." |
| `cortex_relations` | Returns parsed graph edges such as imports, calls, inherits, emits, connects, handles, and QML `instantiates` wiring. | "Show Cortex relations for `generate_bundle` outgoing calls." |
| `cortex_references` | Returns cross-file references from parsed graph edges plus repo grep, bucketed by file type. | "Find all references to `_ensure_fresh` with Cortex." |
| `cortex_search_text` | Full-text body search (FTS5 BM25) across indexed file contents, with line-anchored snippets — a grep replacement over string literals, error messages, comments, and prose that symbol search can't see. | "Search Cortex text for the 'device offline' error message." |
| `cortex_refresh` | Re-ingests the repo and updates freshness metadata. | "Refresh Cortex, then query the checkout flow again." |

Tool results include provenance where available. Read/query/analysis tools keep the index fresh automatically: when the repository fingerprint has changed since ingestion, they run an incremental re-ingest (changed, new, and deleted files only) before answering and report the delta under `auto_refreshed`. Set `CORTEX_AUTO_REFRESH=0` to disable and fall back to stale-state hints plus manual `cortex_refresh`. If no index exists yet, indexed read/query tools still require an explicit `cortex_refresh` first; `cortex_risk` can run a clearly marked partial git-only analysis.

`cortex_query`, `cortex_impact`, and `cortex_overview` cache their result under a key derived from the (post-refresh) repo fingerprint, tool name, and call arguments, so a repeated identical call skips PageRank/packing entirely and returns the prior response data unchanged — any file change invalidates the cache automatically, since it changes the fingerprint. Set `CORTEX_QUERY_CACHE=0` to disable both reading and writing this cache. `cortex gc` prunes cached rows older than 30 days or beyond 200 rows per repo.

The MCP surface has 13 tools: 12 read/query/analysis tools (everything above except `cortex_refresh`) can carry a `_meta` object: `{index_age_seconds, indexed_at, fingerprint_fresh, auto_refreshed?, cached?, saved_tokens?}`. `detailed` responses always include it; `concise` responses include it only when something is worth surfacing — the index is stale, an auto-refresh just ran, the response came from the P1-3 cache (`cached: true`), or the call saved a meaningful number of tokens over a raw file read (`saved_tokens`, exactly the number recorded in the `tool_usage` ledger — see `cortex saved` below) — otherwise it's omitted so a routine concise call costs no extra tokens. A cache hit always reports the *current* index age, never a value frozen from when the cache entry was written.

## CLI Reference

| Command | Purpose |
|---|---|
| `cortex ingest <repo> [--commits 50] [--update]` | Scan source files, git history, graph layers, symbols, and fingerprints into SQLite. |
| `cortex bundle <repo> --task "..." [--budget 4000] [--rank pagerank\|bfs] [--hotspot-boost] [--format md\|json]` | Emit a token-budgeted context bundle. |
| `cortex report <repo> [--out] [--include-test-pairs]` | Write an architecture report with central nodes, hotspots, communities, connections, and a confidence-tiered dead-code section. |
| `cortex risk [range] [--staged] [--format text|json] [--db PATH]` | Analyze a committed range (default `HEAD~1..HEAD`) or staged diff with deterministic per-file risk and missing-context directives. It runs local git only; no index is required, but an unindexed/partial result says so explicitly. |
| `cortex gc [--prune]` | List central data dirs (`--prune` deletes ones whose repo is gone) and prune each repo's query result cache. |
| `cortex enrich <repo> --provider claude\|codex [--force]` | Optional LLM semantic enrichment with local cache. Requires `[llm]`. |
| `cortex semantic setup [--force]` | Explicitly download/cache `minishlab/potion-code-16M` below `CORTEX_DATA_DIR`; this is the only download path. |
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

## Extras

| Extra | Adds | Notes |
|---|---|---|
| `[llm]` | `anthropic`, `openai` | Enables `cortex enrich`; never required for core graphing or MCP. |
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

and paste the printed per-kind factors into `CALIBRATION`. The checked-in factors currently ship as provisional `1.0` (no-op) placeholders — see the CHANGELOG's P1-4 entry for why — so re-running the script in a network-enabled environment and updating those constants is a recommended follow-up.

## Eval Numbers

Regenerated on 2026-07-16 with:

```bash
python3 evals/run_evals.py
```

The harness creates small git fixture repos (including a C++/Qt + QML fixture) at runtime and runs 17 default/off tasks. It reports expected-file precision/recall, expected-symbol recall, token cost, and wall latency. Isolated optional vocabulary-gap and Qt click tasks run in explicit semantic-off and semantic-on modes only with `python3 evals/run_evals.py --semantic` when a real local model is already ready; no setup/download is attempted by the harness. Full per-task output is in `evals/RESULTS.md`.

| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens | Avg Latency ms |
|---|---:|---:|---:|---:|---:|---:|
| bfs | 17 | 0.303 | 0.647 | 0.956 | 630 | 10-15 |
| pagerank | 17 | 0.304 | 0.647 | 0.956 | 632 | 11-15 |
| skeleton_off | 17 | 0.618 | 0.627 | 0.853 | 178 | 12-18 |
| skeleton_on | 17 | 0.588 | 0.627 | 0.853 | 176 | 12-16 |

Interpretation: normal-budget PageRank and BFS both recover almost all gold files/symbols across these fixtures but include extra files. Tight-budget skeleton packing improves recall versus tight-budget truncation while keeping token cost nearly flat. Latency figures are wall-clock on a shared dev sandbox and fluctuate run to run; treat the ranges as illustrative, not a benchmark result.

Token counts reflect Cortex's tokenizer (P1-4): exact `tiktoken` o200k_base counts when the optional `[tokens]` extra is installed, otherwise the calibrated stdlib heuristic described in Token Counting above (currently shipping with provisional 1.0/no-op calibration factors, so these numbers are unchanged from the pre-P1-4 heuristic until real factors are measured and baked in).

## Token Savings

Every successful call to a read/analysis tool (`cortex_query`, `cortex_overview`, `cortex_context`, `cortex_impact`, `cortex_search_symbols`, `cortex_read_symbol`, `cortex_read_file`, `cortex_relations`, `cortex_references`, `cortex_search_text`, `cortex_risk`, `cortex_dead_code`) is logged to a local `tool_usage` ledger with the response's actual token count and a deterministic baseline estimate of what an agent would have spent without Cortex. Run `cortex saved <repo>` to see it:

```bash
cortex saved . --daily
```

| Metric | Meaning |
|---|---|
| Response tokens | `count_text_tokens(json.dumps(payload))` for the actual MCP response. |
| Baseline tokens | For `cortex_context`, the raw content of every distinct resolved target-card path is counted once (`store.fetch_source_content`); neighbor/co-change/impact expansion paths are intentionally excluded because the counterfactual is reading the requested targets. Other file-returning tools price the distinct files they return, while `cortex_read_symbol`'s `skeleton`/`signature` modes and `cortex_read_file`'s `skeleton` mode use the full-file baseline regardless of response size. For `cortex_search_symbols`/`cortex_relations`/`cortex_overview`: the token cost of the `detailed` rendering; `cortex_relations` also adds referenced raw files. |
| Saved tokens | `baseline_tokens - response_tokens`, summed per tool, per day, and overall. |

The baseline policy lives in one auditable function, `_estimate_baseline` in `src/cortex/mcp/tools.py`. It is a proxy, not a measured "tokens not spent": for `cortex_search_symbols`/`cortex_overview` it only captures response-format savings (there's no single raw file backing an index/graph summary), and for `cortex_query`/`cortex_impact` it only prices the files Cortex actually returned, not the rest of the corpus an agent would otherwise have had to search through. Ledger writes are best-effort — a locked or missing database never surfaces as an MCP tool error. Add `--price-per-mtok <input>,<output>` to render dollar figures at your model's own rates (no prices are hardcoded); the ledger's baseline/response tokens are both priced at the input rate since they represent context read into an agent's own model, not model-generated output.

## Cortex vs. The Field

- Serena: Cortex is not an LSP replacement; it focuses on durable repo graph memory, COCHANGE history, and token-budgeted retrieval over MCP.
- claude-context: Cortex does not require embeddings, a vector DB, or network services. The core is deterministic and SQLite local-first.
- repomix: Cortex is selective and graph-aware rather than whole-repo packing. It ranks files/symbols and can skeletonize code when the budget is tight.
- Cortex-specific edge: the COCHANGE temporal layer makes git history a first-class retrieval signal alongside structure and docs.

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
