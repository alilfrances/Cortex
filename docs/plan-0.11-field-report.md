# Plan: Cortex 0.11 — field-report fixes and speed

Status: proposed (not started)
Input: the "Cortex 0.10.0 field test report" (3.7k-file mixed-language repo, 160k nodes, 600k edges)
Base: `main` at 0.10.1 (`719f299`)
Target release: **0.11.0** (Phase 6 changes tool-argument behaviour, so this is not a patch release)

This plan is written to be executed task by task by an implementer that has
not seen the investigation. Every task names the files, the change, the tests
and the acceptance check. Do the tasks **in order**; each one is one commit
and leaves `main`-quality CI green.

---

## 0. Rules for the implementer (read first)

1. **Setup** (once per session):
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ".[llm,watch,dev,semantic,tokens]" tree-sitter-cpp==0.23.4 build twine
   python -m cortex runtime setup          # must report "ready": true
   ```
2. **Checks before every commit** (the same ones CI runs, `.github/workflows/ci.yml`):
   ```bash
   python -m pytest tests/ -q                                   # full suite
   git diff --exit-code tests/goldens                           # goldens untouched unless the task says otherwise
   CORTEX_FORCE_REGEX=1 CORTEX_RUNTIME_NETWORK=0 python -m pytest -q tests/test_structural_dispatch.py tests/test_golden_extraction.py tests/test_golden_mcp_shapes.py
   ```
   Before the PR also run: coverage floors (`python -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json && python scripts/check_coverage_floors.py`), `python evals/run_evals.py`, `python -m build`.
3. **Goldens.** Most tasks below are *behaviour-identical* speedups: they must
   not change any file under `tests/goldens/`. If one does, stop and find out
   why; do not regenerate. Tasks that are *meant* to change output say so,
   name the goldens expected to change, and require regenerating with
   `CORTEX_UPDATE_GOLDENS=1` plus a `CHANGELOG.md` line naming each golden and
   the reason (see `CONTRIBUTING.md`, "Golden snapshots").
4. **Evals.** `evals/RESULTS.md` is rewritten by `run_evals.py` and is
   environment-sensitive. Never commit it unless a task asks. To compare
   ranking, run evals on `main` and on your branch in the *same* environment
   and diff the rows ignoring the latency column.
5. **`GRAPH_FORMAT_VERSION`** (`src/cortex/store.py`) must be bumped in the
   same commit as any change to extraction output (Task 5.1 does this).
6. **Measure speed with the harness**, before and after each Phase 1–2 task:
   ```bash
   python scripts/perf/synthetic_repo.py /tmp/perf-repo --modules 800 --commits 60
   CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --ingest
   CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --profile cortex_risk
   ```
   Use a new `CORTEX_DATA_DIR` for a cold run. `--modules 2400` gives a 3x
   repo for checking that a cost scales linearly.
7. Keep changes minimal and match surrounding style. Do not refactor beyond
   the task. Add a `CHANGELOG.md` bullet under a new `## Unreleased` heading
   for each task that changes behaviour or speed.

---

## 1. Verdict on the report

