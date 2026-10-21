# Plan: Cortex 0.12.1 — review findings on 0.12.0

Status: **implemented in 0.12.1.** Outcomes and deviations from the text below:
- A.1: as planned, plus `returned_count` now counts toward the fit (found while checking
  `budget_feasible`). Acceptance was "map kept for at least half of the 117 changed files at
  budget 2000"; the result is 12 of 117 (all 117 at 4000): the changed-file paths alone are
  about 1.4k tokens even in index form. The map is kept and encoded before any other bucket is
  dropped, which is what the review asked for.
- B.1: same-file calls are walked through without spending a hop (otherwise a delegating
  helper ended the chain); `CALLEE_HOP_SHARE` is (0.9, 0.8) instead of (0.9, 0.6), because at
  0.6 a hop-2 callee scored below ~35 term-only files; the call index is memoised with the graph
  cache; the file item carries the route so the `why` also shows for whole-file picks. The
  0.12 reserve was removed and the eval gate passed without restoring it.
- The regression tests described below were added first as strict `xfail`s and their marks
  removed by the fixing tasks.

Input: test review of 0.12.0 on a C++/QML repository (about 3.7k files, 161k nodes, 622k edges)
Base: `main` at 0.12.0 (`aa98ec6`)
Target release: **0.12.1** (bug fixes; no graph format change)

Written to be executed task by task by an implementer who has not seen the
investigation. Rules, setup and checks are the same as
`docs/plan-0.12-field-report.md` §0 (venv, `python -m cortex runtime setup`,
full suite, `git diff --exit-code tests/goldens`, regex-fallback suite, eval
gate via `scripts/perf/eval_compare.py`). One commit per task, CI green after
each.

---

## 1. Verdict

