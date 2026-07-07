# Changelog

## 0.3.0 - 2026-07-07

Add C, C++, and QML structural extraction (tree-sitter + regex fallback), including Qt-aware signal, slot, emit, connect, Q_OBJECT, and QML handler detection.


## 0.2.3 - 2026-07-07

Auto-refresh: the index now keeps itself current — no manual `cortex_refresh` discipline needed.

- MCP read tools (`cortex_query`, `cortex_overview`, `cortex_impact`, `cortex_search_symbols`) detect a stale fingerprint and run an incremental ingest before answering, so results always reflect the working tree. Responses report what changed under `auto_refreshed`. Disable with `CORTEX_AUTO_REFRESH=0`.
- Missing-database behavior is unchanged: read tools still error with a `cortex_refresh` hint rather than building an index implicitly.
- Incremental ingest now removes deleted files from the index (`deleted_files` in the summary) instead of leaving ghost sources behind.
- Fixed stale graph rows surviving incremental ingest: graph saves during incremental runs now replace the repo graph wholesale instead of upserting, so symbols removed from a file disappear from search results.

## 0.2.2 - 2026-07-06

Retrieval-quality fixes from a live efficacy eval on the Cortex repo itself, where a bundle for an implementation question returned docs plus a stale `build/` duplicate and missed the real source file.

- Ingest now honors `.gitignore` (via `git ls-files --cached --others --exclude-standard`) and skips common artifact dirs (`build`, `dist`, `dist-check`, `.venv`, `venv`, `.tox`, `.eggs`, `.ruff_cache`) so stale copies never enter the graph. Repo fingerprints use the same file listing.
- Bundle scoring adds a name-match bonus: a task term that exactly matches a file stem or symbol name now outranks keyword-dense docs.
- Markdown items are capped at 40% of the bundle budget whenever code candidates also match, so docs can't crowd out implementation files. Docs-only repos are unaffected.
- SessionStart hook stays silent outside git repositories instead of nudging about a missing index.
- New `noisy_lib` eval fixture (fat README, doc plans, gitignored `build/` duplicate) with implementation-detail gold tasks to catch this failure class in CI.
- All manifest versions aligned at 0.2.2; manifest test now asserts `hooks/hooks.json` is the single source of hook wiring.

## 0.2.0 - 2026-07-06

- Fixed release hygiene issues: package naming in docs/errors, stale extras, author metadata, heading extraction, code-file classification, and removed the no-op ingest enrichment flag.
- Replaced BFS-first bundle ranking with pure-Python personalized PageRank while keeping `--rank bfs` for comparison.
- Added symbol granularity, additive SQLite migrations, symbol nodes, source spans, signatures, and tight-budget skeleton packing.
- Added a stdio MCP server with `cortex_query`, `cortex_overview`, `cortex_impact`, `cortex_search_symbols`, `cortex_refresh`, fingerprint staleness checks, and structured tool errors.
- Added Claude Code and Codex plugin packaging, shared skill content, MCP manifests, migration cleanup for old injected guidance, and manifest-version tests.
- Added optional tree-sitter multi-language structural extraction with regex fallback.
- Added graph export, no-CDN HTML viewer, Obsidian export, watch mode, and git-hook refresh support.
- Added a stdlib-only eval harness with runtime fixture repos, gold tasks, ranking comparison, skeleton comparison, token cost, and latency reporting.

## 0.1.0 - 2026-04-23

- Initial Cortex thin slice: local repository ingestion, SQLite store, graph records, commit provenance, token-budgeted bundles, benchmark command, and architecture reports.