Each claim was checked against the code and, where possible, reproduced on a
synthetic repository (`scripts/perf/synthetic_repo.py`, 2.2k files, 17k nodes,
80k edges) and a 3x version (6.4k files, 50k nodes, 237k edges).

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | Cold `cortex_overview` ~2x slower than 0.9.2 | **Valid** (cause differs from the guess) | Community detection over `calls` edges costs 6–7x more than without them (1.40s vs 0.19s; 5.08s vs 0.80s at 3x). Each overview also loads the full graph twice (`_overview_detailed_capabilities` does a second `fetch_graph` only to count parser backends) and the token-savings ledger re-runs `_call_overview` in detailed format on every call, cache hits included. Costs scale linearly, not superlinearly. |
| 2 | Time budget not enforced consistently | **Valid** | `check_deadline()` is called only in `community.py`, `store.ingest_lock` and the dead-code soft stop. `generate_bundle` (query), `analyze_risk`, `rank_file_impact`, ingest/refresh and `_ensure_fresh` auto-refresh never check it. Refresh reports no progress. |
| 3 | `cortex_query` ranking weak for task prompts | **Valid**, with a sharper root cause | Qt Linguist translation catalogues use the `.ts` extension and are classified as TypeScript **code** (`ingest._classify_path`), parsed with the TypeScript grammar and scored as source. Empty files (`token_count: 0`) with a name/path hit are selected (`bundle.py` packing loop accepts `0 <= allowed`). Call edges reach file scores only indirectly (seeds are files; `calls` link symbols), so 0.10.0 did not change ranking. |
| 4 | `cortex_dead_code` ignores `path` | **Valid** | The tool schema has no `path`; no tool rejects unknown arguments, so it is silently dropped and the whole repo is scanned. |
| 5 | Dead-code false positives on framework entry points | **Valid, and worse than reported** | No override awareness in `deadcode.py`. Reproduced on a 10-line QML file: QML *bindings*, *externals*, *imports* (`QtQuick.Controls`), *parameters* and *ids* are reported, several at **high** confidence, some twice. They are not definitions and must never be candidates. |
| 6 | Inconsistent argument names | **Valid** | `cortex_relations` accepts `symbol`/`target`; `cortex_references`/`cortex_read_symbol` take `symbol`; `cortex_path` takes `symbol_a`/`symbol_b` only. |
| 7 | Unknown name returns noise | **Valid** | `store.search_nodes` ORs the identifier sub-tokens; `DefinitelyNotHelperXyz` returns 20 `helperN` candidates as `ambiguous`. |
| 8 | `cortex_path` ambiguity needs node ids | **Valid** | Neither `Mod17::m3` nor `src/mod17.cpp:m3` resolves (20 ambiguous `m3`); only the exact `symbol:src/mod17.cpp:m3` works. |
| 9 | Identical calls not cached | **Partly valid** | `query`, `impact` and the overview *analysis* are already cached per fingerprint. `dead_code`, `risk`, `search_symbols`, `relations`, `path`, `references` are not. Calls are processed serially, so coalescing is unnecessary once results are cached. |
| 10 | `risk` run-all hides partial list | **Mostly not a bug** | `impacted_tests.select_tests` already returns the partial `tests_to_run` next to `tests_run_all`; the report's list was empty because no test reached the header. A per-changed-file coverage breakdown would still make the reason clearer. |
| — | Open question: were removed dead-code candidates correct? | Not verified | Task 4.5 adds an audit script. |

### Additional problems found during the investigation

| Problem | Impact | Task |
|---|---|---|
| `regex_backend.resolve_local_import` scans every known path per unresolved `#include` (basename fallback) — O(includes × files) | Full ingest **superlinear**: 3x files took 8x as long (40s → 331s). With an index: 40.9s → **8.9s**, identical graph. This, not the extra extraction, is most of the report's 274s refresh. | 1.1 |
| `risk._qt_instantiation_directives` builds a header/impl pair map with `_pair_for` for *every* path, each scanning every path — O(files²) with `pathlib` | 43s of a 51s `cortex_risk` (profiled) | 1.2 |
| Incremental ingest loads the full graph a second time only to count parser backends (`_parser_summary(store.fetch_graph(...))`) | Every auto-refresh after an edit pays an extra full graph load | 1.3 |
| `cortex_query` tokenizes every source twice, splits identifiers with uncompiled regexes (2.7M `re._compile` lookups), recomputes each node's outgoing weight on every PageRank iteration, and re-reads all sources plus the full graph after the bundle is built | ~10s query on the 2.2k-file repo | 1.4 |

### Baseline (2.2k-file synthetic repo, Linux x86_64, Python 3.13, cold, budget disabled)

| Step | Time |
|---|---|
| full ingest | 40.3s |
| `fetch_graph` | 1.9s |
| `detect_communities` | 1.6s |
| `analyze_dead_code` | 7.3s |
| `cortex_overview` cold / cached | 11.9s / 1.7s |
| `cortex_dead_code` | 7.8s |
| `cortex_query` | 9.8s |
| `cortex_risk` (`HEAD~3..HEAD`) | 15.2s |
| `cortex_impact` | 1.3s |
| `cortex_path` | 2.8s |

---

## Phase 1 — Speed, behaviour-identical (no golden changes)

### Task 1.1 — Index basenames for include/import resolution (ingest is quadratic)

