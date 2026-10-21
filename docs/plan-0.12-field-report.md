# Plan: Cortex 0.12 — 0.11.0 field-report fixes and speed

Status: proposed (not started)
Input: "Cortex 0.11.0 field report: root causes for open issues" (mixed C++/Qt/QML
repo, 3.2k files, 160k nodes, 620k edges, stdio MCP server, default 30s budget)
Base: `main` at 0.11.0 (`07fe2f4`)
Target release: **0.12.0** (graph format bump, new/changed response fields, changed test pairing)

This plan is written to be executed task by task by an implementer that has
not seen the investigation. Every task names the files, the change, the tests
and the acceptance check. Do the tasks **in order**; each one is one commit
and leaves CI green. Where a task needs a judgement call, the decision rule is
written into the task — follow it rather than improvising.

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
   python -m pytest tests/ -q
   git diff --exit-code tests/goldens      # unless the task says which goldens change
   CORTEX_FORCE_REGEX=1 CORTEX_RUNTIME_NETWORK=0 python -m pytest -q \
     tests/test_structural_dispatch.py tests/test_qt_relations.py tests/test_qt_emits.py \
     tests/test_context_mcp.py tests/test_qml_full_support.py tests/test_qml_modules.py \
     tests/test_qml_resolution.py tests/test_calls_cpp.py tests/test_calls_js.py \
     tests/test_calls_multilang.py tests/test_golden_extraction.py tests/test_golden_mcp_shapes.py
   ```
   Before the PR also run: coverage floors
   (`python -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json && python scripts/check_coverage_floors.py`),
   `python evals/run_evals.py`, `python -m build`.
3. **Goldens.** Tasks marked *behaviour-identical* must not change any file
   under `tests/goldens/`. If one does, stop and find out why; do not
   regenerate. Tasks that are *meant* to change output list the goldens
   expected to change. Regenerate with
   `CORTEX_UPDATE_GOLDENS=1 python -m pytest tests/test_golden_*.py tests/test_eval_baseline.py`,
   re-run without the variable, review the diff (it must contain only what the
   task intends), and add a `CHANGELOG.md` line naming each golden and why
   (`CONTRIBUTING.md`, "Golden snapshots").
4. **Evals.** Never commit `evals/RESULTS.md` unless a task asks. To compare
   ranking, use the comparison script in Task 0.2 (same environment, fixed git
   dates).
5. **`GRAPH_FORMAT_VERSION`** (`src/cortex/store.py`, currently `3`) must be
   bumped in the same commit as any change to extraction output (Tasks 2.1 and
   2.2 do this).
6. **Measure speed with the harness** before and after every task whose
   acceptance names a bench number (Tasks 1.1, 3.1, 5.1, 5.2, 6.2, 6.3):
   ```bash
   python scripts/perf/synthetic_repo.py /tmp/perf-repo --modules 800 --commits 60 --qt
   CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --ingest --no-cache
   CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --profile cortex_query --no-cache
   ```
   Always use a fresh `CORTEX_DATA_DIR` for a cold run. Paste the before/after
   rows into the commit message body.
7. Keep changes minimal and match the surrounding style (module-level
   single-slot memo globals, long explanatory comments only where the code is
   non-obvious). Do not refactor beyond the task. Add a `CHANGELOG.md` bullet
   under `## Unreleased` for every task that changes behaviour or speed.

---

## 1. Verdict on the report

Every claim was checked against the code at `07fe2f4` (the report's line
numbers match exactly), and where possible reproduced: on the synthetic repo
(`--modules 800`: 2.2k files, 16.7k nodes, 79k edges), on a hand-written Qt
header, and on the vendored real Qt project
`evals/fixtures/oss/cpp_qt_sortfilterproxymodel`.

