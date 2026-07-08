# Changelog

## 0.7.3 — 2026-07-08

- Boost whole-identifier matches in `cortex_search_symbols` over sub-token floods. A camelCase/snake query splits into tokens matched independently, so a query containing one common token ("flow") could flood the results — and, via the candidate `LIMIT`, drop the real target entirely — with that token's matches. Candidate fetch now orders by a whole-identifier `LIKE` pattern (tokens in sequence, so `DeviceListModel` / `device_list_model` / `device list model` all match either spelling) and then by how many query tokens hit the label; ranking adds a whole-query-embedded tier above scattered-token matches and grades partial name matches by token coverage.
- Weight directory-path tokens in `cortex_query` ranking (`PATH_MATCH_BONUS = 40`): a task term matching a path segment ("ui", "backend", "mcp") now breaks ties between same-named files, weaker than the existing stem/symbol `NAME_MATCH_BONUS` but stronger than body keyword density.
- Return multiple ranked snippets from `cortex_query` instead of one budget-filling file dump: when more than one candidate matches, each item is capped at `ITEM_BUDGET_SHARE = 0.4` of the budget (oversized items degrade to skeleton/truncated form via the existing packing fallbacks), so the top file can no longer crowd out every other match.

## 0.7.2 — 2026-07-08

- Fix `cortex_search_symbols` (P0) burying the queried symbol under alphabetical, unrelated results. `CortexStore.search_nodes` matched query tokens against each node's `source_ref` (file path) with the same weight as the symbol name, so a query like `CortexStore` matched the *path* of every symbol under `src/cortex/` and ranked `__init__`, `_bfs_proximity`, … equal to the real class (then sorted alphabetically). With no `ORDER BY`, the genuine node could also be dropped by the candidate `LIMIT` before ranking ran. Now the SQL fetches label matches first (`ORDER BY` a label/signature/path priority) so a name match is never truncated away, and the Python ranker buckets strictly name → signature → path, so path-only hits always rank last. (Not related to `enrichment_enabled`, which does not gate symbol matching.)
- Fix `cortex_read_symbol` (P1) returning a single declaration line for C/C++/QML/JS/… symbols. The regex structural backend hard-coded `span_end = span_start`; it now brace-matches the body (skipping strings and comments) to the closing brace, and anchors the line number and signature on the symbol *name* so leading `^\s*` / greedy return-type matches no longer skew the span onto a blank or preceding line. Also fixes `_signature` overshooting toward EOF for a symbol on the final line with no trailing newline.
- Fix `cortex_impact` (P2) listing commit SHAs as impacted paths ranked `1.0`. Commit nodes default to `granularity="file"` and link to files via COCHANGE `touches` edges, so `rank_file_impact` counted them as file neighbors; it now excludes commit nodes with the same guard the community/rank layers use.
- Improve `cortex_query` relevance (P2) when the task names a language or extension: same-language files are boosted and other code languages demoted (multiplicatively, so unrelated files are never seeded by language alone), so e.g. a QML task no longer resolves to the same-named C++ file.

## 0.7.1 — 2026-07-08

- Demote test/eval/fixture/example/benchmark/sample paths in `cortex_query` ranking (`AUX_PATH_DEMOTION = 0.5`) unless the task itself mentions test/eval intent terms. Fixes keyword-dense auxiliary files (e.g. `evals/run_evals.py` fixture strings) outranking real implementation files and exhausting the token budget; demotion applies before pagerank/BFS seeding so graph ranking inherits it.

## 0.7.0 — 2026-07-07

- Improve bundle ranking for agent tasks: stopwords no longer dominate query terms, camelCase/snake_case identifiers split into searchable subtokens, and source-file term rarity down-weights repo-wide common words so target symbols like `_ensure_fresh` rank above keyword-noisy distractors.
- Add `response_format: "concise" | "detailed"` to Cortex read tools. Concise is now the default, with compact status, rounded scores, shorter query rationales, and slim symbol-search results; detailed preserves provenance, fingerprints, and existing metadata.
- Add `cortex_read_symbol`, which resolves a symbol name or `node_id` and returns exact numbered source lines for its stored span, with non-error disambiguation when multiple symbols match.
- Generalize tight-budget skeleton packing beyond Python so any code file with symbol spans can emit import/include lines, signatures, and language-neutral body elision.
- Improve `search_nodes` for multi-token and normalized identifier queries so `generate bundle`, `generate_bundle`, and `generateBundle` converge on the same symbol candidates.
- Rewrite Cortex tool descriptions and skill guidance around agent tool-use patterns, including the recommended search -> read_symbol -> impact navigation loop.
- Extend evals with a distractor-rich stale-index auto-refresh task and report Precision@3 alongside existing precision/recall metrics.