- **Files:** `src/cortex/structural/regex_backend.py` (`resolve_local_import`, `resolve_qml_cpp_type`), `src/cortex/graph.py` (`build_file_layer`), `src/cortex/ingest.py` (incremental path that calls `build_file_layer`).
- **Change:**
  1. Add a small class in `regex_backend.py`:
     ```python
     class KnownPaths(frozenset):
         """A frozenset of repo-relative paths with lazily built lookup indexes."""
         # lazily computed dicts: basename -> sorted list of paths;
         # normalised C/C++ stem -> sorted list of paths (for resolve_qml_cpp_type)
     ```
     It must behave as a set for every existing caller (`in`, iteration, `len`).
     Build each index on first use and store it on the instance (use
     `__slots__`-free attributes or a `functools.cached_property`).
  2. `resolve_local_import(target, known_paths)`: keep the exact-path check;
     for the basename fallback use `known_paths.by_basename` when
     `isinstance(known_paths, KnownPaths)`, else build a throwaway dict (old
     behaviour, same result). Keep "exactly one match → return it, else None".
  3. `resolve_qml_cpp_type`: same, using a normalised-stem index for the
     snake_case fallback; keep the suffix loop order and "exactly one
     candidate" rule.
  4. In `build_file_layer` (and anywhere else `known_paths` is created for a
     batch, e.g. `ingest._ingest_locked` passes `scan.current_paths`), wrap
     once: `known = KnownPaths(paths)` and pass `known` down.
- **Tests:** `tests/test_resolve_local_import_index.py`: (a) for a set of
  paths with unique, duplicate and missing basenames, `resolve_local_import`
  returns the same value for a plain `set` and for `KnownPaths`; (b) same for
  `resolve_qml_cpp_type` including a snake_case header; (c) a perf guard:
  10,000 lookups against 20,000 paths finish in under 1s with `KnownPaths`.
- **Acceptance:** goldens unchanged; harness full ingest drops from ~40s to
  ~9s on the 2.2k-file repo and the 3x repo is no longer >3x slower than 1x;
  node and edge counts identical to before.

### Task 1.2 — Linear header/implementation pairing in `cortex_risk`

- **Files:** `src/cortex/risk.py` (`_pair_for`, `_pair_directives`, `_qt_instantiation_directives`).
- **Change:** build one index per `analyze_risk` call: `stem -> {suffix_lower:
  [paths]}` over `all_paths` (use `posixpath`/string ops, not `Path`, in the
  loop). Rewrite `_pair_for(path, index)` to look up candidates by stem and
  filter by the allowed suffix set, returning the same **sorted** list as
  today. `_qt_instantiation_directives` builds `pair_map` from the index in
  O(files). Keep the public behaviour and ordering exactly.
- **Tests:** unit test comparing old and new `_pair_for` results on a path set
  with `.h/.hpp/.cpp/.cc`, mixed case suffixes and duplicate stems in different
  directories (copy the old implementation into the test as the oracle).
- **Acceptance:** goldens unchanged; `bench_tools.py --profile cortex_risk`
  no longer shows `_pair_for`/`pathlib` at the top; `cortex_risk` on the 2.2k
  repo drops from ~15s to a few seconds.

### Task 1.3 — Count parser backends without loading the graph

- **Files:** `src/cortex/store.py`, `src/cortex/ingest.py` (`_parser_summary` call at the end of the incremental path), `src/cortex/mcp/tools.py` (`_overview_detailed_capabilities`).
- **Change:** add `CortexStore.parser_backend_counts(repo_path) -> dict[str, int]`
  using one SQL query over nodes with `kind = 'file'` (exactly what
  `ingest._parser_summary` counts today) with
  `json_extract(metadata_json, '$.parser_backend')` grouped and counted. Use
  it in both places instead of `fetch_graph`. In the incremental ingest path,
  `merged_nodes` is already in memory when the graph changed; prefer counting
  from it when available. Output dicts must be identical to today's.
- **Tests:** compare `parser_backend_counts` with the old in-Python count on
  an ingested fixture (`tests/fixtures/multilang_repo` or the Qt fixture).
- **Acceptance:** goldens unchanged; cached `cortex_overview` drops below
  ~0.5s on the harness repo.

### Task 1.4 — `cortex_query` hot paths

