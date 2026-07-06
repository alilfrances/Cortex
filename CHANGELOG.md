# Changelog

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