## 0.6.2 — 2026-07-07

- Fix `cortex_impact` returning 0 neighbors for C/C++/QML files (and Python files using absolute imports): STRUCTURAL import edges always pointed at synthetic `module:{name}` nodes even when the include/import target matched a real file in the repo, so `rank_file_impact` (which only counts file-to-file edges) had nothing to walk beyond sparse COCHANGE history. Added `resolve_local_import()` (exact-path match, then unique-basename fallback) to `regex_backend.py`, wired into `treesitter_backend.py` and `ast_extract.py`, so `#include "airpod.hpp"` / `import pkg.mod` now resolve to `file:...` edges when the target exists among ingested sources.

## 0.6.1 — 2026-07-07

- Fix `cortex_search_symbols` dumping 150k+ char results: `GraphNode.to_dict()` now truncates `signature` to 200 chars, and `CortexStore.search_nodes` ranks exact/prefix label matches before substring matches (was alphabetical, so close matches could be pushed out by `limit`).
- Fix `cortex_relations` silently returning all edges unfiltered when called with `target` instead of `symbol`: `target` is now accepted as an alias for `symbol`.
- Fix `cortex_impact` returning an indistinguishable empty result for a path with no co-change/structural edges vs. a path that doesn't match any node in the graph (e.g. absolute path, wrong casing): now raises/returns an explicit `unknown_path` error with a hint instead of a bare `[]`.

## 0.6.0 — 2026-07-07

- Add `cortex_references` MCP tool: blast-radius query for a symbol, unioning parsed graph edges with a repo-wide grep (honoring ingest skip-dirs), bucketed by `code`/`script`/`doc`/`config`/`other`, deduped against graph-covered locations. Closes the gap where `cortex_relations` only sees parser-indexed languages and misses cross-language wiring (CMakeLists.txt, shell scripts, `.qrc`, JSON/YAML configs, docs).
- Fix stale tool-discovery surfaces: `hooks/session-start.py`'s SessionStart context and `skills/cortex/SKILL.md` hadn't been updated since `cortex_relations` shipped (0.3.0) — agents had no signal these tools existed and fell back to raw grep. Both now enumerate all six query tools with per-tool trigger guidance.

## 0.5.1 — 2026-07-07

- Slim `cortex_relations` and `cortex_impact` output: edge endpoints collapse to a single `"label @ path:line"` string instead of a 3-field object; internal-only `edge_id`/`layer` fields dropped from both tools' responses (`cortex_relations` also drops `weight`/`confidence`, kept in `cortex_impact`'s `why` since it drives ranking there). Both tools now honor a token `budget` param (default 2000, same mechanism as `cortex_query`'s bundle), returning `truncated`/`returned_count` so a broad query can't dump hundreds of edges into a caller's context every turn.

## 0.5.0 — 2026-07-07

- Add `cortex_relations` MCP tool: query graph edges filtered by relation type (`contains`, `imports`, `inherits`, `emits`, `connects`, `handles`) at symbol granularity, e.g. "who inherits class X", "who emits signal Y". Previously the C++/QML/Qt structural edges added in 0.3.0–0.4.1 had no query path. Backed by new SQL-filtered `CortexStore.query_edges()`/`get_nodes()` (no full-graph load); output resolves edge endpoints to labels/paths without dumping raw edge metadata.

## 0.4.1 — 2026-07-07

- Fix: C++/QML structural extraction never produced `inherits` edges. Regex backend's base-clause capture was discarded; tree-sitter backend had no inheritance extraction at all. Both backends now emit `inherits` edges for `class Foo : public Bar`-style declarations.
- Fix: Qt signal/slot/emit/connect/Q_OBJECT detection previously existed only in the regex fallback, so it silently vanished for any file the tree-sitter backend successfully parsed. Tree-sitter path now runs the same Qt/QML detection regardless of which backend handles the file.

## 0.4.0 — 2026-07-07

- Per-repo data (`cortex.db`, `cortex_report.md`) now lives in a central store at `~/.cortex/data/<sha256-prefix-of-repo-path>/` instead of a `.cortex/` directory inside the target repo. Indexing no longer touches the target repo.
- Existing in-repo `.cortex/` directories keep working (legacy fallback); delete one to migrate that repo to the central store on next refresh.
- New: `CORTEX_DATA_DIR` env var overrides the central store location.
- New: `cortex gc [--prune]` lists or deletes central data dirs whose source repo is gone.
- Each central data dir carries a `meta.json` recording the source repo path.

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