- **Files:** `src/cortex/bundle.py`, `src/cortex/rank.py`, `src/cortex/mcp/tools.py` (`_call_query`).
- **Changes (each must keep results bit-identical):**
  1. `_split_identifier`: precompile the two regexes at module level and wrap
     the function in `functools.lru_cache(maxsize=65536)` returning a tuple
     (callers only iterate). `store.py` has its own `_split_identifier`; do
     the same there.
  2. `_tokenize_text`: precompile `[A-Za-z0-9_]+`.
  3. `generate_bundle`: `_term_weights` tokenizes `f"{path}\n{content}"` for
     every source and the scoring loop tokenizes `f"{path}\n{haystack}"` again.
     Keep both computations (they differ for code: boilerplate is stripped)
     but compute each source's sets once per call and reuse them in the
     semantic-gating block (`lexical_signal_paths`) too.
  4. `rank.personalized_pagerank`: compute each node's `outgoing_weight` and
     the normalised `(target, weight / outgoing_weight)` list **once** before
     the iteration loop. Keep the iteration order and arithmetic identical
     (`damping * source_rank * normalised_weight` must equal the old
     `damping * source_rank * (weight / outgoing_weight)` — compute
     `weight / outgoing_weight` exactly as before, just once).
  5. `_call_query`: it re-reads every source and calls `fetch_graph` again only
     to build `why`. Have `generate_bundle` optionally return the `sources`
     and `edges` it already loaded (e.g. a private
     `_generate_bundle_with_context(...)` that `generate_bundle` wraps), and
     build `seed_paths` from the already-tokenized sets instead of lowercased
     substring search **only if** the result is identical; otherwise keep the
     substring test but reuse the loaded sources.
- **Tests:** existing query/bundle tests and `tests/goldens/evals/*` must pass
  unchanged; add a test that `personalized_pagerank` output is identical
  (exact float equality) to a copy of the old implementation on the
  `_build_qt_app` graph from `evals/run_evals.py`.
- **Acceptance:** goldens unchanged; `cortex_query` on the 2.2k repo drops
  from ~10s to under ~4s.

### Task 1.5 — Reuse overview analysis in `cortex_dead_code`; cache it

- **Files:** `src/cortex/mcp/tools.py` (`_call_dead_code`, `_call_overview`).
- **Change:** `cortex_overview` already caches `build_report_data` output
  (including full `dead_code` findings) under
  `_overview_analysis_cache_key(fingerprint)`. In `_call_dead_code`, when the
  call's `budget` gives the same `reference_budget` as overview
  (`max(2000, budget)` → i.e. `budget <= 2000`), read that cache and, on a
  hit with `dead_code_partial` false, use its `findings` instead of running
  `analyze_dead_code`. Otherwise run as today and cache the dead-code result
  under its own key `(fingerprint, "cortex_dead_code_analysis", {"budget":
  max(2000, budget), "v": 1})` when not partial. Apply
  `truncate_dead_code_result` after, exactly as today.
- **Tests:** a call sequence test: overview then dead_code performs one
  `analyze_dead_code` (monkeypatch a counter); two dead_code calls perform one.
- **Acceptance:** goldens unchanged (`cortex_dead_code` payload goldens exist
  under `tests/goldens/mcp/payloads/*/cortex_dead_code.*`).

### Task 1.6 — Cheaper token-savings baseline for `cortex_overview`

- **Files:** `src/cortex/mcp/tools.py` (`_detailed_rendering_tokens`, `_record_tool_usage`).
- **Change:** `_detailed_rendering_tokens` re-runs `_call_overview(detailed)`
  for every overview call. After Task 1.3 that re-run is cheap except for
  `_fit_overview_report`'s binary search, which re-renders and re-tokenizes
  large prefixes of up to 38k findings. Cap the binary search's upper bound:
  each finding renders to at least one line of > 4 tokens, so
  `hi = min(len(findings), budget // 4)` gives the same answer with far less
  work. Verify by asserting in a test that the result equals the uncapped
  search for budgets 0, 50, 2000, 20000 on a synthetic analysis with 10,000
  findings.
- **Acceptance:** goldens unchanged; the second `cortex_overview` call
  (cached analysis) is under ~0.5s on the harness repo.

## Phase 2 — The `cortex_overview` regression

### Task 2.1 — Leave `calls` edges out of community detection

- **Why:** 0.10.0 added ~165k `calls` edges at the 3x scale (69% of all
  edges). `detect_communities` runs label propagation over every edge, so it
  became 6–7x slower and its communities changed, which no 0.10.0 changelog
  entry intended (communities are a file-cluster view; calls are
  symbol-level).
- **Files:** `src/cortex/community.py`.
- **Change:** in the adjacency loop, `continue` when `edge.relation ==
  "calls"` (add a module constant `EXCLUDED_RELATIONS = frozenset({"calls"})`
  with a one-line comment explaining why). This restores the 0.9.2 edge set.
- **Tests:** a test that a graph's communities are identical with and without
  extra `calls` edges added.
- **Goldens:** check `git diff tests/goldens`. Anything containing community
  ids/labels may change back toward 0.9.2 values. If a golden changes, verify
  the new value equals the value at tag/commit `04f6dab` (0.9.2) where that
  golden existed, regenerate, and name it in the CHANGELOG.
