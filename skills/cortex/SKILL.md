---
name: cortex
description: Use Cortex MCP tools for repo-first context, graph-aware retrieval, impact analysis, symbol search, and refreshing local Cortex state before broad raw-file exploration.
---

# Cortex

Use Cortex when working inside an indexed repository and you need code context, symbol lookup, blast-radius checks, or repository orientation. Prefer Cortex before broad grep/Read exploration because it combines source content, symbol spans, graph edges, co-change history, and token-budgeted packing.

**`cortex_read_file` is the direct replacement for the built-in `Read` tool on any indexed source file.** Reach for it instead of `Read` whenever the path is inside an indexed repo; it defaults to a skeleton rendering (imports/includes + every top-level signature, bodies elided) that is almost always enough to orient yourself, at a fraction of the tokens a raw `Read` would cost.

## Workflow

1. Start with `cortex_overview` for unfamiliar repos or architecture questions.
2. For a concrete task, call `cortex_query` with the task and a budget.
3. For named code, use the search -> read -> impact loop:
   - `cortex_search_symbols` to locate candidate functions/classes/methods.
   - `cortex_read_symbol` with the chosen `node_id` to read only the exact numbered source span. Pass `mode: "skeleton"` for a signature-plus-nested-members view or `mode: "signature"` for just the signature line when you don't need the full body yet.
   - `cortex_impact` on the containing file to inspect structural and co-change neighbors before editing.
4. Need to look at a whole file instead of one symbol? Use `cortex_read_file` in place of the built-in `Read` tool — it defaults to `mode: "skeleton"` (imports/includes + top-level signatures, bodies elided); pass `mode: "full"` when you actually need every line.
5. Use `cortex_relations` for parsed graph questions such as imports, contains, inherits, emits, connects, or handles.
6. Use `cortex_references` when configs, docs, scripts, CMake/QRC, or other parser-missed surfaces may reference a symbol.
7. Use `cortex_search_text` for body text — string literals, error messages, comments, Markdown prose — that `cortex_search_symbols` can't find because it only matches symbol names/signatures/paths, not file contents.
8. Default to `response_format: "concise"`; pass `response_format: "detailed"` only when you need provenance, fingerprints, full metadata, or detailed why-edges.
9. If a tool reports `stale: true`, call `cortex_refresh` or rerun the read tool after refresh.
10. Watch for a `_meta` object on any response (`index_age_seconds`, `indexed_at`, `fingerprint_fresh`, and optionally `auto_refreshed`/`cached`/`saved_tokens`). `detailed` responses always carry it; a `concise` response only carries it when something is worth acting on, so its mere presence is itself a signal — check `fingerprint_fresh`/`auto_refreshed` before trusting a concise result on a repo you suspect just changed.

## Tools

### `cortex_query`

Returns a ranked, token-budgeted bundle for a task. Use for "what files matter for this change?" questions before raw file reads. Concise mode keeps compact per-item rationale.

**Standing guidance:** when you already know the filename or extension/language involved, include it in the `task` string. Ranking gives a large bonus to task terms that hit a file stem or symbol name, a smaller bonus for a matching directory segment, and boosts/demotes files by language when the task names one (e.g. "qml", "python", ".cpp") — so naming the file explicitly resolves ties and language-alike distractors that keyword-only phrasing can't.

Example:

```json
{"task":"fix stale index detection in store.py auto refresh path","budget":4000}
```

### `cortex_search_symbols`

Returns symbol candidates without source bodies. Use when the user names an identifier or when `cortex_query` suggests a file and you need a precise function/class.

Example:

```json
{"query":"generate bundle"}
```

### `cortex_read_symbol`

Returns source for one symbol span, formatted as `line_number: source` by default. Use after `cortex_search_symbols`; if the result is ambiguous, call again with the returned `node_id`. `mode` controls how much of the symbol comes back:

- `"full"` (default) — exact stored source lines, numbered. Unchanged from before `mode` existed.
- `"skeleton"` — the symbol's own signature plus, for a class/component, its nested members' signatures with bodies elided. Cheaper than `"full"` when you need to see a class's shape before deciding which method to read in full.
- `"signature"` — just the signature line and span metadata (`span_start`/`span_end`), no body at all. Cheapest option when you only need to confirm a symbol exists or check its location.

Example:

```json
{"symbol":"symbol:src/cortex/bundle.py:generate_bundle","budget":2000,"mode":"skeleton"}
```

### `cortex_read_file`

Direct replacement for the built-in `Read` tool on an indexed source file. `mode: "skeleton"` (default) returns import/include lines plus every top-level symbol's signature with bodies elided — enough to orient on a file's shape without paying for every line. `mode: "full"` returns the exact indexed content, equivalent to a raw `Read`. Falls back to full content automatically when the file has no indexed symbols (e.g. prose/Markdown). Respects `budget` like the other read tools.

Example:

```json
{"path":"src/cortex/bundle.py","budget":4000}
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

### `cortex_search_text`

Returns line-anchored body-text matches (FTS5 BM25) across indexed file contents — string literals, error messages, comments, Markdown prose. Use as a grep replacement when the target text isn't a symbol name/signature/path (that's `cortex_search_symbols`). Falls back to `fts_available: false` with empty results if this Python's `sqlite3` build lacks FTS5.

Example:

```json
{"query":"device offline retry","limit":10}
```

### `cortex_overview`

Returns repository graph summary, communities, god nodes, and surprising links. Use for orientation rather than precise code reading.

Example:

```json
{"repo_path":"."}
```

### `cortex_refresh`

Re-ingests the repo into the local SQLite index. Use when no database exists, after large file changes, or when stale results are reported.
