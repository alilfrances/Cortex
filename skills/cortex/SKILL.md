---
name: cortex
description: Use Cortex MCP tools for repo-first context, batched triage, graph-aware retrieval, impact analysis, symbol search, and refreshing local Cortex state before broad raw-file exploration.
---

# Cortex

Use Cortex when working inside an indexed repository and you need code context, symbol lookup, blast-radius checks, or repository orientation. Prefer Cortex before broad grep/Read exploration because it combines source content, symbol spans, graph edges, co-change history, and token-budgeted packing.

**`cortex_read_file` is the direct replacement for the built-in `Read` tool on any indexed source file.** Reach for it instead of `Read` whenever the path is inside an indexed repo; it defaults to a skeleton rendering (imports/includes + every top-level signature, bodies elided) that is almost always enough to orient yourself, at a fraction of the tokens a raw `Read` would cost.

## Built-in tool hook

The plugin's `PreToolUse` hook watches built-in `Read`, `Grep`, and `Glob` calls. It consults only the read-only SQLite index and silently passes anything it cannot answer, including unindexed targets, regex-shaped searches, plain directory/extension globs, small/windowed reads, unavailable databases, and non-git directories. Stale metadata can still produce advisory context in the default `advise` mode; only `enforce` downgrades stale redirects to advice. On an indexed positive it gives a nonblocking exact replacement in `additionalContext`; it does not replace Qt-aware symbol resolution, so `deviceConnected`, `onFoo`, C++ signals/slots, and QML handlers use the same `cortex_search_symbols`/`cortex_references` path as other identifiers.

`CORTEX_HOOK_MODE=advise` is the default. Set `CORTEX_HOOK_MODE=off` to disable the hook. `CORTEX_HOOK_MODE=enforce` is experimental and opt-in: it denies only fresh unscoped indexed redirects and automatically downgrades to advice when the `repos.updated_at` age exceeds `CORTEX_HOOK_STALE_AFTER_SECONDS` (default 86400). Path-scoped Grep/Glob and other option-rich searches are never enforced because their filters cannot be represented exactly by the MCP replacement. `CORTEX_HOOK_READ_THRESHOLD_BYTES` (default 512) controls when a whole-file `Read` can be replaced by a skeleton. Indexed decisions are recorded as metadata-only JSONL under the central Cortex data directory for future adoption analysis; logging never blocks a raw tool call.

## Workflow

1. Start with `cortex_overview` for unfamiliar repos or architecture questions; inspect its `top_hotspots` list before choosing risky files.
2. Before editing several known files or symbols, call `cortex_context` once with all targets. It returns one ordered triage card per target under a shared budget, covering paths, symbols, neighbors, co-change, hotspots, and Qt/QML wiring in one round trip.
3. For a concrete task, call `cortex_query` with the task and a budget. Leave ranking unchanged by default; pass `hotspot_boost: true` only when churn×complexity should be an explicit tie-breaker.
4. For named code, use the search -> read -> impact loop:
   - `cortex_search_symbols` to locate candidate functions/classes/methods.
   - `cortex_read_symbol` with the chosen `node_id` to read only the exact numbered source span. Pass `mode: "skeleton"` for a signature-plus-nested-members view or `mode: "signature"` for just the signature line when you don't need the full body yet.
   - `cortex_impact` on the containing file to inspect structural and co-change neighbors before editing.
5. Need to look at a whole file instead of one symbol? Use `cortex_read_file` in place of the built-in `Read` tool — it defaults to `mode: "skeleton"` (imports/includes + top-level signatures, bodies elided); pass `mode: "full"` when you actually need every line.
6. Use `cortex_relations` for parsed graph questions such as imports, contains, inherits, emits, connects, handles, or QML `instantiates` wiring.
7. Use `cortex_references` when configs, docs, scripts, CMake/QRC, or other parser-missed surfaces may reference a symbol.
8. Use `cortex_search_text` for body text — string literals, error messages, comments, Markdown prose — that `cortex_search_symbols` can't find because it only matches symbol names/signatures/paths, not file contents.
9. Default to `response_format: "concise"`; pass `response_format: "detailed"` only when you need provenance, fingerprints, full metadata, or detailed why-edges.
10. If a tool reports `stale: true`, call `cortex_refresh` or rerun the read tool after refresh.
11. Watch for a `_meta` object on any response (`index_age_seconds`, `indexed_at`, `fingerprint_fresh`, and optionally `auto_refreshed`/`cached`/`saved_tokens`). `detailed` responses always carry it; a `concise` response only carries it when something is worth acting on, so its mere presence is itself a signal — check `fingerprint_fresh`/`auto_refreshed` before trusting a concise result on a repo you suspect just changed.

## Tools

### `cortex_context`

Batches repository-relative file paths, exact file node ids, and symbol names/node ids into one deterministic response. The default `budget` is 2000 and is split across targets while retaining one card per input in the original order. Each compact card includes its resolved `node_id`/`kind`/`path`, signature or Markdown headings, span, structural neighbors, co-change partners with weights, hotspot data, and a per-card `truncated` flag. Ambiguous symbols return non-error `matches` cards using the same shape as `cortex_read_symbol`; missing targets get a missing card without aborting the rest. Optional `include` values add broader `impact`, `cochange`, or `symbols` detail without changing the compact defaults. Qt/QML cards expose QObject signals/slots/emits/connects and QML handlers plus local QML/C++ instantiation relations. If a deliberately tiny budget cannot fit irreducible card metadata, the response sets `budget_feasible: false` and preserves each original target string.

Use this before editing a group of files:

```json
{"targets":["src/cortex/mcp/tools.py","symbol:src/cortex/bundle.py:generate_bundle","README.md"],"budget":2000,"include":["impact"]}
```

### `cortex_query`

Returns a ranked, token-budgeted bundle for a task. Use for "what files matter for this change?" questions before raw file reads. Concise mode keeps compact per-item rationale. `hotspot_boost: true` is opt-in and promotes files that combine frequent git touches with high per-language branch/binding complexity; omit it to preserve the default ranking.

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

Returns parsed graph edges filtered by relation and symbol. Use for structural questions where parser coverage is enough, including Qt `emits`/`connects`/`handles` and QML `instantiates` edges.

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

Returns repository graph summary, communities, god nodes, top churn×complexity hotspots, and surprising links. Use for orientation rather than precise code reading. In `response_format: "detailed"`, the `semantic` field reports optional Model2Vec installation, enabled/active state, local model readiness, indexed chunks, and a no-network reason.

Example:

```json
{"repo_path":"."}
```

### `cortex_refresh`

Re-ingests the repo into the local SQLite index. Use when no database exists, after large file changes, or when stale results are reported.

### Optional semantic retrieval

Install `[semantic]` only when desired. Run `cortex semantic setup` explicitly to cache the verified `minishlab/potion-code-16M` model below `CORTEX_DATA_DIR`, then run a full ingest to create symbol chunks. `cortex semantic status` is local-only. Ingest/query never download or contact a provider; set `CORTEX_SEMANTIC=0` to force the normal lexical/graph result even after setup.
