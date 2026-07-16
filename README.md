# Cortex

Cortex is a graph-aware, local-first context engine for code agents. It ingests a git repository into a deterministic SQLite store, builds STRUCTURAL, COCHANGE, and HEADING graph layers, ranks context with personalized PageRank, and packs task-focused bundles with symbol skeletons when budgets are tight.

The result is a repo-native context service: MCP tools for live agent queries, CLI commands for reports and exports, and no required network, embedding, vector DB, or LLM dependency.

Current package and plugin metadata is `0.7.5` (`pyproject.toml` and `.codex-plugin/plugin.json`). Use that as the shipped docs version.

## What Cortex Builds

- STRUCTURAL layer: files, imports, definitions, symbol nodes, and contains edges.
- COCHANGE layer: git history coupling between files changed together.
- HEADING layer: Markdown sections for docs and planning context.
- Ranking: personalized PageRank by default, with BFS available for comparison.
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

Cortex ports graphify's agent-context behavior as a native Claude Code `SessionStart` hook. When a project has a Cortex index (legacy `.cortex/cortex.db` in-repo, or the central store under `~/.cortex/data/`), the hook quickly compares the stored repo fingerprint with the current `compute_repo_fingerprint` value and injects short context saying whether the index is fresh or stale, how many files are indexed, and to prefer `cortex_query`, `cortex_search_symbols`, and `cortex_impact` before raw grep-style exploration. If no database exists, it emits a one-line hint that `cortex_refresh` can build it.

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
python3 -m pip install -e .                          # cortex CLI
python3 -m pip install -e ".[llm,languages,watch]"   # enrichment, tree-sitter, watchdog
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
| `cortex_query` | Builds a task-focused retrieval bundle under a token budget. | "Use Cortex to find the files and symbols for adding password reset." |
| `cortex_overview` | Returns a compact repo overview from the stored graph and report data. | "Ask Cortex for an overview before we refactor the API layer." |
| `cortex_impact` | Ranks structural and co-change neighbors for a file or symbol. | "Use Cortex impact on `src/cortex/bundle.py` before changing packing." |
| `cortex_search_symbols` | Searches indexed file and symbol nodes by name. | "Search Cortex symbols for `SessionStore` and related methods." |
| `cortex_read_symbol` | Returns numbered source lines for one indexed symbol span. | "Read the `generate_bundle` symbol with Cortex instead of opening the whole file." |
| `cortex_relations` | Returns parsed graph edges such as imports, calls, inherits, emits, and connects. | "Show Cortex relations for `generate_bundle` outgoing calls." |
| `cortex_references` | Returns cross-file references from parsed graph edges plus repo grep, bucketed by file type. | "Find all references to `_ensure_fresh` with Cortex." |
| `cortex_refresh` | Re-ingests the repo and updates freshness metadata. | "Refresh Cortex, then query the checkout flow again." |

Tool results include provenance where available. Read tools keep the index fresh automatically: when the repository fingerprint has changed since ingestion, they run an incremental re-ingest (changed, new, and deleted files only) before answering and report the delta under `auto_refreshed`. Set `CORTEX_AUTO_REFRESH=0` to disable and fall back to stale-state hints plus manual `cortex_refresh`. If no index exists yet, read tools still require an explicit `cortex_refresh` first.

## CLI Reference

| Command | Purpose |
|---|---|
| `cortex ingest <repo> [--commits 50] [--update]` | Scan source files, git history, graph layers, symbols, and fingerprints into SQLite. |
| `cortex bundle <repo> --task "..." [--budget 4000] [--rank pagerank\|bfs] [--format md\|json]` | Emit a token-budgeted context bundle. |
| `cortex report <repo> [--out] [--include-test-pairs]` | Write an architecture report with central nodes, communities, and connections. |
| `cortex gc [--prune]` | List central data dirs; `--prune` deletes ones whose repo is gone. |
| `cortex enrich <repo> --provider claude\|codex [--force]` | Optional LLM semantic enrichment with local cache. Requires `[llm]`. |
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

## Extras

| Extra | Adds | Notes |
|---|---|---|
| `[llm]` | `anthropic`, `openai` | Enables `cortex enrich`; never required for core graphing or MCP. |
| `[languages]` | tree-sitter and language grammars | Adds structural extraction for JS/TS/Go/Rust/Swift/Java/Ruby/C/C++ where grammars import cleanly; regex fallback remains available. |
| `[qml]` | `tree-sitter-language-pack` | Adds QML tree-sitter extraction through the bundled qmljs grammar; kept separate because the pack ships many grammars. |
| `[watch]` | `watchdog` | Improves `cortex watch`; polling fallback is stdlib-only. |

The regex fallback is Qt-aware for C++/QML signal, slot, emit, connect, Q_OBJECT, and handler patterns.

## Eval Numbers

Regenerated on 2026-07-06 with:

```bash
python3 evals/run_evals.py
```

The harness creates two small git fixture repos at runtime and runs 13 tasks. It reports expected-file precision/recall, expected-symbol recall, token cost, and wall latency. Full per-task output is in `evals/RESULTS.md`.

| Mode | Tasks | Precision | Recall | Symbol Recall | Avg Tokens | Avg Latency ms |
|---|---:|---:|---:|---:|---:|---:|
| bfs | 13 | 0.293 | 0.692 | 1.000 | 677 | 12.4 |
| pagerank | 13 | 0.290 | 0.692 | 1.000 | 678 | 13.9 |
| skeleton_off | 13 | 0.679 | 0.692 | 0.942 | 179 | 14.1 |
| skeleton_on | 13 | 0.641 | 0.692 | 0.942 | 176 | 13.7 |

Interpretation: normal-budget PageRank and BFS both recover all gold files/symbols in these small fixtures but include extra files. Tight-budget skeleton packing improves recall versus tight-budget truncation while keeping token cost nearly flat.

## Token Savings

Every successful call to a read tool (`cortex_query`, `cortex_overview`, `cortex_impact`, `cortex_search_symbols`, `cortex_read_symbol`, `cortex_relations`, `cortex_references`) is logged to a local `tool_usage` ledger with the response's actual token count and a deterministic baseline estimate of what an agent would have spent without Cortex. Run `cortex saved <repo>` to see it:

```bash
cortex saved . --daily
```

| Metric | Meaning |
|---|---|
| Response tokens | `count_text_tokens(json.dumps(payload))` for the actual MCP response. |
| Baseline tokens | For file-returning tools: the raw content of every distinct file referenced in the response, read in full (`store.fetch_source_content`) — what an agent would have spent with plain Read/grep. For `cortex_search_symbols`/`cortex_relations`/`cortex_overview`: the token cost of the `detailed` rendering of the same call (the savings the concise format already provides); `cortex_relations` also adds the referenced files' raw content. |
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