- **Acceptance:** `detect_communities` on the 3x repo ≤ 1s (was 5.1s).

## Phase 3 — Make the time budget consistent and visible (claim 2)

Design decisions (implement exactly this):

- **Read tools** (`query`, `risk`, `impact`, `context`, `references`,
  `path`, `relations`) are **hard-bounded**: they call `check_deadline()` at
  stage boundaries and inside loops that can exceed ~0.5s. Exceeding the budget
  returns the existing `deadline_exceeded` error from `call_tool`.
- **Analysis tools** (`overview`, `dead_code`) keep the existing **soft** stop
  (`partial: true`) and must now be **counted**: add `soft_deadline_count` to
  the per-tool counters in `src/cortex/mcp/status.py` and increment it when a
  result comes back partial.
- **`cortex_refresh` is exempt** (a half-written index is worse than a slow
  one) but must report **progress**.
- **Auto-refresh inside a read tool** (`_ensure_fresh`): when the needed
  refresh is a *full* rebuild (no index, or `get_repo_graph_version <
  GRAPH_FORMAT_VERSION`) and a budget is active, do **not** run it inside the
  read tool. Return the current index with `stale: true` and
  `refresh_hint: "Index needs a full rebuild; call cortex_refresh."`.
  Incremental auto-refresh keeps running as today.

### Task 3.1 — Deadline checks in read tools

- **Files:** `src/cortex/bundle.py` (`generate_bundle`: after loading, every
  500 sources in each per-source loop, before PageRank), `src/cortex/rank.py`
  (`personalized_pagerank`: once per iteration), `src/cortex/risk.py`
  (`analyze_risk`: between stages), `src/cortex/impact.py`
  (`rank_file_impact`: per BFS level), `src/cortex/pathfind.py`
  (`shortest_paths`: per BFS level), `src/cortex/references.py` (`find_references`: per 200 files
  scanned).
- Use `from .deadline import check_deadline` (it is a no-op outside MCP).
- **Tests:** for each tool, monkeypatch `CORTEX_TOOL_TIMEOUT_SECONDS=0.001`
  on a fixture big enough to take > 1ms and assert the response is
  `{"error": "deadline_exceeded", "tool": ...}` and that
  `cortex_status` counters show `deadline_exceeded_count` incremented.

### Task 3.2 — Soft-stop counter

- **Files:** `src/cortex/mcp/status.py` (`_ToolAggregate.record`/`as_dict`, `StatusRegistry.record_completed`),
  `src/cortex/mcp/tools.py` (`call_tool`: after `_dispatch_tool`, detect a
  partial result — `dead_code_partial` or `partial` in the payload — and pass
  `soft_deadline=True` to the registry).
- **Golden:** `tests/goldens/mcp/shapes/cortex_status.*.json` gains the new
  counter key; regenerate and name it.

### Task 3.3 — Refresh progress

- **Files:** `src/cortex/ingest.py`, `src/cortex/graph.py`
  (`build_file_layer` file loop), `src/cortex/mcp/status.py`,
  `src/cortex/mcp/tools.py` (`_call_refresh`), `src/cortex/mcp/server.py`.
- **Change:**
  1. Add an optional `progress: Callable[[str, int, int], None] | None`
     parameter to `ingest_repository` / `_ingest_locked` / `build_graph` /
     `build_file_layer`. Report stages `"scan"`, `"parse"` (done/total files,
     at most every 50 files), `"resolve"`, `"cochange"`, `"write"`.
  2. `RequestStatus` (`src/cortex/mcp/status.py`) gets `stage`, `done`, `total` fields;
     `REGISTRY.snapshot()` includes them for in-flight calls. `_call_refresh`
     passes a callback that updates its own `RequestStatus`.
  3. If the request's params contain `_meta.progressToken`, `server.py`
     emits `notifications/progress` (`{"progressToken", "progress", "total",
     "message"}`) through the existing locked writer, at most once per second.
- **Tests:** a refresh on a small fixture records the stage sequence; a
  stdio test with a `progressToken` receives at least one
  `notifications/progress` before the result.

### Task 3.4 — No full rebuild inside a budgeted read tool

- **Files:** `src/cortex/mcp/tools.py` (`_ensure_fresh`).
- **Tests:** with a budget set and an index whose graph version is older,
  `cortex_search_symbols` returns quickly with `stale: true` and the hint, and
  does not call `ingest_repository` (monkeypatch a counter).

## Phase 4 — Dead-code correctness and speed (claims 4, 5)

