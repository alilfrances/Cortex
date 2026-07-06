---
name: cortex
description: Use Cortex MCP tools for repo-first context, graph-aware retrieval, impact analysis, symbol search, and refreshing local Cortex state before broad raw-file exploration.
---

# Cortex

Use Cortex when working in a git repository and the task needs codebase orientation, impact analysis, relevant files, related symbols, or a compact context bundle. Prefer Cortex before broad raw-file searches when `.cortex/` exists or when a Cortex MCP server is available.

## Workflow

1. If Cortex state may be missing or stale, call `cortex_refresh`.
2. For a broad architecture or orientation question, call `cortex_overview`.
3. For a concrete task, bug, feature, or question, call `cortex_query` with the task and token budget.
4. For change-risk analysis around a file or symbol, call `cortex_impact`.
5. For direct symbol lookup, call `cortex_search_symbols`.

## MCP Tools

### `cortex_query`

Use for task-focused retrieval. Pass the user's task, optional `repo_path`, and a reasonable `budget`. The result is a token-budgeted bundle of relevant files and symbols with provenance explaining why each item was selected.

### `cortex_overview`

Use for initial repo orientation, architecture questions, and planning. It returns the repository report, central graph nodes, communities, and high-level structure.

### `cortex_impact`

Use before editing a file or symbol, or when reviewing a proposed change. It returns structurally related and cochanged neighbors ranked by connection strength, plus why those nodes may be affected.

### `cortex_search_symbols`

Use when the user names a function, class, method, module, or file-like identifier. Search first, then use `cortex_query` or `cortex_impact` on promising results if more context is needed.

### `cortex_refresh`

Use when `.cortex/cortex.db` is missing, stale, or the user asks to refresh repo context. It re-ingests the repo and writes the default Cortex report. Prefer this over manual ingest/report commands when the MCP server is available.

## Fallback

If the MCP server is unavailable but the `cortex` command exists, run:

```bash
cortex refresh .
cortex bundle . --task "<task>" --budget 4000
```

If the host cannot find `cortex`, configure the MCP server command as:

```bash
python3 -m cortex.mcp.server
```
