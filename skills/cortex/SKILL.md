---
name: cortex
description: Use Cortex MCP tools for repo-first context, graph-aware retrieval, impact analysis, symbol search, and refreshing local Cortex state before broad raw-file exploration.
---

# Cortex

Use Cortex when working inside an indexed repository and you need code context, symbol lookup, blast-radius checks, or repository orientation. Prefer Cortex before broad grep/Read exploration because it combines source content, symbol spans, graph edges, co-change history, and token-budgeted packing.

## Workflow

1. Start with `cortex_overview` for unfamiliar repos or architecture questions.
2. For a concrete task, call `cortex_query` with the task and a budget.
3. For named code, use the search -> read -> impact loop:
   - `cortex_search_symbols` to locate candidate functions/classes/methods.
   - `cortex_read_symbol` with the chosen `node_id` to read only the exact numbered source span.
   - `cortex_impact` on the containing file to inspect structural and co-change neighbors before editing.
4. Use `cortex_relations` for parsed graph questions such as imports, contains, inherits, emits, connects, or handles.
5. Use `cortex_references` when configs, docs, scripts, CMake/QRC, or other parser-missed surfaces may reference a symbol.
6. Default to `response_format: "concise"`; pass `response_format: "detailed"` only when you need provenance, fingerprints, full metadata, or detailed why-edges.
7. If a tool reports `stale: true`, call `cortex_refresh` or rerun the read tool after refresh.

## Tools

### `cortex_query`

Returns a ranked, token-budgeted bundle for a task. Use for "what files matter for this change?" questions before raw file reads. Concise mode keeps compact per-item rationale.

Example:

```json
{"task":"fix stale index detection in the auto refresh path","budget":4000}
```

### `cortex_search_symbols`

Returns symbol candidates without source bodies. Use when the user names an identifier or when `cortex_query` suggests a file and you need a precise function/class.

Example:

```json
{"query":"generate bundle"}
```

### `cortex_read_symbol`

Returns exact stored source lines for one symbol span, formatted as `line_number: source`. Use after `cortex_search_symbols`; if the result is ambiguous, call again with the returned `node_id`.

Example:

```json
{"symbol":"symbol:src/cortex/bundle.py:generate_bundle","budget":2000}
```

### `cortex_impact`

Returns files related by STRUCTURAL and COCHANGE edges. Use after selecting a file or before editing to find likely tests, callers, and coupled modules.

Example:

```json
{"path":"src/cortex/store.py","limit":10}
```

### `cortex_relations`

Returns parsed graph edges filtered by relation and symbol. Use for structural questions where parser coverage is enough.

Example:

```json
{"relation":"imports","symbol":"bundle","direction":"both"}
```

### `cortex_references`

Returns graph plus grep references bucketed by file type. Use for cross-language or config/doc/script blast radius.

Example:

```json
{"symbol":"_ensure_fresh","budget":2000}
```

### `cortex_overview`

Returns repository graph summary, communities, god nodes, and surprising links. Use for orientation rather than precise code reading.

Example:

```json
{"repo_path":"."}
```

### `cortex_refresh`

Re-ingests the repo into the local SQLite index. Use when no database exists, after large file changes, or when stale results are reported.