### Task 4.1 — Only definitions are dead-code candidates

- **Files:** `src/cortex/deadcode.py` (`analyze_dead_code` candidate filter).
- **Change:** skip nodes whose `metadata["qml_kind"]` is in
  `{"binding", "external", "import", "parameter", "id"}`. Filter on
  `qml_kind` only (not on `kind`): other languages may use the same `kind`
  names for real definitions. Put the set in a named module constant with a
  comment. These are references/uses, not definitions.
- **Tests:** a QML fixture with the file used during the investigation
  (bindings `ScrollBar.horizontal.policy: ...`, `ScrollBar.vertical: ...`, a
  property, a signal, a function, a handler, an id, imports) reports none of
  the non-definitions and no duplicate entries.
- **Goldens:** `tests/goldens/mcp/payloads/*/cortex_dead_code.*` and overview
  goldens may lose entries; verify every removed entry is one of these kinds,
  regenerate, name them.

### Task 4.2 — Framework overrides are low confidence

*Implemented, with one deliberate narrowing:* a method of a class whose base is
outside the repository is **not** flagged unless it carries an override marker.
Nearly every class has some framework base, so flagging all of them would hide
real dead code (a plain `Activity` subclass method with no `@Override` stays
high confidence).

- **Files:** `src/cortex/deadcode.py` (`_OverrideIndex`, `_candidate_reason`).
- **Change:** before the reference tier, return `("low", "no incoming edges;
  overrides a base-class or framework method")` when the declaration carries
  `@Override` / `override` / `virtual`, when the enclosing class declares the
  method with `override`/`virtual` (an out-of-line C++ definition has no marker,
  its header declaration does), or when it shares a name with a method of a
  resolved base class (depth <= 6).
- **Tests:** `tests/test_deadcode_overrides.py` (Java `@Override`, Swift
  `override func`, C++ `override`/`virtual` in the header, resolved base).

### Task 4.3 — `path` argument for `cortex_dead_code`

- **Files:** `src/cortex/mcp/tools.py` (schema in `TOOL_DEFINITIONS` and
  `_call_dead_code`), `src/cortex/deadcode.py` (`analyze_dead_code` gains
  `path_prefix: str | None = None`).
- **Change:** normalise like `cortex_impact`'s `path` (repo-relative, no
  leading `./`, trailing `/` optional). Candidates are restricted to nodes
  whose `source_ref` equals the path or starts with `path + "/"`; reference
  search stays repo-wide (a use outside the directory still counts). An
  unknown path returns `{"error": "unknown_path", ...}` like `cortex_impact`.
  The Task 1.5 cache is keyed on the full findings; with a cache hit, filter
  the cached findings by prefix instead of re-running.
- **Golden:** `tests/goldens/mcp/tools_list.json` (new property); regenerate
  and name it.
- **Tests:** directory scoping, file scoping, unknown path, and that a
  reference outside the directory still credits a symbol inside it.

### Task 4.4 — Early-exit reference classification

- **Files:** `src/cortex/deadcode.py` (`_reference_tier`,
  `_AnalysisContext.references`), `src/cortex/references.py`.
- **Why:** dead code only needs one of three answers per label (no hits /
  comment-or-doc hits only / any code hit), but `find_references` builds the
  full hit list and binary-searches a token budget for every candidate.
- **Change:** add `references.classify_references(label, own_span, ...)`
  that scans the same graph hits then grep hits **in the same order** as
  `find_references` and stops at the first hit that is outside the symbol's
  own span and is not a comment/doc hit (→ `"low"`). It must reproduce the
  current decision exactly, **including** the current truncation: today only
  the first `best` hits that fit `reference_budget` tokens are considered.
  Implement the same prefix limit (count tokens incrementally as hits are
  appended, stop when the prefix would exceed the budget).
- **Tests:** for every candidate in the Qt and Python eval fixtures, the new
  classifier returns the same tier and reason as the old path (keep the old
  path in the test as the oracle).
- **Acceptance:** goldens unchanged; `analyze_dead_code` on the 2.2k repo at
  least 2x faster.

### Task 4.5 — Dead-code precision audit script (open question)

- **Files:** new `scripts/perf/audit_dead_code_credit.py`.
- **Change:** for a given indexed repo, list symbols that are *not* dead-code
  candidates only because of a resolved `calls` edge (incoming edges are all
  `calls`), sample N (default 50, seeded), and print each with its caller's
  file:line and the source line, so a person can check whether the call edge
  is real. No product code changes.