| # | Review claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `cortex_risk` on a large diff (`HEAD~30..HEAD`) returns `truncated: true` with `tests_by_changed_file` removed whole, contradicting the 0.12.0 CHANGELOG ("given up last … re-encoded as indices") | **Valid: truncation-order bug, and the CHANGELOG overstates it** | `truncate_risk_result` (`src/cortex/risk.py`) decides the map (compact or drop) inside the secondary-bucket loop, **before** `directives` and `tests_to_run` are shortened. On a big diff `tests_to_run` alone is larger than the whole budget, so the map can never survive. Reproduced on the 2.2k-file bench repo, `HEAD~30..HEAD`, default budget 2000: the map is dropped (`omitted_fields` ends with it) while 30 `tests_to_run` entries are kept, each with a long `reason` string. Sizes: `tests_to_run` 7.1k tokens (3.7k without `reason`, 1.3k as bare paths), map 3.5k full / 1.4k compact, `directives` 2.8k, 117 changed files, 185 tests. `omitted_fields` itself is correct. |
| 2 | `cortex_query` misses the files with the cause (a base-class implementation and a manager reached by calls from the top match); the 0.12 call-expansion reserve fills with weak neighbours | **Valid** | Reproduced with a fixture (§Task B.0): the top match `DeviceManager::reportDevice` resolves a `calls` edge to `PeripheralCore::assignCompanion` (which calls `Registry::drop`). At budgets 600 and 900 neither callee file is in the bundle; at 1500 the direct callee appears last and the second hop never does. Root causes: (a) call expansion is **file-level and undirected**: every neighbour of the 8 best *files*, callers included, is lifted to the same `CALL_EXPANSION_BONUS * score/top` (at most 20), so the resolved callee of the top *symbol* ties with an unrelated caller (`admin_control.cpp` scored 20.7 vs the callee's 20.8) and ties break by path; (b) 20 is below every file that shares a single task word in its name or path (`NAME_MATCH_BONUS` 100, `PATH_MATCH_BONUS` 40), so in a large repo those fill the budget first; (c) the 0.12 reserve only holds budget back from a file's third and later *spans*, not from term-only whole files or skeletons, so it never protects the callee; (d) only one hop is followed. |

---

## Phase A — `cortex_risk`: keep the per-file test map on large diffs (claim 1)

### Task A.1 — Truncation order that keeps the map

- **Files:** `src/cortex/risk.py` (`truncate_risk_result` and helpers), `tests/test_risk_truncate.py`, `tests/test_risk_shape.py`, `CHANGELOG.md`.
- **New order** once the payload is over budget (each step only while still over):
  1. Shorten `files` (unchanged, binary search).
  2. **Re-encode the map first** (lossless): replace test paths with indices into a self-contained table, not into `tests_to_run`, so later cuts to `tests_to_run` cannot invalidate it:
     ```json
     "tests_by_changed_file": {"src/a.cpp": [0, 3]},
     "tests_by_changed_file_tests": ["tests/tst_a.cpp", "..."],
     "tests_by_changed_file_encoding": "test_table"
     ```
     The table lists each distinct referenced test once, in `tests_to_run` order (highest score first). Remove the old `"tests_to_run_index"` encoding and `_pruned_compact_indices`; nothing outside `risk.py` reads it (grep to confirm).
  3. Drop the other secondary buckets in `_RISK_DROP_ORDER` (without the map), as today.
  4. Strip `reason` from every `tests_to_run` entry (keep `test`, `via`, `score`) and add `"tests_to_run.reason"` to `omitted_fields`.
  5. Shorten `directives` (binary search).
  6. Shorten `tests_to_run`, keeping at least `TESTS_TO_RUN_FLOOR = 10` entries (or all, if fewer); `tests_run_all` switches on as today.
  7. Shorten the map **by changed-file priority**: keep the entries of the highest-risk changed files first (the order of the original `files` list, then path), dropping table tests no longer referenced. Record `tests_by_changed_file_omitted: <count of changed files removed>`.
  8. Shorten `tests_to_run` below the floor.
  9. Only now drop the map whole (and its table and encoding keys) and add it to `omitted_fields`.

  Steps 6–8 use the existing `_longest_fitting_prefix` binary search. Sizes only shrink with the prefix, so each search is exact.
- **Tests:** first remove the two `xfail(strict=True)` marks on the large-diff tests already in
  `tests/test_risk_truncate.py` (built by `_large_diff_result`); they must then pass.
  Then:
  - Update `DROP_ORDER` in `tests/test_risk_shape.py` and the oracle-equivalence test in `tests/test_risk_truncate.py` for the new order. That test currently only covers results without a map; keep it for the order of the other fields.
  - Add a large synthetic result to `tests/test_risk_truncate.py`: 120 changed files, 180 tests and long reasons, built with the existing `_result` style. At budget 2000, assert:
    - the map is present in `test_table` form;
    - every index is in range;
    - decoding gives a subset of the original mapping;
    - `tests_by_changed_file_omitted` plus the kept entries equals the number of changed files;
    - `tests_to_run` has at least 10 entries.
  - Assert the map survives at every budget at which any `directives` survive.
  - Keep the `omitted_fields` exactness test.
- **Acceptance:**
  - On the bench repo, `HEAD~30..HEAD` at budget 2000 keeps the map with entries for at least half of the 117 changed files.
  - Record the exact number in the commit.
  - Goldens unchanged, unless a golden risk payload was over budget; in that case regenerate it and name it in `CHANGELOG.md`.

### Task A.2 — Correct the CHANGELOG

- Under `## 0.12.1`, state that 0.12.0's description of `tests_by_changed_file` was wrong for large diffs: the map was dropped before `tests_to_run` was shortened. Describe the new order and the `test_table` encoding, which replaces `tests_to_run_index`, a breaking change for anyone who parsed it. The old encoding shipped in 0.12.0 only.

---

## Phase B — `cortex_query`: rank callees of the top match above term-only files (claim 2)

### Task B.0 — Regression fixture and gold task (done with this plan)

- **Files:** `evals/run_evals.py` (new `_build_companion_repo` and `COMPANION_GOLD_TASKS`, kept out of `GOLD_TASKS` like `SPREAD_GOLD_TASKS`; add the repo to `build_fixture_repos`), `tests/test_bundle_call_expansion.py`.
- **Fixture** (C++/QML, file names deliberately share no word with the task):
  - `src/device_manager.{h,cpp}`: `DeviceManager::onAccessoryCaseReplugged` calls `reportDevice`, which calls `d->assignCompanion()` on a `PeripheralCore*`. Add a long `deleteDevice` as well.
  - `src/peripheral_core.{h,cpp}`: `PeripheralCore::assignCompanion` calls `releaseCompanion`, which calls `Registry::drop`.
  - `src/registry.{h,cpp}`: `Registry::drop`.
  - Distractors that share task words only:
    - `src/tag_helper.cpp`;
    - `src/admin_control.cpp`, which *calls* `DeviceManager::deleteDevice`;
    - `qml/DevicePopup.qml`;
    - `src/{device_list_model,case_status,accessory_icons,report_log,replug_watch}.cpp`;
    - 30 files `src/device_widget_{k}.cpp`, each a comment with the task words and a body of about 150 lines.
  - Commit with fixed dates (`_commit_all`).
- **Gold task:** "device is reported and then deleted when its accessory case is replugged", expected files `src/device_manager.cpp`, `src/peripheral_core.cpp`, `src/registry.cpp`, `budget=900, tight_budget=900`.
- **Test:** `tests/test_bundle_call_expansion.py::test_callees_of_the_top_match_are_packed`. Ingest the fixture, run `generate_bundle` at 600 and 900, and assert all three expected files are present. Mark it `xfail(strict=True)` in this commit; Task B.1 removes the mark.
- **Acceptance:** on `main` the test xfails. Verified during the investigation: `peripheral_core.cpp` and `registry.cpp` are both absent at 600 and 900.

### Task B.1 — Directional, symbol-level call expansion

- **Files:** `src/cortex/bundle.py`, `src/cortex/mcp/tools.py` (`_bundle_why`, `_compact_why`), `tests/test_bundle_call_expansion.py`, eval goldens.
- **Change:**
  1. Build a callee index once per bundle from the graph:
     - `calls_from: dict[symbol_id, list[(callee_symbol_id, lineno)]]` over `calls` edges whose target starts with `symbol:` (resolved) and is in another file, in edge order;
     - `bases_of: dict[class_id, list[base_class_id]]` from `inherits` edges whose target is a symbol.
     Reuse `_call_graph_links`'s single pass, extended to fill these.
  2. **Seeds:** the `CALLEE_SEEDS = 6` highest-scoring symbol-span candidates (score > 0), plus the enclosing class of each seed.
  3. **Expansion:** breadth first from each seed through `calls_from`, plus `bases_of` for the seed's class.
     - Hop weights: `CALLEE_HOP_SHARE = (0.9, 0.6)` for depth 1 and 2, and 0.6 for a base class at depth 1.
     - At most `CALLEES_PER_SEED = 4` direct callees per seed, taken in call-site line order.
     - Callers are not expanded here.
     - A callee's score is `max(own span score, seed span score * hop share)`; keep the best over seeds.
  4. **Candidates:** for each expanded callee symbol, ensure a `symbol` span `BundleItem` exists with that score and metadata `{"call_expansion": {"from": seed_id, "hop": n}}`. Create it if the span had no keyword score. Raise the callee file's `source_scores` entry to `max(current, callee score * SYMBOL_FILE_SCORE_SHARE)`; files stay below their own best callee span.
  5. **Old file-level lift:** keep `CALL_EXPANSION_BONUS` (undirected, ≤ 20) for the neighbours that step 3 did not reach. **Remove** the 0.12 reserve: `CALL_EXPANSION_BUDGET_SHARE`, `SPANS_BEFORE_RESERVE`, `pack_call_expanded`, and `pack`'s `span_ceiling` parameter. Callees now compete on score, and the reserve did not protect them. Keep the nested-span skip.
     **Decision rule:** if the eval gate fails without the reserve, restore it unchanged and rerun.
  6. **Why:** `_bundle_why` adds `{"type": "call", "from": <seed node id>, "hop": n}` for items carrying `call_expansion`; `_compact_why` renders `"calls: <seed label> -> <callee label>"`, checked before the keyword branch.
- **Tests:**
  - Remove the `xfail` from B.0.
  - Add a unit test: two seeds call the same callee and its score is the best of the two; callers of a seed are not lifted by this step.
  - Add a cap test: a seed calling 10 functions yields 4 callee items.
  - Keep `tests/test_bundle_spans.py::test_the_spread_task_reaches_the_helpers_its_spans_call_into` passing (it must, via callees instead of the reserve).
- **Acceptance:**
  - Eval gate (`scripts/perf/eval_compare.py`, recall gated) passes on the 17 historical tasks.
  - The companion task has recall 1.0 in all four modes.
  - The spread task stays at 1.0.
  - Regenerate the eval goldens and name them in `CHANGELOG.md`.
  - Bench cold `cortex_query` is no slower than 0.12.0 by more than 10%; the callee index is one pass over the edges.

### Task B.2 — Release 0.12.1

- Bump the version in the same files as 0.12.0: `pyproject.toml`, `src/cortex/__init__.py`, both plugin manifests, the README line, and the `tests/test_golden_mcp_shapes.py` serverInfo assert. Regenerate `mcp/initialize.json`.
- Add a CHANGELOG `## 0.12.1` with the A and B entries and the A.2 correction.
- Run every check, including coverage floors and `python evals/run_evals.py`.

---

## Out of scope

- Following `connects`/`emits` (Qt signal-slot) edges in query expansion: same mechanism, but it needs its own gold task.
- A per-test-file inversion of the map (`test -> changed files`): only worth it if A.1's partial map proves insufficient on the reviewer's repo.