| # | Claim | Verdict | Evidence / correction |
|---|---|---|---|
| 1 | Ledger re-runs tools outside the deadline | **Valid** | `call_tool` runs `_record_tool_usage` after both `REGISTRY.track` and `deadline_scope` (`mcp/tools.py:2817`); `_detailed_rendering_tokens` re-dispatches the whole tool. Profiled `cortex_path`: **2.85s of 6.27s (45%)** is the ledger's second run (2× `fetch_graph`, 2× `shortest_paths`, 2× `_ensure_fresh`). `cortex_status` excludes it because it runs after `track` exits — which explains the reporter's 12.9s vs ~25s. |
| 2 | Qt macros parsed as nested `class QObject` | **Valid symptom, wrong root cause** — the proposed fix alone would not remove the fake nodes | The fake `symbol:<file>:QObject` class is **fabricated by the regex Qt pass**, not by tree-sitter: `_extract_qml_cpp_registration` (`structural/regex_backend.py:1061-1115`) finds a property's owner class with `_QT_CLASS_RE`, which requires `{` **on the `class` line**. Qt style puts `{` on the next line, so no owner is found and the code falls back to the literal `"QObject"` (`owner = class_lines.get(lineno, "") or "QObject"`), creating `QObject`, `QObject.<prop>`, `QObject.<invokable>`. Reproduced: 17 of the vendored Qt fixture's headers get a fake `QObject` (64 fabricated nodes). The same brace rule also (a) drops the `qt: "qobject"` tag from every brace-on-next-line class (`_extract_qt_cpp_edges`), and (b) makes the regex backend emit **no** `inherits` edges for such classes (`_extract_cpp_inheritance_edges`). Separately, tree-sitter does mis-parse Qt macros: the sample header has 6 `ERROR` nodes, and with an export macro (`class MYLIB_EXPORT Player : public QObject`) tree-sitter parses the **whole class as a function named `QObject`** and `Player` disappears; the regex backend names that class `MYLIB_EXPORT`. Length-preserving masking of the macros (the report's suggestion, plus export macros and `signals:`/`slots:`) brings the ERROR count to 0 and restores both classes. **Both fixes are needed** (Tasks 2.1, 2.2). |
| 3 | `cortex_risk` test selection noise | **Valid** | Label-based `inherits` attachment (`impacted_tests.py:271-278`) attaches `name:QObject` to every repo class labelled `QObject` (the fake ones from #2). `_SUFFIX_PATTERNS` has no C/C++ entry, so `ui/main.cpp` pairs with every `tests/**/main.cpp` and `footest.cpp`/`tst_foo.cpp` (Qt's QTest convention, not mentioned in the report) never pair. Also found: the pairing loop is O(changed files × all paths) and re-sorts all paths for each changed file. The report's "reverse-degree hub cutoff in `_structural`" is **rejected**: a changed member of a real base class legitimately impacts every subclass's tests, and a cutoff can silently skip them without triggering run-all. The label-match cap covers the noisy case. |
| 4 | `truncate_risk_result` drops `tests_by_changed_file` first | **Valid** (nuance) | All of `files` is popped first, then `tests_by_changed_file` is the first of the secondary buckets dropped (`risk.py:893`). No `omitted_fields`. Also each pop re-serializes and re-tokenizes the whole payload (O(n²)). |
| 5 | Cold `cortex_query` returns nothing at the deadline; PageRank adds ~0 | **Valid, quantified** | Profile of a cold query (synthetic, 10.5s): `personalized_pagerank` **4.3s of the 7.5s bundle**, `fetch_graph` 1.9s. The PageRank bonus for non-seed files is at most **0.07** (×10 multiplier) against keyword scores of 10–100+: it only orders files that have no keyword hit. A file-level PageRank is **8.6× faster** (0.53s vs 4.56s) and on the 17-task eval matrix changes exactly one row, upward (`python_app` "markdown setup" `skeleton_off` precision 0.4 → 0.5); disabling PageRank entirely gives the same result. Also found: `_bundle_why` scans every edge for every selected item (O(items × edges): ~12M iterations at 620k edges); and spans are scored for every symbol of every file even when the file shares no term with the task. |
| 6 | One file takes the whole query budget | **Valid** (by code reading) | `ITEM_BUDGET_SHARE` caps each item, not each file; every name-matching span gets `NAME_MATCH_BONUS` (100) while call-expanded neighbours get at most `CALL_EXPANSION_BONUS` (20). Also found: a span nested inside an already selected span of the same file (method inside its class) is selected again, duplicating text. |
| 7 | `cortex_overview` permanently `dead_code_partial` | **Valid** | A partial analysis is never cached (`mcp/tools.py:1052`), so each call recomputes communities, re-writes them (`save_communities`) and restarts dead code from candidate 0. `ReferenceCorpus.__init__` (reads every git-listed file from disk) and `_symbol_files` (tokenizes all of them) have no stop check. |
| 8a | CMake targets always reported dead | **Valid** | `target:` nodes are `granularity="symbol"` with only outgoing `builds` edges (`regex_backend._target_node`). |
| 8b | No stop check after the loop | **Partly valid** | When the loop finishes, the result *is* complete, so not marking it `partial` is correct. The real problem is that a single candidate can overrun: the first `references()` call builds the whole corpus uninterruptibly. Fixed by making corpus construction stoppable (Task 6.1). |
| 8c | `path=` only filters candidates | **Valid, by design** | References are searched repo-wide on purpose (a use outside the path credits a symbol inside it). The cost is setup: `fetch_graph`, `fetch_sources`, reference indexes, corpus. The graph cache (Task 3.1) and a narrowed corpus token index (Task 6.1) address it. |
| 9a | `_call_search` has no result cache | **Valid** | Low value (SQL `LIKE`), but cheap to add (Task 6.1). |
| 9b | `cortex_status` has no `version` | **Valid** | `SERVER_INFO["version"]` is also a hard-coded `"0.11.0"` literal duplicating `cortex.__version__`. |

### Additional problems found during the investigation

| Problem | Impact | Task |
|---|---|---|
| `fetch_graph` (JSON-decodes every node and edge row) runs on almost every tool call: query, path, impact, risk, dead code, overview, context | 2.0s at 79k edges, roughly linear in edges, so expect >10s per call at 620k edges | 3.1 |
| `_OverrideIndex._declares_method` scans every method of every file on each call | 1.6s of an 8.2s `analyze_dead_code` | 6.3 |
| `_bundle_why` scans every edge once per selected query item | O(items × edges); several seconds at 620k edges | 5.2 |
| `ui/main.cpp`-style pairing also feeds `cortex_risk`'s `missing_tests` directives (`risk.py:531`) | False "missing test" advice | 4.1 |
| `_estimate_baseline` runs after `REGISTRY.track` exits | `cortex_status.slowest_seconds` under-reports | 1.1 |

### Baseline (synthetic `--modules 800`, Linux x86_64, Python 3.13, cold, budget disabled)

| Step | Time |
|---|---|
| full ingest | 10.5s |
| `fetch_graph` | 2.0s |
| `analyze_dead_code` (graph given) | 8.2s |
| `cortex_overview` cold / cached | 11.1s / 0.24s |
| `cortex_query` cold (unique task) | 10.5s (PageRank 4.3s) |
| `cortex_path` | 6.3s (ledger 2.85s) |
| `cortex_impact` | 3.8s |
| `cortex_risk` (`HEAD~3..HEAD`) | 3.1s |

---

## Phase 0 — Harness (no product change)

### Task 0.1 — Qt mode for the synthetic repo; cold-query switch for the bench

- **Files:** `scripts/perf/synthetic_repo.py`, `scripts/perf/bench_tools.py`.
- **Change:**
  1. `synthetic_repo.py`: add `--qt` (default off; output without it must be
     byte-identical to today, so existing numbers stay comparable). With `--qt`,
     for every 4th module `k` also write:
     - `src/qt/widget{k}.h`, **brace on the next line**, every 3rd one with an
       export macro (`class WIDGETS_EXPORT Widget{k} : public QObject`):
       ```cpp
       #pragma once
       #include <QObject>
       class Widget{k} : public QObject
       {
           Q_OBJECT
           Q_CLASSINFO("Author", "perf")
           Q_PROPERTY(int value READ value WRITE setValue NOTIFY valueChanged)
       public:
           explicit Widget{k}(QObject *parent = nullptr);
           int value() const;
           void setValue(int v);
           Q_INVOKABLE void refresh();
       signals:
           void valueChanged();
       private:
           int m_value = 0;
       };
       ```
     - `src/qt/widget{k}.cpp` defining those members; `refresh()` calls
       `Widget{d}::refresh` on one dependency `d` chosen with the existing
       seeded `rng`.
     - tests: `tests/qt/tst_widget{k}.cpp` for even `k/4`, `tests/qt/widget{k}test.cpp`
       for odd `k/4`, each `#include "widget{k}.h"` and calling `refresh()`.
     - once: `src/app/main.cpp`, `tests/unit/main.cpp`, `tests/integration/main.cpp`,
       and `CMakeLists.txt` with `add_library(widgets <all widget cpps>)` and
       `add_executable(app src/app/main.cpp)`.
  2. `bench_tools.py`: add `--no-cache` (sets `CORTEX_QUERY_CACHE=0` before
     importing cortex) and add rows `("cortex_dead_code", {"path": "src/mod17.cpp"})`
     and, when `src/qt/` exists, `("cortex_risk", {"range": "HEAD~1..HEAD"})`
     after appending a commit that edits `src/qt/widget4.h` line 9 (do the edit
     in the bench only when `--qt-edit` is passed, so default runs stay read-only).
- **Tests:** none (scripts). Run both scripts once.
- **Acceptance:** `--qt` repo ingests; the bench prints every row.

### Task 0.2 — Eval comparison script

- **Files:** new `scripts/perf/eval_compare.py`.
- **Change:** a script that runs `evals.run_evals.run_evals(..., stdlib_tokens=True, oss=False)`
  with `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` fixed to `2024-01-01T00:00:00+00:00`
  (same as `tests/baseline_helpers.fixed_git_env`) and writes
  `{"<repo>|<task description>|<mode>": {"precision", "precision_at_3", "recall", "file_recall", "symbol_recall", "files"}}`
  to a JSON file; with `--compare A.json B.json` it prints the changed rows and
  the mean of each metric for A and B.
- **Acceptance:** two runs on `main` produce identical JSON.
- **Decision rule used by later tasks ("eval gate"):** a ranking change is
  acceptable only if (a) no row's `recall` or `file_recall` decreases and (b)
  each metric's mean is ≥ the baseline's. Record the `--compare` output in the
  commit message.

---

## Phase 1 — Token-savings ledger (report #1, high)

### Task 1.1 — Price the detailed rendering from the work already done; count ledger time

*Behaviour-identical for payloads (`saved_tokens` is a volatile golden key).*

- **Files:** `src/cortex/mcp/tools.py`.
- **Change:**
  1. Add a module global next to `_analysis_memo`:
     `_ledger_detailed: dict[str, Any] | None = None` — the `_content(...)`
     result of the *detailed* rendering of the current call. Reset it to `None`
     in `call_tool`'s `finally`. Add
     ```python
     def _stash_detailed_rendering(result: dict[str, Any]) -> None:
         global _ledger_detailed
         if _in_tool_call:
             _ledger_detailed = result
     ```
  2. In each structure-only handler, stash the detailed rendering on every
     **non-error** return path, built from data already computed (never by
     re-running the query):
     - `_call_search`: before the concise branch pops `why`, stash
       `_content(_format_payload({"repo_path": ..., "items": copy.deepcopy(nodes)}, status, "detailed"))`.
       If `response_format == "detailed"`, stash the real result instead.
     - `_call_relations`: if concise, also build
       `[_relation_item(edge, nodes, "detailed") for edge in edges]`, fit it with
       `_prefix_fit_items(detailed_items, budget)` and stash the formatted
       detailed payload with the same keys (`repo_path`, `items`, `truncated`,
       `returned_count`). If detailed, stash the real result.
     - `_call_path`: the payload does not depend on the format, so stash
       `_content(_format_payload(payload, status, "detailed"))` on both the
       success path and the `ambiguous` path.
     - `_call_overview`: if detailed, stash the real result. If concise, compute
       the detailed `extra_fields` exactly as the detailed branch does
       (`_overview_detailed_capabilities(...)`, then the `dead_code_partial`
       fields), call `_fit_overview_report(repo_root, analysis, budget, extra_fields=...)`
       a second time (it is a cheap binary search over an already-built
       analysis) and stash `_content(_format_payload(detailed_payload, status, "detailed", cached=cache_hit))`.
  3. `_detailed_rendering_tokens(tool, arguments)`: if `_ledger_detailed` is
     not `None`, parse **it** with the existing code
     (`json.loads(result["content"][0]["text"])` → `_without_meta` → `json.dumps`
     → `count_text_tokens`) — byte-identical to the old count. Else, if
     `_in_tool_call` is true, return `0` (never re-dispatch inside a tool call).
     Else keep the old re-dispatch (direct callers and existing tests use it).
  4. Move the ledger inside the tracked region but outside the deadline:
     ```python
     with REGISTRY.track(name, tracked_id) as request_status:
         _current_request = request_status
         with deadline_scope(budget_seconds, tool_name=name):
             result = _dispatch_tool(name, args)
         _record_tool_usage(name, args, result)
     ```
     keeping the existing `except DeadlineExceeded` / `except Exception`
     handlers around the `with REGISTRY.track(...)` block, unchanged.
     `_record_tool_usage` never raises, so `track` still records success.
  5. Update the `_detailed_rendering_tokens` and `call_tool` docstrings (they
     describe the re-dispatch).
- **Tests:** add to `tests/test_token_savings.py`:
  - for each of `cortex_search_symbols`, `cortex_relations`, `cortex_path`,
    `cortex_overview`, in both formats: the ledger row's `baseline_tokens`
    written by `call_tool` equals `_estimate_baseline(...)` computed with the
    legacy re-dispatch (call `_detailed_rendering_tokens` directly outside a
    tool call) — exact equality;
  - a dispatch-count test: monkeypatch `CortexStore.fetch_graph` with a
    counting wrapper; one `call_tool("cortex_path", ...)` calls it exactly once
    (it is 2 today); same for `shortest_paths` (patch `mcp_tools.shortest_paths`);
  - `cortex_status` after a `cortex_path` call: its `slowest_seconds` for
    `cortex_path` is ≥ the wall time measured around `call_tool` minus 0.05s.
- **Acceptance:** goldens unchanged; bench `cortex_path` drops from ~6.3s to
  ~3.4s (it drops further after Task 3.1).

---

## Phase 2 — Qt extraction correctness (report #2, high)

### Task 2.1 — Never fabricate a `QObject` owner; brace-on-next-line and export macros in the regex Qt passes

*Changes extraction output.*

- **Files:** `src/cortex/structural/regex_backend.py`, `src/cortex/store.py`
  (`GRAPH_FORMAT_VERSION = 4`), `CHANGELOG.md`.
- **Change:**
  1. Add a helper that finds the class owning a line from the class nodes
     already extracted for this file (both backends produce class nodes with
     correct `span_start`/`span_end` when the parse is sound):
     ```python
     def _class_owner_by_line(path: str, nodes: list[GraphNode]) -> Callable[[int], str]:
         """Innermost class/struct symbol of `path` whose span contains the line, or ''."""
     ```
     Use nodes with `source_ref == path`, `granularity == "symbol"`,
     `kind == "class"`, integer spans, and a `node_id` that does **not** end
     with `:QObject` unless the file really declares `class QObject`. Pick the
     smallest containing span; ties by `node_id`.
  2. Fix the line-based scan as the fallback for files with no class nodes:
     replace the `_QT_CLASS_RE.match(line)` scans in `_extract_qml_cpp_registration`
     and `_extract_qt_cpp_edges` with a small state machine: a line matching
     `^\s*(?:class|struct)\s+(?:[A-Z][A-Z0-9_]*\s+(?=[A-Za-z_]))?(?P<name>[A-Za-z_]\w*)\b[^;]*$`
     sets a *pending* class; the first `{` seen while pending (same line or a
     later line) opens it at the current brace depth; the class ends when the
     depth returns to that level. A `;` before any `{` cancels the pending class
     (forward declaration `class Foo;`).
  3. In `_extract_qml_cpp_registration`, the Q_PROPERTY / Q_INVOKABLE loop: the
     owner is `owner_of(lineno)` (helper first, fallback scan second). If
     there is no owner, **skip** that line. Delete the `or "QObject"` fallback.
     The QML-macro loop above it uses the same owner function in place of
     `class_lines`.
  4. In `_extract_qt_cpp_edges`, tag `qt = "qobject"` on the owner of every line
     containing `Q_OBJECT` (owner function, not `current_class`).
  5. `_QT_CLASS_RE` (used by `_extract_cpp_inheritance_edges`): allow the `{` on
     a later line and an optional all-caps export macro:
     `^\s*(?:class|struct)\s+(?:[A-Z][A-Z0-9_]*\s+(?=[A-Za-z_]))?(?P<name>[A-Za-z_]\w*)\b(?:\s*final\b)?(?:\s*:\s*(?P<bases>[^{;]+))?\s*\{`
     with `re.MULTILINE` (the `[^{;]+` already spans newlines). Check every
     other use of `_QT_CLASS_RE` still behaves (grep for it).
  6. `_CPP_DEF_PATTERNS` class pattern (`regex_backend.py:43`): insert the same
     optional export-macro group before `(?P<name>...)` so
     `class MYLIB_EXPORT Player` is named `Player`. The lookahead
     `(?=[A-Za-z_])` keeps `class FOO {` named `FOO`.
  7. Bump `GRAPH_FORMAT_VERSION` to 4.
- **Tests:** new `tests/test_qt_owner_extraction.py`, run under both backends
  (parametrize; use `CORTEX_FORCE_REGEX` / the dispatch helpers the existing Qt
  tests use):
  - the header from Task 0.1 (brace on next line, with and without
    `WIDGETS_EXPORT`, plus a `Q_GADGET` struct with `Q_PROPERTY` and `Q_ENUM`):
    no node label `QObject`, no node id containing `:QObject.`; properties are
    `Widget.value`, invokable `Widget.refresh`; the class has
    `metadata["qt"] == "qobject"`; an `inherits` edge `Widget -> name:QObject` exists;
  - a `Q_PROPERTY` line outside any class produces no property node;
  - every `.h` in `evals/fixtures/oss/cpp_qt_sortfilterproxymodel`: zero nodes
    labelled `QObject` (17 files have one today).
- **Goldens expected to change:** `extraction/*/qml_full_repo/Widget.hpp.json`,
  `__whole_fixture__.json`, `graph/*/qt_app.json` (only `QObject`-owned nodes and
  edges replaced by correctly owned ones, plus `qt` tags/`inherits` edges for
  brace-on-next-line classes), possibly `mcp/payloads/*`. Do **not** touch the
  `baseline-7b0e6e0` goldens. If anything else changes, stop and explain it.
- **Acceptance:** tests pass under both backends; CHANGELOG names the goldens
  and the format bump.

### Task 2.2 — Mask Qt and export macros before tree-sitter parses C++

*Changes extraction output.*

- **Files:** `src/cortex/structural/treesitter_backend.py`, `src/cortex/store.py`
  (`GRAPH_FORMAT_VERSION = 5`), `CHANGELOG.md`.
- **Change:**
  1. Add `_mask_qt_macros(source: bytes) -> bytes` that works on **bytes** and
     replaces every byte of a match except `\n` with `b" "`, so byte offsets
     and (row, column) points are unchanged. Masking the decoded `str` instead
     would shift offsets after any non-ASCII character. Assert
     `len(result) == len(source)`. Mask:
     - bare macros: `Q_OBJECT`, `Q_GADGET`, `Q_NAMESPACE`, `Q_INVOKABLE`,
       `Q_SIGNAL`, `Q_SLOT`, `QML_ELEMENT`, `QML_SINGLETON`, `QML_ANONYMOUS`;
     - macro calls with their (one level of nested) parentheses:
       `Q_PROPERTY`, `Q_CLASSINFO`, `Q_ENUM`, `Q_ENUM_NS`, `Q_FLAG`, `Q_FLAG_NS`,
       `Q_ENUMS`, `Q_FLAGS`, `Q_INTERFACES`, `Q_DECLARE_FLAGS`, `Q_DISABLE_COPY`,
       `Q_DISABLE_COPY_MOVE`, `QML_NAMED_ELEMENT`, `QML_UNCREATABLE`,
       `QML_FOREIGN`, `QML_EXTENDED`
       (pattern `\b(?:NAMES)\s*\((?:[^()]|\([^()]*\))*\)`);
     - section labels at line start: `signals`/`Q_SIGNALS` → `public` padded
       with spaces to the same length; `slots`/`Q_SLOTS` (with optional
       preceding `public|protected|private`) → spaces, keeping the colon;
     - an all-caps export macro between `class|struct` and the class name:
       `^(\s*(?:class|struct)\s+)([A-Z][A-Z0-9_]*)(\s+)(?=[A-Za-z_]\w*\s*(?:final\s*)?[:{]|[A-Za-z_]\w*\s*$)`
       (multiline) → spaces.
     Do not mask `emit`/`Q_EMIT`: that would turn emits into `calls` edges. It
     is out of scope.
  2. In `extract_treesitter_edges`, for `suffix in _CPP_SUFFIXES` only:
     `tree = parser.parse(_mask_qt_macros(source))` but keep passing the
     **original** `source` to every text helper (`_node_text`, `_signature`,
     `call_sites.CallContext`), so signatures still show `Q_INVOKABLE`.
  3. Bump `GRAPH_FORMAT_VERSION` to 5.
- **Tests:** in `tests/test_qt_owner_extraction.py` (tree-sitter only):
  `_mask_qt_macros` preserves length and newline positions, including for a
  `Q_CLASSINFO("Author", "Jürgen")` line (multi-byte); parsing the masked Task
  0.1 header yields no `ERROR` node and a `class_specifier` named `Widget{k}` for
  both the plain and export-macro variants; `Q_INVOKABLE void refresh();` now
  yields a `refresh` symbol inside the class span with the original signature
  text.
- **Goldens expected to change:** tree-sitter extraction/graph goldens of
  `qml_full_repo` and `qt_app` only (members of Qt classes now parsed instead of
  ERROR-recovered). Regex goldens must **not** change.
- **Acceptance:** as Task 2.1. On the bench `--qt` repo:
  `sqlite3 <db> "select count(*) from graph_nodes where label='QObject' or node_id like '%:QObject.%'"`
  returns 0.

---

## Phase 3 — Shared speed infrastructure

### Task 3.1 — In-process graph cache for read-only MCP tools

*Behaviour-identical.*

- **Files:** `src/cortex/mcp/tools.py`, `src/cortex/report.py` (`build_report_data`
  gains optional `graph`), `src/cortex/mcp/status.py` (report cache occupancy).
- **Change:**
  1. In `tools.py`, a single-slot cache like `_analysis_memo`:
     ```python
     _graph_cache: tuple[tuple[str, str, str, int], tuple[list[GraphNode], list[GraphEdge]]] | None = None

     def _cached_graph(store: CortexStore, repo_root: Path, status: dict[str, Any]) -> tuple[list[GraphNode], list[GraphEdge]]:
         """fetch_graph, memoised per (db, repo, indexed fingerprint, indexed_at).
         The returned lists and objects are shared: callers must not mutate them."""
     ```
     Key: `(str(store.db_path), str(repo_root.resolve()), status["fingerprint"], int(status.get("indexed_at") or 0))`
     (`status` is the dict `_ensure_fresh` returns: `fingerprint` is the
     *indexed* fingerprint, so another process's ingest changes the key).
     Disabled when `CORTEX_GRAPH_CACHE=0`.
     Not cleared in `call_tool`'s `finally` (it must survive across calls).
     Cleared in `_call_refresh` before ingest.
  2. Use it in the read-only handlers only: `_call_query`, `_call_impact` (both
     sites), `_call_context`, `_call_risk`, `_call_path`, the dead-code helper
     (line ~1850), and `_call_overview` → `build_report_data(..., graph=...)`.
     **Never** in ingest/enrich: `ingest.py:559` and `enrich.py:190` mutate
     what they fetch (`hotspots.py:206`, `callresolve.py:710`).
  3. Audit every consumer reached from those handlers for mutation of nodes,
     edges, their `metadata` dicts, or the lists themselves (`grep -n "\.metadata\[.*\] *=\|\.metadata\.update\|\.append(\|\.extend(\|\.sort("`
     in `bundle.py rank.py risk.py impact.py impacted_tests.py deadcode.py report.py community.py hotspots.py pathfind.py references.py`).
     Where a consumer sorts or appends to a passed list, make it copy first.
  4. `cortex_status` (`status.py` snapshot): add `"graph_cache": {"nodes": N, "edges": M}`
     or `null`. Keep it outside the per-tool counters. Regenerate the
     `cortex_status` shape goldens.
- **Tests:** new `tests/test_graph_cache.py`:
  - two consecutive `cortex_impact` calls → `fetch_graph` called once;
  - after `cortex_refresh` with a changed file → called again;
  - `CORTEX_GRAPH_CACHE=0` → called every time;
  - **mutation guard:** ingest the Qt fixture, call every read tool (query,
    impact, context, risk, path, dead_code, overview, in both formats), then
    assert the cached graph equals a fresh `store.fetch_graph` (compare
    `[n.to_dict() for n in nodes]` and the edges the same way).
- **Goldens expected to change:** `mcp/shapes/cortex_status.*.json` only.
- **Acceptance:** bench (second call of each tool): `cortex_impact`,
  `cortex_path` and `cortex_risk` drop by about the `fetch_graph` time (~2s).
  Note the memory cost (one graph, about the same as one `fetch_graph` result)
  in `CHANGELOG.md`, together with the kill switch.

---

## Phase 4 — `cortex_risk` test selection and truncation (report #3, #4)

### Task 4.1 — C/C++ test naming and stricter pairing

*Changes output.*

- **Files:** `src/cortex/testpatterns.py`, `src/cortex/report.py`
  (`_looks_like_src_test_pair`), `src/cortex/impacted_tests.py`, `src/cortex/risk.py`
  (uses `_looks_like_src_test_pair`; picks up the change automatically).
- **Change:**
  1. `testpatterns.py`: add
     ```python
     @dataclass(frozen=True)
     class TestName:
         base: str      # normalised comparison base
         is_test: bool
         by_name: bool  # the file name itself follows a test naming convention
     def test_name_info(path: str) -> TestName: ...
     ```
     and make `test_base_name(path)` return `(info.base, info.is_test)`
     (unchanged for every non-C/C++ suffix). Mark both with `__test__ = False`.
     For `.c .cc .cpp .cxx .h .hh .hpp .hxx`:
     - name markers, applied to the lowercased stem: prefix `^(?:tst_|test_)+`,
       suffix `(?:_tests?)+$`; and on the **original** stem the CamelCase suffix
       `(?<=[a-z0-9])Tests?$` (`FooTest`, `FooTests`). Any match → `by_name=True`.
     - only when the path is inside a test directory and no name marker
       matched: strip `(?<=[a-z0-9])tests?$` from the lowercased stem
       (`footest` → `foo`), `by_name=False`.
     - `is_test = by_name or _is_test_directory(parts)`.
  2. Add `pairs_with_test(source: str, test: str) -> bool` in `testpatterns.py`:
     ```text
     GENERIC_STEMS = {"main", "utils", "util", "common", "config", "helpers", "helper",
                      "types", "constants", "index", "init", "__init__", "mod", "lib", "app"}
     s = test_name_info(source); t = test_name_info(test)
     if s.is_test or not t.is_test or s.base != t.base: False
     if t.by_name and s.base not in GENERIC_STEMS: True
     test_dirs = lowercased directory parts of `test` that are not test directories
     source_dir = lowercased name of source's parent directory ('' at repo root)
     if source_dir and source_dir in test_dirs: True          # mirrored layout
     if not test_dirs and s.base not in GENERIC_STEMS: True   # flat tests/ directory
     False
     ```
     Replace the body of `report._looks_like_src_test_pair(a, b)` with
     `return pairs_with_test(a, b) or pairs_with_test(b, a)`. Today's function
     is symmetric and `report.py:64` passes cochange endpoints in either order;
     `risk.py:531` and `impacted_tests.py:584` pass `(source, test)`, for which
     the reversed call is always false. Keep the name so all three callers
     pick up the new rule.
  3. `impacted_tests.select_tests_with_coverage`, tier (c): build once per call
     `tests_by_base: dict[str, list[str]]` from `sorted(known_paths)` filtered
     by `is_runnable_test`; for each changed file, look up its base and keep
     candidates where `pairs_with_test(path, candidate)`. Remove the per-file
     `enumerate(sorted(known_paths))` loop. Results identical apart from the new
     rules.
  4. `_GraphIndex.__init__`, label-based `inherits` attachment (lines 271-278)
     and the `metadata.bases` block: attach only when the label matches **at
     most 3** repo classes (`_LABEL_ATTACH_MAX = 3`), with a comment that a
     label shared by more classes cannot be resolved by name and over-selects.
- **Tests:** `tests/test_testpatterns.py`, table-driven:

  | source | test | pairs? |
  |---|---|---|
  | `src/foo.cpp` | `tests/tst_foo.cpp` | yes |
  | `src/foo.cpp` | `tests/foo_test.cpp` | yes |
  | `src/foo.cpp` | `tests/unit/FooTest.cpp` | yes |
  | `src/foo.cpp` | `tests/unit/footest.cpp` | yes |
  | `src/foo.cpp` | `src/foo_test.cpp` | yes |
  | `ui/main.cpp` | `tests/integration/main.cpp` | no |
  | `ui/main.cpp` | `tests/ui/main.cpp` | yes |
  | `app/main.cpp` | `tests/main_test.cpp` | no |
  | `src/net/socket.cpp` | `tests/net/socket.cpp` | yes |
  | `src/net/socket.cpp` | `tests/socket.cpp` | yes |
  | `src/net/socket.cpp` | `tests/db/socket.cpp` | no |
  | `src/contest.cpp` | `src/contest.cpp` | no (not a test) |
  | `a.py` | `tests/test_a.py` | yes (unchanged) |

  Plus `test_base_name` results for every existing non-C++ case are unchanged
  (the existing tests cover this). In `tests/test_impacted_tests.py`: a
  `_GraphIndex` with 5 classes labelled `Item` and an unresolved
  `inherits name:Item` edge attaches nothing; with 2 classes it attaches both.
- **Goldens expected to change:** possibly `mcp/payloads/*/cortex_risk.*` if a
  fixture has C++ tests; nothing else. Dead-code goldens may lose findings in
  newly recognised `*_test.cpp`/`tst_*.cpp` files (correct: they are tests).
- **Acceptance:** bench `--qt --qt-edit` `cortex_risk` row: `tests_to_run`
  includes `tests/qt/tst_widget4.cpp`, and neither `tests/unit/main.cpp` nor
  `tests/integration/main.cpp`.

### Task 4.2 — `truncate_risk_result`: later drop, compact form, `omitted_fields`, no O(n²)

*Changes output for over-budget results only.*

- **Files:** `src/cortex/risk.py`, `tests/test_risk_shape.py`, `src/cortex/mcp/tools.py`
  (the `cortex_risk` tool description, if it documents the fields).
- **Change:**
  1. Track every top-level key removed in an ordered list and, when non-empty,
     set `output["omitted_fields"] = [...]` (in drop order). Only fully
     removed keys go in it; shortened lists (`files`, `directives`,
     `tests_to_run`) are already reported by `truncated` and `returned_count`.
     Add `omitted_fields` to the budget measurement like any other key.
  2. Move `tests_by_changed_file` from the head of the second drop list to
     **after** `unindexed_files`, i.e. the last secondary bucket before
     directives are shortened. Before removing it, try a compact form once:
     replace each test path with its index in the current `output["tests_to_run"]`
     (`{"src/a.cpp": [0, 3]}`) and set
     `output["tests_by_changed_file_encoding"] = "tests_to_run_index"`; drop it
     only if the payload still does not fit. When `tests_to_run` is shortened
     later, drop indices that are now out of range (and drop a changed file
     whose list becomes empty).
  3. Replace the three one-at-a-time `pop()` loops (`files`, `directives`,
     `tests_to_run`) with a binary search over the prefix length, the same
     technique `deadcode.truncate_dead_code_result` uses. Size is monotone in
     the prefix length, so the result is identical. For `tests_to_run`, the
     `tests_run_all` switch must still happen whenever at least one item is
     removed.
  4. Update `DROP_ORDER` in `tests/test_risk_shape.py` to the new order and
     add assertions: `omitted_fields` lists exactly the keys missing from the
     output; at the budget where `tests_by_changed_file` first changes it is in
     the compact form with in-range indices.
- **Tests:** above, plus an equivalence test: for budgets 0..full+20 on
  `_synthetic_result()`, the new `files`/`directives`/`tests_to_run` prefixes
  equal those of a copy of the old loop implementation kept in the test.
- **Goldens expected to change:** `mcp/shapes/cortex_risk.*` only if a golden
  run is over budget (then `omitted_fields` appears).
- **Acceptance:** risk on `HEAD~30..HEAD` of the bench repo with the default
  budget keeps `tests_by_changed_file` (compact form allowed) and names every
  dropped key in `omitted_fields`.

---

## Phase 5 — `cortex_query` speed and ranking (report #5, #6)

### Task 5.1 — File-level PageRank, with deadline headroom

*Changes ranking; must pass the eval gate.*

- **Files:** `src/cortex/rank.py`, `src/cortex/bundle.py`, `src/cortex/mcp/tools.py`,
  `tests/test_rank.py`, eval goldens.
- **Change:**
  1. `rank.py`: add
     ```python
     def collapse_to_file_graph(nodes, edges) -> tuple[list[GraphNode], list[GraphEdge]]:
         """Map every symbol node to `file:<source_ref>`, drop self-loops, and merge
         parallel edges per (source, target, layer) by summing weights."""
     ```
     Keep non-symbol, non-commit nodes (files, modules, targets) as they are.
     Edge ids: `f"file-rank:{source}>{target}>{layer}"`. Iterate edges in input
     order and emit merged edges sorted by `(source, target, layer)` so float
     accumulation is deterministic. Leave `personalized_pagerank` unchanged.
  2. `generate_bundle`: accept an optional keyword `rank_graph` (the collapsed
     graph); when absent, build it with `collapse_to_file_graph(nodes, edges)`.
     Run `personalized_pagerank(*rank_graph, seed_scores)`. Only `file:` keys
     are read for candidate scores, as today.
  3. `_skeleton_item(item, symbols, node_scores, remaining)` received symbol
     PageRank scores, which no longer exist. Pass a `symbol_scores` dict built in
     the span-scoring loop (`node_id -> name_bonus + span_score`, for spans with
     a positive score). **Eval gate decides:** if the gate fails with
     `symbol_scores`, pass `{}` instead (verified during the investigation to
     pass the gate: one row improves, none regress).
  4. In `tools.py`, memoise the collapsed graph next to the graph cache entry
     (same key) and pass it as `rank_graph`.
  5. Deadline headroom. In `generate_bundle`, around the PageRank call:
     ```python
     outer = current_deadline()
     if outer is not None and outer.enabled:
         reserve = max(2.0, 0.25 * outer.budget_seconds)
         allowance = outer.remaining - reserve
         if allowance <= 0:
             pagerank_scores = {}; degraded = True
         else:
             try:
                 with deadline_scope(allowance, tool_name="pagerank"):
                     pagerank_scores = personalized_pagerank(...)
             except DeadlineExceeded:
                 pagerank_scores = {}; degraded = True
     ```
     The inner scope always expires before the outer one, so the outer budget
     keeps `reserve` seconds for candidate building and packing. When
     `degraded`, append `"Graph ranking skipped to fit the time budget."` to
     `confidence_notes` and set `bundle["degraded"] = ["graph_rank"]` (JSON
     output only). `_call_query` must **not** `_cache_set` a degraded payload.
- **Tests:** `tests/test_rank.py`: `collapse_to_file_graph` merges parallel
  edges, drops self-loops, keeps file and module nodes; determinism (two runs
  produce identical floats). `tests/test_bundle_*.py` (new test): with a fake
  deadline whose `remaining` is below the reserve, `generate_bundle` returns
  items and the degraded note; `_call_query` does not cache it.
- **Goldens expected to change:** `tests/goldens/evals/default_matrix.json`
  and `default_files.*.json`: exactly the `python_app` "markdown setup"
  `skeleton_off` row (precision 0.4 → 0.5), or a set the eval gate accepts.
  Possibly `mcp/payloads/*/cortex_query.*`.
- **Acceptance:** eval gate passes; bench cold `cortex_query` drops from
  ~10.5s to ≤ 6s; the profile no longer shows `personalized_pagerank` above
  ~0.6s.

### Task 5.2 — Remove the other O(corpus) and O(edges) work from a query

*Behaviour-identical.*

- **Files:** `src/cortex/bundle.py`, `src/cortex/mcp/tools.py`.
- **Change:**
  1. Span prefilter (lossless). In the symbol-span loop, skip all spans of a
     file when the file's tokens (already computed for `_term_weights`: keep a
     `tokens_by_path` dict instead of a throwaway list) share no term with
     `task_terms` minus `noise_terms`; a span can then only score through
     `name_bonus`, so still check `task_terms & _tokenize_text(symbol.label)`
     per symbol and score just those. Do not use the file *score* for this:
     `_score_text` adds `recency_weight` even with zero overlap.
  2. `_bundle_why` is called per item and scans all edges. In `_call_query`,
     build once: `seed_nodes = {f"file:{p}" for p in seed_paths}`, and a dict
     `file_node -> list[(seed_path, edge)]` from one pass over the edges where
     one endpoint is in `seed_nodes` and the other starts with `file:`. Pass the
     relevant list into `_bundle_why` instead of `edges`. Keep the output
     identical, including the `(-weight, edge_id)` sort and the `[:5]` cap.
- **Tests:** existing bundle/query tests and goldens unchanged; add a test that
  `_bundle_why` output is identical to the old implementation (kept in the
  test) on the Qt eval fixture for every selected item.
- **Acceptance:** goldens unchanged; bench cold `cortex_query` improves further
  (record the numbers).

### Task 5.3 — Spread the query budget across files

*Changes ranking; must pass the eval gate.*

- **Files:** `src/cortex/bundle.py`, `evals/run_evals.py` (one new gold task), eval goldens.
- **Change:**
  1. **Nested spans.** In the packing loop, skip a symbol item whose
     `[span_start, span_end]` lies inside an already selected symbol item of the
     same path (keep a `selected_spans: dict[path, list[tuple[int, int]]]`).
     Also skip a symbol item when its whole file is already selected (this
     check exists already).
  2. **Per-file decay.** After building `symbol_items_by_path`, multiply the
     score of the k-th best span of each file (0-based, in the existing sort
     order) by `SPAN_SCORE_DECAY ** k` with `SPAN_SCORE_DECAY = 0.5`, before
     the global `candidates.sort`. Comment why: name matches give every span of
     the matching file +100, so one file otherwise takes the whole budget.
  3. **Reserve for call-expanded files.** Remember which paths were lifted by
     the call-expansion block (`expanded_paths`) and, per expanded path, the
     symbol ids on its side of the resolved `calls`/`inherits` edges to the seed
     files (extend `_call_graph_file_neighbors` to also return
     `(file, neighbor) -> set[neighbor symbol id]`). Hold back
     `CALL_EXPANSION_BUDGET_SHARE = 0.15` of the budget during the main loop
     while some expanded path is not selected. Afterwards, for each unselected
     expanded path in score order, select a skeleton of that file built from
     only the called symbols (`_skeleton_item(item, called_symbols, symbol_scores, remaining)`),
     falling back to the full skeleton and then to nothing. Leftover reserve
     returns to the normal loop (run the main loop once more over the
     remaining candidates with the released budget).
  4. Add a gold task to `evals/run_evals.py` reproducing the report: a fixture
     where `fileA.cpp` has 6 methods whose names contain the task term, and
     `fileA.cpp` calls into `base.cpp` and `manager.cpp`, which share no term
     with the task. Gold: `fileA.cpp`, `base.cpp`, `manager.cpp`.
- **Tests:** `tests/test_bundle_spans.py`: nested spans are not both selected;
  decay order; with the new fixture, `base.cpp` and `manager.cpp` appear.
- **Goldens expected to change:** eval goldens (new rows; existing rows must
  pass the eval gate), possibly `mcp/payloads/*/cortex_query.*`.
- **Acceptance:** eval gate on existing rows; the new task's recall is 1.0. If
  the gate fails, first drop item 2 (decay) and retry; if it still fails,
  keep only items 1 and 4 and record the failing rows in the PR.

---

## Phase 6 — Overview and dead code (report #7, #8)

### Task 6.1 — Stoppable reference corpus; narrower corpus for scoped runs

*Behaviour-identical when not stopped.*

- **Files:** `src/cortex/references.py`, `src/cortex/deadcode.py`.
- **Change:**
  1. `references.py`: add `class AnalysisStopped(Exception)`. Give
     `ReferenceCorpus.__init__` an optional `should_stop: Callable[[], bool] | None`;
     check it every 200 files while reading, and every 200 files in
     `_symbol_files`; raise `AnalysisStopped` when it returns true. Store
     `should_stop` on the instance for `_symbol_files`.
  2. `deadcode._AnalysisContext`: accept and forward `should_stop`.
     `analyze_dead_code`: wrap each candidate's `_candidate_reason(...)` call in
     `try/except AnalysisStopped`: set `partial = True` and `break` **without**
     appending a finding for that candidate.
  3. When `path_prefix` is set, build the corpus with
     `symbols = {labels of the candidates inside the prefix}` instead of every
     symbol label (`files_for` already falls back to a substring scan for other
     symbols, so results are identical).
- **Tests:** `tests/test_deadcode.py`: a `should_stop` that turns true after N
  corpus files yields `partial=True` and only findings for candidates fully
  processed; a never-stopping run equals today's output.
- **Acceptance:** goldens unchanged.

### Task 6.2 — Resume partial dead-code analysis; cache the rest of the overview

*Changes behaviour of partial runs only.*

- **Files:** `src/cortex/deadcode.py`, `src/cortex/report.py`, `src/cortex/mcp/tools.py`.
- **Change:**
  1. `analyze_dead_code(..., resume: Mapping[str, Any] | None = None)`. The
     candidate order is already the sort key `(source_ref, label, node_id)`.
     On a stop, return `"cursor": [source_ref, label, node_id]` of the first
     candidate **not** processed. With `resume={"cursor": [...], "findings": [...]}`,
     skip candidates whose key is `<` the cursor and start `findings` from the
     given list. The final sort is unchanged, so a run resumed to the end
     produces exactly the single-run result.
  2. `report.build_report_data(..., resume=None)` passes it through and returns
     `dead_code_cursor` when partial.
  3. `_call_overview`: bump `_OVERVIEW_ANALYSIS_CACHE_VERSION` to 2. Always
     cache the analysis, partial or not, with its cursor. On a cache hit whose
     analysis is partial: run `analyze_dead_code` with
     `resume={"cursor": analysis["dead_code_cursor"], "findings": analysis["dead_code"]}`
     and the graph from the graph cache, merge the result into the cached
     analysis (`dead_code`, `dead_code_partial`, `dead_code_cursor`) and write it
     back. Communities, hotspots, god nodes and surprising connections are
     **not** recomputed, and `save_communities` is not called again.
     `cached` in `_meta` stays true only for a complete hit.
  4. The dead-code helper (`_dead_code_analysis`, line ~1817): for a repo-wide
     call, use the same cached overview analysis (complete, or resume it as
     above). Path-scoped calls keep today's behaviour, except they may filter a
     complete repo-wide result (they already do).
  5. Add a `"dead_code_progress": {"processed": i, "total": n}` field to the
     partial overview payload so a caller sees it advancing.
- **Tests:** `tests/test_dead_code_cache.py`:
  - **equivalence:** with a `should_stop` that stops after every 3 candidates,
    resuming until `partial` is false yields findings identical to one
    unstopped run (Qt fixture and `tests/fixtures/qml_dead_code`);
  - two consecutive partial `cortex_overview` calls (monkeypatch the stop to
    trip after K candidates): `detect_communities` and `save_communities` are
    called once, the second call's `dead_code_progress.processed` is larger, and
    the call after the last one is complete and `cached`.
- **Goldens expected to change:** `mcp/shapes/cortex_overview.*` only if a
  golden run is partial (it should not be).
- **Acceptance:** on the bench with `CORTEX_TOOL_TIMEOUT_SECONDS=4`,
  repeated `cortex_overview` calls reach `dead_code_partial: false` within a
  few calls instead of never.

### Task 6.3 — Dead-code accuracy and speed details

- **Files:** `src/cortex/deadcode.py`.
- **Change:**
  1. Skip candidates with `node.kind == "target"` (CMake/qmake build targets
     are entry points, not code).
  2. `_OverrideIndex`: build `self._methods_by_label: dict[str, list[GraphNode]]`
     once in `__init__`; in `_declares_method`, replace the loop over
     `self._methods_by_file.values()` with a loop over
     `self._methods_by_label.get(name, ())` keeping the same
     `f"{cls.label}::{name}" in signature` test. Results identical.
- **Tests:** a CMake fixture (`add_library(core a.cpp)`, `add_executable(app main.cpp)`)
  reports no `target` finding; `_declares_method` equivalence with the old
  implementation (kept in the test) on the Qt fixture.
- **Goldens expected to change:** dead-code goldens that list CMake targets
  (`mcp/payloads/*/cortex_dead_code.*` and overview reports if a fixture has
  `CMakeLists.txt`).
- **Acceptance:** bench `analyze_dead_code` drops by about 1.5s.

---

## Phase 7 — Minor (report #9) and release

### Task 7.1 — Cache `cortex_search_symbols`

- **Files:** `src/cortex/mcp/tools.py`.
- **Change:** in `_call_search`, use `_cache_key(status["current_fingerprint"], "cortex_search_symbols", arguments)`
  with `_cache_get`/`_cache_set` like `_call_impact`; cache the bare payload
  for the requested format and pass `cached=` to `_format_payload`. On a hit,
  the Task 1.1 ledger stash is rebuilt from the cached concise payload by
  re-adding `"why": [{"type": "like_query", "query": <query>}]` to each item.
  This is exactly what the handler adds, so no re-query is needed.
- **Tests:** two identical calls → second has `_meta.cached == true` and the
  same items; ledger baseline equal on both calls.
- **Acceptance:** goldens unchanged except where `_meta.cached` is pinned (it
  should not be for a single call).

### Task 7.2 — `version` in `cortex_status`; single source of truth

- **Files:** `src/cortex/mcp/status.py`, `src/cortex/mcp/server.py`.
- **Change:** `SERVER_INFO = {"name": "cortex", "version": __version__}`
  (`from .. import __version__`). Add `"version": __version__` to the
  `cortex_status` result.
- **Goldens expected to change:** `mcp/shapes/cortex_status.*.json` (key sets
  only; no payload golden pins `cortex_status`). `mcp/initialize.json` already
  contains the version string and must stay byte-identical.
- **Tests:** status contains `version == cortex.__version__`.

### Task 7.3 — Release 0.12.0

- Bump the version in `pyproject.toml` and `src/cortex/__init__.py` (plus any
  plugin manifests the existing release commits touch: compare with `git show 07fe2f4 --stat`).
  Move `## Unreleased` to `## 0.12.0` in `CHANGELOG.md` with a short
  summary, and call out the graph-format bump (full rebuild on first refresh),
  the new `CORTEX_GRAPH_CACHE` switch, `omitted_fields`, and the C++ test
  pairing rules.
- Run every check in Rules §2, plus coverage floors, `python evals/run_evals.py`,
  `python -m build && twine check dist/*`.
- Fill in this table in the PR description from the bench (same machine,
  `--qt`, cold): before / after for ingest, `cortex_query` cold, `cortex_path`,
  `cortex_overview` cold, `cortex_dead_code` (repo, path), `cortex_risk`, plus
  the second-call numbers that show the graph cache.

---

## Out of scope (recorded so they are not lost)

- Masking `emit`/`Q_EMIT` for tree-sitter (would add `calls` edges alongside
  `emits`; needs its own golden review).
- `target_link_libraries` → `links` edges between CMake targets. That would let
  `impacted_tests` follow "library changed → tests linking it", which is a real
  recall gain, but it is a new edge kind and needs its own design.
- A reverse-degree hub cutoff in `impacted_tests._structural` (rejected in §1,
  row 3).
- Background (thread) dead-code computation: the resume cursor (Task 6.2)
  gives the same convergence deterministically without threads.