## Phase 5 — Query ranking (claim 3)

### Task 5.1 — Qt Linguist `.ts` files are translation data, not TypeScript

- **Files:** `src/cortex/ingest.py` (`_classify_path` and its callers),
  `src/cortex/structural/__init__.py` (dispatch), `src/cortex/store.py`
  (`GRAPH_FORMAT_VERSION` → 3).
- **Change:** classify a `.ts` file as `"text"` when its content, after
  leading whitespace/BOM, starts with `<?xml` or `<!DOCTYPE TS` or `<TS`.
  `_classify_path` only sees the path, so add a content-aware wrapper (e.g.
  `_classify_source(path, content)`) used where the content is already read,
  and make sure such files are not sent to the TypeScript structural
  extractor. Bump `GRAPH_FORMAT_VERSION` to 3 with a comment line.
- **Tests:** a Linguist `.ts` file is `text` with no symbol nodes; a real
  TypeScript `.ts` file is unchanged.
- **Goldens:** none expected (no fixture has a Linguist file); if the graph
  format version appears in a golden, regenerate and name it.

### Task 5.2 — Demote locale/translation data and never pack empty items

- **Files:** `src/cortex/bundle.py`.
- **Change:**
  1. Add `LOCALE_PATH_RE` matching `(^|/)(i18n|l10n|locale|locales|translations?)(/|$)`
     and translation suffixes (`.ts` classified as text, `.po`, `.pot`,
     `.xlf`, `.xliff`, `.qm`). Multiply such files' score by
     `AUX_PATH_DEMOTION` unless the task mentions translation/locale terms
     (`translation`, `translate`, `locale`, `i18n`, `l10n`, `language`) — same
     pattern as `AUX_INTENT_TERMS`.
  2. In the packing loop skip any candidate with `token_count <= 0`
     (currently `0 <= allowed` lets empty files in).
- **Tests:** a fixture with a large XML `.ts` catalogue that mentions every
  task word and a small C++ class that implements the behaviour: the class
  ranks above the catalogue; an empty `__init__.py` whose path matches is
  never in `items`.
- **Goldens/evals:** run evals on `main` and the branch in the same
  environment; rows must be unchanged or better. If
  `tests/goldens/evals/*.json` change, regenerate and name them.

### Task 5.3 — Expand along the call graph (eval-gated)

- **Files:** `src/cortex/bundle.py`.
- **Change:** after `source_scores` is computed and before PageRank:
  aggregate symbol-level `calls` and `inherits` edges to file pairs once
  (`source_ref` of each endpoint; ignore `name:`/`module:` targets and
  same-file pairs). For the top `K=8` files by `source_scores`, add a bonus
  `CALL_EXPANSION_BONUS * seed_score_normalised` to each neighbour file that
  has score 0 or lower than the bonus, for neighbours that are the *callee*,
  the *caller*, the *base class* or the *subclass* file. Start with
  `CALL_EXPANSION_BONUS = 20.0` (below `PATH_MATCH_BONUS`). Only files with
  a positive final score are candidates, so this lets a base class or caller
  enter the bundle.
- **Acceptance gate (do not merge otherwise):** on the same-environment eval
  comparison, the macro precision/recall over the 7 OSS fixtures and the
  17-task matrix must not drop; at least one new fixture test (the
  reporter's shape: keyword hit in a manager class; the behaviour lives in a
  base class, its subclass and a state manager connected by calls/inherits)
  must start passing. Tune the constant only against evals, never against a
  single test. If the gate cannot be met, leave this task undone and say so
  in the PR.

## Phase 6 — Tool-argument ergonomics (claims 6, 7, 8)

### Task 6.1 — Reject unknown arguments; accept documented aliases

- **Files:** `src/cortex/mcp/tools.py` (`call_tool` before `_dispatch_tool`).
- **Change:**
  1. Alias table applied first (rename, never overwrite an explicit
     canonical value):
     `cortex_path`: `source`/`from` → `symbol_a`, `target`/`to` → `symbol_b`;
     `cortex_references`, `cortex_read_symbol`: `target`/`name` → `symbol`;
     `cortex_relations`: `name` → `symbol`;
     `cortex_query`: `query`/`prompt` → `task`;
     `cortex_search_symbols`, `cortex_search_text`: `pattern`/`text` → `query`.
  2. Then any key not in the tool's `inputSchema.properties` (ignore keys
     starting with `_`) returns
     `{"error": "unknown_argument", "tool", "unknown": [...], "accepted": [...],
     "hint": "did you mean ...?"}` with `isError: true`. Suggest with
     `difflib.get_close_matches`.
