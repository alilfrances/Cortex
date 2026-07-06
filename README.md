# Cortex

Cortex is a graph-aware, local-first context engine for code agents. It ingests a git repository into a deterministic SQLite store, builds STRUCTURAL, COCHANGE, and HEADING graph layers, ranks context with personalized PageRank, and packs task-focused bundles with symbol skeletons when budgets are tight.

The result is a repo-native context service: MCP tools for live agent queries, CLI commands for reports and exports, and no required network, embedding, vector DB, or LLM dependency.

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

### Claude Code (one-command plugin install)

```bash
claude plugin marketplace add alilfrances/Cortex
claude plugin install cortex@cortex
```

That's it. The plugin registers the Cortex MCP server (`.mcp.json` launches `bin/cortex-mcp.py`, which self-locates its own `src/`), the `cortex` skill, and everything else. In a project, ask Claude to call `cortex_refresh` once to build the index (or run `cortex ingest .` if you installed the CLI).

For local development of the plugin itself:

```bash
claude --plugin-dir /path/to/Cortex
```

### Codex

Cortex ships `.codex-plugin/plugin.json`, `skills/cortex/SKILL.md`, and `.mcp.json`. Install through your Codex plugin flow, or register the MCP server explicitly in `~/.codex/config.toml`:

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

This creates `.cortex/cortex.db` and `.cortex/cortex_report.md` inside the target repo.

## MCP Tools

| Tool | What it does | Example prompt |
|---|---|---|
| `cortex_query` | Builds a task-focused retrieval bundle under a token budget. | "Use Cortex to find the files and symbols for adding password reset." |
| `cortex_overview` | Returns a compact repo overview from the stored graph and report data. | "Ask Cortex for an overview before we refactor the API layer." |
| `cortex_impact` | Ranks structural and co-change neighbors for a file or symbol. | "Use Cortex impact on `src/cortex/bundle.py` before changing packing." |
| `cortex_search_symbols` | Searches indexed file and symbol nodes by name. | "Search Cortex symbols for `SessionStore` and related methods." |
| `cortex_refresh` | Re-ingests the repo and updates freshness metadata. | "Refresh Cortex, then query the checkout flow again." |

Tool results include provenance where available, plus stale-state hints when the repository fingerprint changed since ingestion.

## CLI Reference

| Command | Purpose |
|---|---|
| `cortex ingest <repo> [--commits 50] [--update]` | Scan source files, git history, graph layers, symbols, and fingerprints into SQLite. |
| `cortex bundle <repo> --task "..." [--budget 4000] [--rank pagerank\|bfs] [--format md\|json]` | Emit a token-budgeted context bundle. |
| `cortex report <repo> [--out .cortex] [--include-test-pairs]` | Write an architecture report with central nodes, communities, and connections. |
| `cortex enrich <repo> --provider claude\|codex [--force]` | Optional LLM semantic enrichment with local cache. Requires `[llm]`. |
| `cortex benchmark <repo> [--budget 4000] [--format text\|json]` | Compare bundle token cost against full-corpus reading. |
| `cortex mcp` | Run the stdio MCP server. |
| `cortex migrate [project_dir]` | Remove old injected v0.1 `## cortex` guidance and point users to plugin setup. |
| `cortex graph export <repo> --format graphml\|json\|obsidian --out <path>` | Export the stored graph. |
| `cortex graph view <repo> --out cortex-graph.html` | Write a self-contained no-CDN HTML graph viewer. |
| `cortex watch <repo> [--interval 30]` | Refresh on changes using watchdog when installed, polling otherwise. |
| `cortex hook install\|uninstall\|status [project_dir]` | Manage repo-local git hooks that run `cortex refresh`. |

`cortex refresh <repo>` is also available as a convenience command for ingest plus report generation.

## Extras

| Extra | Adds | Notes |
|---|---|---|
| `[llm]` | `anthropic`, `openai` | Enables `cortex enrich`; never required for core graphing or MCP. |
| `[languages]` | tree-sitter and language grammars | Adds structural extraction for JS/TS/Go/Rust/Swift/Java/Ruby where grammars import cleanly; regex fallback remains available. |
| `[watch]` | `watchdog` | Improves `cortex watch`; polling fallback is stdlib-only. |

## Eval Numbers

Regenerated on 2026-07-06 with:

```bash
python3 evals/run_evals.py
```

The harness creates two small git fixture repos at runtime and runs 10 gold tasks. It reports expected-file precision/recall, expected-symbol recall, token cost, and wall latency. Full per-task output is in `evals/RESULTS.md`.

| Mode | Tasks | Precision | Recall | Avg Tokens | Avg Latency ms |
|---|---:|---:|---:|---:|---:|
| bfs | 10 | 0.300 | 1.000 | 655 | 9.9 |
| pagerank | 10 | 0.300 | 1.000 | 655 | 11.5 |
| skeleton_off | 10 | 0.733 | 0.725 | 175 | 11.0 |
| skeleton_on | 10 | 0.750 | 0.825 | 173 | 11.0 |

Interpretation: normal-budget PageRank and BFS both recover all gold files/symbols in these small fixtures but include extra files. Tight-budget skeleton packing improves recall versus tight-budget truncation while keeping token cost nearly flat.

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