- **Tests:** every alias maps; unknown keys error with the accepted list;
  `_meta` passes; existing tests keep passing (fix any test that passed a
  stray argument).
- **CHANGELOG:** call this out as a behaviour change.

### Task 6.2 — No weak fuzzy matches when resolving a symbol

- **Files:** `src/cortex/mcp/tools.py` (`_resolve_symbol`).
- **Change:** keep exact node id, then exact label. For the `search_nodes`
  fallback, only keep candidates whose label contains **all** query tokens (or
  whose normalised label equals the normalised query). If none remain, return
  `symbol_not_found` with up to 5 `did_you_mean` labels from the unfiltered
  matches. `cortex_search_symbols` itself is unchanged (it is a search).
- **Tests:** `DefinitelyNotHelperXyz` → `symbol_not_found` with
  `did_you_mean`; a partial but full-token match still returns `ambiguous`.

### Task 6.3 — Qualified names and definition preference

- **Files:** `src/cortex/mcp/tools.py` (`_resolve_symbol`).
- **Change:** before the search fallback, accept:
  1. `path:label` and `path::label` → node id `symbol:path:label` (and
     `path:Class.method` forms as stored);
  2. `Class::method` / `Class.method` → symbols labelled `method` whose
     enclosing class (via `contains`) is labelled `Class`;
  3. when exactly one candidate lives in an implementation file
     (`.c/.cc/.cpp/.cxx/.m/.mm`) and every other candidate is a declaration in
     a header of the same qualified name, return the implementation one.
- **Tests:** `Mod17::m3`, `src/mod17.cpp:m3` and `src/mod17.cpp::m3` resolve
  directly on the synthetic shape (use a small fixture, not the generator);
  a header declaration + cpp definition pair resolves to the definition.

## Phase 7 — Lower-priority items (claims 9, 10)

### Task 7.1 — Cache `cortex_risk`

- **Files:** `src/cortex/mcp/tools.py` (`_call_risk`).
- **Change:** cache key = fingerprint + arguments + SHA-256 of the diff text
  the risk analysis reads (working tree/staged/range output), so an
  uncommitted edit changes the key even if file stats do not. Same
  `_cache_get`/`_cache_set` pattern as `_call_impact`.
- **Tests:** two identical calls run `analyze_risk` once; editing a file
  between calls runs it twice.

### Task 7.2 — Show what the test selection did find

- **Files:** `src/cortex/impacted_tests.py`, `src/cortex/risk.py`.
- **Change:** add `tests_by_changed_file: {path: [test, ...]}` and
  `uncovered_changed_files: [path, ...]` to the risk output (concise and
  detailed), computed from the existing `selection`. Keep `tests_to_run`,
  `tests_run_all`, `tests_run_all_reason` unchanged.
- **Goldens:** risk payload/shape goldens gain keys; regenerate and name them.

## Phase 8 — Release

- Bump to **0.11.0** in every place the 0.10.1 bump touched: `pyproject.toml`,
  `src/cortex/__init__.py`, `.claude-plugin/plugin.json`,
  `.claude-plugin/marketplace.json` (both fields), `.codex-plugin/plugin.json`,
  `SERVER_INFO` in `src/cortex/mcp/server.py`, `README.md` line 7, and
  `tests/test_golden_mcp_shapes.py` + `tests/goldens/mcp/initialize.json`
  (`tests/test_plugin_manifests.py` checks them).
- Turn `## Unreleased` in `CHANGELOG.md` into `## 0.11.0 — <date>` with a
  before/after table from the harness (2.2k and 6.4k repos).
- Update `README.md` sections that describe changed behaviour: dead-code
  `path`, argument aliases/unknown-argument errors, refresh progress, the
  time-budget rules from Phase 3.

## Expected outcome

| Step (2.2k-file harness repo) | Before | Expected after |
|---|---|---|
| full ingest | 40s | ~9s (Task 1.1; was superlinear, now ~linear) |
| `cortex_overview` cold / cached | 11.9s / 1.7s | ~5s / <0.5s (1.3, 1.6, 2.1, 4.1, 4.4) |
| `cortex_dead_code` | 7.8s | ~3s, and 0s after an overview (1.5, 4.4) |
| `cortex_query` | 9.8s | <4s (1.4) |
| `cortex_risk` | 15.2s | a few seconds (1.2) |

On the reporter's repository the refresh and risk numbers should improve the
most, because both are quadratic in file count today.
