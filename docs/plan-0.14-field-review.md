# Plan: Cortex 0.14.0 — 0.13.0 field review (fourth pass)

Status: proposed (not started)
Input: `docs/review-0.13.0-field.md` ("Cortex 0.13.0: field review and fix plan (fourth pass)",
C++/Qt/QML repository, 3.7k files, 161k nodes, 622k edges). Section letters (§A–§H) below refer
to that document.
Base: `main` at 0.13.0 (`f476f21`)
Target release: **0.14.0**. `GRAPH_FORMAT_VERSION` 6 → 7 (extraction and resolver output change,
see Task 1.2), so this is a minor release, not 0.13.1 (same rule as 0.13.0).

This plan is written to be executed task by task by an implementer who has not seen the
investigation. Every task names the files, the change, the tests and the acceptance check. Do the
tasks **in order**; each one is one commit and leaves CI green. Where a task needs a judgement call,
the decision rule is written into the task — follow it rather than improvising. If a decision rule
cannot be met, stop that task, leave it out of the commit, and record what you measured under
"Not met" in `CHANGELOG.md`; do not loosen a gate to make it pass.

---

## 0. Rules for the implementer (read first)

1. **Setup** (once per session):
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ".[llm,watch,dev,tokens]" build twine
   python -m cortex runtime setup          # must report "ready": true
   ```
   Run the suite once more **without** `tiktoken` (`pip uninstall -y tiktoken`) before the PR:
   the reviewer and two 0.13.0 regressions only showed up with the stdlib token estimate.
2. **Checks before every commit** (what CI runs, `.github/workflows/ci.yml`):
   ```bash
   python -m pytest tests/ -q
   git diff --exit-code tests/goldens      # unless the task lists the goldens it changes
   CORTEX_FORCE_REGEX=1 CORTEX_RUNTIME_NETWORK=0 python -m pytest -q \
     tests/test_structural_dispatch.py tests/test_qt_relations.py tests/test_qt_emits.py \
     tests/test_context_mcp.py tests/test_qml_full_support.py tests/test_qml_modules.py \
     tests/test_qml_resolution.py tests/test_calls_cpp.py tests/test_calls_js.py \
     tests/test_calls_multilang.py tests/test_golden_extraction.py tests/test_golden_mcp_shapes.py
   ```
   Before the PR also run: coverage floors
   (`python -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json && python scripts/check_coverage_floors.py`),
   `python evals/run_evals.py`, `python -m build`.
3. **Goldens.** A task that does not list a golden must not change it; if one changes, stop and
   find out why. To regenerate:
   `CORTEX_UPDATE_GOLDENS=1 python -m pytest tests/test_golden_*.py tests/test_eval_baseline.py`,
   re-run without the variable, review the diff (only what the task intends), and add a
   `CHANGELOG.md` line naming each golden and why (`CONTRIBUTING.md`, "Golden snapshots").
4. **Eval gate** for every Phase 5 task (and any task that says so):
   ```bash
   git stash -u && python scripts/perf/eval_compare.py /tmp/before.json && git stash pop
   python scripts/perf/eval_compare.py /tmp/after.json
   python scripts/perf/eval_compare.py --compare /tmp/before.json /tmp/after.json
   ```
   It passes only if no row's `recall` or `file_recall` decreases and each metric's mean is >= the
   baseline's. Paste the `--compare` summary into the commit body. Never commit `evals/RESULTS.md`.
5. **Profile runs.** "`--field-shape` repo" below means
   `python scripts/perf/synthetic_repo.py /tmp/field-repo --modules 40 --commits 120 --field-shape`,
   indexed into a fresh `CORTEX_DATA_DIR`. Cold-time measurements use the procedure in the 0.13.0
   CHANGELOG (budget 4000, query cache off, one process per call, alternating, median of 9).
6. **Old-release comparisons.** Several tasks compare with 0.12.1. Make a worktree once:
   `git worktree add /tmp/cortex-0121 0798de8`, and run old code as
   `PYTHONPATH=/tmp/cortex-0121/src python ...` (the editable install is overridden by `PYTHONPATH`).
7. **Failing test first.** For every defect task, add the regression test in the same commit as
   the fix, but write and run it against the unfixed code first and paste the failure into the
   commit body.
8. Keep changes minimal and in the surrounding style (module-level constants with an explanatory
   comment, long comments only where the code is non-obvious). Add a `CHANGELOG.md` bullet under
   `## Unreleased` for every task that changes behaviour, output shape or speed.
9. Do not put any model name or session link in commits, code or docs.

---

## 1. Verdict on the review

Every claim was checked against `f476f21`. Line numbers in the review match the code (±4 lines).
Reproductions were run on hand-written mini-repositories and on the test fixtures
(`tests/test_risk_truncate.py::_large_diff_result`, `tests/fixtures/risk/synthetic_head30.json.gz`).

| § | Claim | Verdict | Evidence / correction |
|---|---|---|---|
| A | `Ns::f()` to a namespace split over `.h`/`.cpp` regressed to `name:` | **Valid, and wider than stated** | Reproduced: `lib/ns.h` (`namespace Ns { int f(); }`), `lib/ns.cpp` (`namespace Ns { int f() {…} }`), `app/caller.cpp` calling `Ns::f()`: 0.13.0 → `name:f`, 0.12.1 → `symbol:lib/ns.cpp:f` (`scoped`). It fails **even when the include resolves to `file:`**, not only when it stays `module:`: the header's implementation sibling (`impl_siblings`) puts both namespace nodes in `near`, and `locality()` step 3 then keeps only the header's. Header prototypes produce no symbol node (confirmed), so the header namespace has no members. Block-nested `namespace A { namespace B {…} }` regressed the same way (0.12.1 resolves `A::B::f()`, 0.13.0 does not). |
| A fix 1 | Index namespace members under `qualified_defs[ns_label]` | **Change it** | `qualified_defs` is keyed by the *last* label segment and is also the fallback of `_members()` for any class lookup, so every `detail::f` / `impl::f` of unrelated namespaces would merge into one ambiguous candidate list, and a class that shares a label with a namespace would pick up its free functions. Use a namespace-only member lookup instead (Task 1.1). |
| A fix 2 | Skip `locality()` steps 3–4 when all candidates are namespaces | **Valid**, adopted inside Task 1.1 | |
| A fix 3, invariant | `scoped-namespace` audit bucket; frozen "no `symbol:` → `name:`" list | **Valid** | The frozen list can be generated now from the 0.12.1 worktree (Task 0.1). |
| — | *(new)* `namespace A::B {` is labelled `A` by tree-sitter | **New defect** | `_name_for_node` returns the first identifier of the `nested_namespace_specifier`. `A::B::f()` looks up label `B` and never finds it. The regex backend already labels it `B`. Fixed in Task 1.2. |
| B | `cortex_risk` returns fewer `files` at a larger budget | **Valid** | Reproduced on synthetic fixtures with a budget sweep (500–20000, step 250): non-monotone `len(files)` on all three fixtures (small range: 17 compact files at 3000, 11 detailed at 4000; bench `HEAD~30`: 35 at 3250, 10 at 3500). Root cause as stated (cut to floor first, detail compaction conditional, refill last). |
| B fix 2 | Refill `files` before `directives` and `tests_to_run` | **Conflicts with the review's own acceptance** | On a large range `files` has hundreds of entries; if it outranks `tests_to_run` above the floor, the 100-commit range at 4000 drops from 42 tests to 10, failing "keeps map 39 and 42 tests at 4000". Correct order: tests above floor > files above floor > directives (Task 2.1). A prototype of that order (move the floor cut after `directives`, drop the end refill, seed `returned_count`) removes every `files` violation on all three fixtures and keeps tests/map unchanged at 2000 and 4000. |
| B (extra) | — | **Two details the review missed** | (1) `returned_count` only enters the payload inside `files_probe`; once `files` is no longer cut early, it is first added after the last fit check and makes a result that fit infeasible (seen at budget 1000 in the prototype). Seed it before any fit. (2) A remaining `map` dip (10 → 9 between budgets 750 and 1000 on `_large_diff_result`) is the existing "below the tests floor the map is fitted first" flip; it is by design and the invariant test must exclude that transition (Task 2.1). |
| C | Two false `tests_by_changed_file` mappings via "production over mock" | **Plausible; root cause cannot be checked here** (the build data is in the private repository), mechanism confirmed in code | In 0.12.1 the ambiguous `new StepA` edge stayed `name:` and was not walked; 0.13.0 resolves it through `locality()` step 2 and `_structural` walks it. Fix 3 (do not walk an edge whose answer came only from step 2) restores 0.12.1 parity and is cheap. Fixes 1–2 (CMake include directories per test target) are a new feature with its own incremental-refresh questions: **deferred** (§4). |
| D.1 | ~95% of unresolved `inherits` are out-of-repo bases | **Valid as accounting** | There is no C++ `external:` target today: the `calls.external` bucket in `cortex_status` is only QML's `external:qml:` targets. Rewriting targets to `external:` would change every consumer that reads `name:` targets (`deadcode._base_nodes`, `impacted_tests` label attach, `_is_resolvable_inherits`, `cortex_relations`). Record the classification in edge metadata and count it in SQL instead (Task 3.1). |
| D.2 | Resolve quoted includes relative to the includer first | **Valid** | `resolve_local_import` has no includer argument; exact path, then unique basename. Note it is the compiler rule only for the includer's own directory; parents are a heuristic for `-I` roots. An incremental refresh does not re-resolve includes of unchanged files (already true for basename uniqueness), so a full index and an incremental one can differ after a file is added; the task pins that. |
| D.3 | `locality()` step 4: longest common directory prefix | **Valid** | |
| E.1 | ≥46k of 100.6k `name:` calls are out-of-repo | **Valid as accounting** | Same approach as D.1 (Task 3.2). |
| E.2 | Anonymous namespace labelled after the first identifier inside | **Valid, and worse than stated** | Reproduced: `namespace { QString helper(); }` → `class QString` node. Worse: `namespace { int helper() {…} } … helper();` → the namespace becomes `class helper`, and the real function `helper` is **dropped** (`seen_symbols` deduplicates by name), so the call resolves to the class node. Every `.cpp` whose anonymous namespace starts with a function loses that function. |
| F | T1–T4 recall 0; hubs win | **Root causes valid; one expectation wrong** | All cited lines check out. But fix 1 alone (proportional name bonus) cannot lift T2: the hub's *text* score is 503 vs the gold file's 212, a gap of 291 that no change to a 100-point bonus closes. Fixes 1 and 2 (length normalisation) only work together; fix 2 is the main lever. "Scale callee scores by relevance (0.25–1)" was tried in 0.13.0 and reverted (companion fixture recall 1.0 → 0.67); use relevance as a tie-break and a small multiplier instead (Task 5.2). "Map `new X` to the `.cpp` constructor" in the resolver would change `dead_code` (the class would lose its `new` callers); do it at query time only (Task 5.4). "Drop split parts shorter than 3 characters" would kill real 2-letter path terms (`ui`, `io`, `db`); handle the `iOS`/`re-` cases specifically (Task 5.5). |
| G | Smaller items | **Valid** | |
| H | Field-shape additions | **Valid** | Added per task, next to the fix that needs them. |

---

## 2. Phases

| Phase | Tasks | Risk | Format/goldens |
|---|---|---|---|
| 0 Harness | 0.1 frozen call-target invariant | none | new golden file |
| 1 Regressions | 1.1 namespace lookup, 1.2 namespace extraction, 1.3 impacted-tests parity, 1.4 audit buckets | low | format 6 → 7 in 1.2 |
| 2 `cortex_risk` | 2.1 truncation order + monotonicity | low | `cortex_risk` shape goldens may change |
| 3 Accounting | 3.1 external `inherits`, 3.2 external `calls`, 3.3 status/gates | low | `cortex_status` goldens |
| 4 Resolver | 4.1 relative includes, 4.2 nearest-directory locality | medium | graph goldens |
| 5 Query ranking | 5.0 fixtures, 5.1–5.9 | **high**, eval-gated one task at a time | eval goldens |
| 6 Smaller items | 6.1–6.3 | low | MCP shape goldens |
| 7 Release | 7.1 | — | version goldens |

Phases 1–4 are independent of Phase 5. If Phase 5 cannot pass its gate in time, ship 0.14.0 with
Phases 0–4 and 6, and move the remaining Phase 5 tasks to 0.15 (say so in the CHANGELOG).

---

## Phase 0 — Harness

### Task 0.1 — Frozen call-target invariant (§A "invariant test", §H.2)

- **Files:** new `tests/fixtures/calls_invariant/` (small C++ repos), new
  `tests/goldens/calls/invariant_0121.json`, new `tests/test_call_target_invariant.py`,
  new `scripts/perf/freeze_call_targets.py`.
- **Fixtures** (one directory each, plain source files, no git needed):
  1. `split_namespace/`: `lib/ns.h` = `namespace Ns {\nint f();\nint g();\n}`;
     `lib/ns.cpp` = `#include "ns.h"\nnamespace Ns {\nint f() { return 1; }\nint g() { return f(); }\n}`;
     `app/caller.cpp` = `#include "ns.h"\nint main() { return Ns::f() + Ns::g(); }`.
  2. `split_namespace_dup_basename/`: fixture 1 plus `other/ns.h` = `namespace Other {}` (the include stays `module:`).
  3. `split_namespace_indirect/`: fixture 1, but `app/caller.cpp` includes `app/facade.h`, which includes `ns.h`.
  4. `nested_namespace/`: `lib/ab.h` = `namespace A { namespace B { int f(); } }`; `lib/ab.cpp` = `namespace A::B {\nint f() { return 1; }\n}`; `app/c.cpp` includes `"ab.h"` and calls `A::B::f()`.
  4b. `nested_namespace_blocks/`: as 4, but `lib/ab.cpp` = `namespace A {\nnamespace B {\nint f() { return 1; }\n}\n}`
     (resolves in 0.12.1, `name:f` in 0.13.0: the same regression as fixture 1).
  5. Copy (do not move) the C++ files the existing call tests already use for `scoped`, `this`,
     `declared_type`, `inherited`, `same_file`, `import_scoped` and `repo_unique` resolution
     (`tests/test_calls_cpp.py` builds them inline: write them out as files here).
- **Script** `freeze_call_targets.py FIXTURE_DIR... --out JSON`: for each fixture, run
  `structural.extract_structural_edges` over every file (plus a `file:` node per file, as
  `graph.build_file_layer` does), then `callresolve.resolve_call_endpoints`, and write
  `{fixture: {"<source>|<lineno>|<callee>": "<target>"}}` for every contract call edge, sorted.
  Run it with 0.12.1 (`PYTHONPATH=/tmp/cortex-0121/src`) to produce `invariant_0121.json`.
  Check that fixtures 1–3 have `symbol:lib/ns.cpp:f` and 4b `symbol:lib/ab.cpp:f` there. (Fixture 4
  is `name:` in 0.12.1 too: that is the new nested-namespace label defect, Task 1.2; record it in
  an `"expected_fixed"` list in the test, not in the frozen file.)
- **Test:** for every frozen edge whose 0.12.1 target is `symbol:`, the current target must also be
  `symbol:` — the same id, or a different id only when the 0.12.1 target is an auxiliary path
  (`callresolve._is_auxiliary`) and the new one is not (the intended mock → production change).
  `name:` → `symbol:` changes are allowed. Edges in `expected_fixed` must be `symbol:` once their
  task lands. Mark the test `xfail(strict=True)` for fixtures 1–4b in this commit; Task 1.1
  (1–3, 4b) and Task 1.2 (4) remove the marks.
- **Acceptance:** the test runs against `f476f21` and fails exactly on fixtures 1–4b.

---

## Phase 1 — Regressions and extractor bugs

### Task 1.1 — Namespace-scoped calls look up members across every namespace node (§A)

- **Files:** `src/cortex/callresolve.py`, `tests/test_call_resolution.py` (or
  `tests/test_calls_cpp.py`, whichever holds the C++ scoped-call cases), `tests/test_deadcode.py`,
  `tests/test_call_target_invariant.py`.
- **Change:**
  1. Add `_Index.namespace_members(label, callee, source_file) -> list[str]`:
     - `pool` = ids in `classes_by_label[label]` for which `is_namespace()` holds (the index is
       already one language family);
     - if `from_path` is not auxiliary, drop auxiliary namespaces from `pool` when that leaves any
       (same rule as `locality()` step 2);
     - `members` = sorted union of `class_members(ns, callee)` over `pool` (this already includes
       the `qualified_defs[label][callee]` fallback for out-of-line `int Ns::f() {}` definitions);
     - if `len(members) > 1`: `members = self.prefer(members, source_file)` (same file, then included
       files and their implementation siblings, then `locality()`);
     - return `members`.
  2. In `_resolve_scoped`, in the final `label` branch: if `classes_by_label[label]` is non-empty
     and **every** id in it is a namespace, return `("scoped", found)` from `namespace_members`
     (or `None` when empty) instead of `prefer` + `_members`. Classes keep the current path.
  3. In `locality()`, skip steps 3 and 4 when every candidate in `pool` is a namespace (defence in
     depth for any other caller of `prefer` with namespace candidates). Update the docstring.
  4. Do **not** change `ClassIndex.qualified_defs` (see §1, A fix 1).
- **Tests:**
  - The review's layout, asserting `symbol:lib/ns.cpp:f` with `resolution: scoped`: include
    resolved; include indirect; duplicate `ns.h` basename (include stays `module:`); a mock copy of
    the namespace under `tests/mocks/` defining `f` too (production caller → production `f`); two
    unrelated namespaces with the same label in different directories each defining `f` with the
    caller including one of them (→ the included one; with neither included → ambiguous, two
    candidates).
  - A class that shares its label with a namespace keeps class-member resolution (`Foo::m()` where
    `Foo` is a class → the class member, not a namespace function).
  - `dead_code` on the split-namespace fixture does not report `Ns::f`.
  - `namespace A { namespace B { … } }` blocks split over `.h`/`.cpp`, called as `A::B::f()`.
  - Remove the `xfail` marks of fixtures 1–3 and 4b in `test_call_target_invariant.py`.
- **Prototype evidence** (scratch, not committed): steps 1–2 alone turn every case above into the
  expected target — split namespace with the include resolved, with a duplicated basename, with a
  mock copy under `tests/mocks/` (0.12.1 left that one ambiguous), block-nested `A::B` — while two
  unrelated `Ns` with no include stay ambiguous and `Foo::m()` on a class is unchanged.
- **Acceptance:** on the `--field-shape` repo and on `evals/fixtures/oss/cpp_qt_sortfilterproxymodel`,
  run `scripts/perf/audit_unresolved_calls.py` before and after: no call edge goes from `symbol:` to
  `name:`; paste both ratio lines into the commit body. No golden changes (if one does, explain it
  in the CHANGELOG: it can only be a `name:` → `symbol:` move of a namespace call).

### Task 1.2 — Anonymous and nested namespaces in the tree-sitter extractor (§E.2, new defect)

- **Files:** `src/cortex/structural/treesitter_backend.py`, `src/cortex/store.py`
  (`GRAPH_FORMAT_VERSION`), `tests/test_calls_cpp.py`, `tests/test_golden_extraction.py` (only if a
  fixture needs adding), `tests/test_call_target_invariant.py`, `tests/test_graph_format_upgrade.py`
  (if it pins the number).
- **Change:**
  1. In the definition loop of `extract_treesitter_edges`, for `node.type == "namespace_definition"`:
     - if `node.child_by_field_name("name")` is `None` (anonymous namespace): emit **no** symbol
       node and do not add anything to `seen_symbols`; the loop continues into its children as
       today (`_iter_nodes` visits them regardless), so the functions inside become ordinary
       file-level symbols. Do not touch `_name_for_node`'s generic fallback (other grammars rely on
       it).
     - if the name child is a `nested_namespace_specifier` (`namespace A::B {`): use the text of
       its **last** `namespace_identifier` as the label, so the node is labelled `B` like the regex
       backend's `name.split("::")[-1]`. Keep the symbol id `symbol:<path>:B`.
  2. `GRAPH_FORMAT_VERSION` 6 → 7 with a comment line `7 = 0.14.0: anonymous namespaces emit no
     class node, nested namespaces are labelled by their last segment; namespace-scoped call
     resolution` (the Task 1.1 resolver change also needs the rebuild: an incremental refresh only
     re-resolves affected edges, so an upgraded 0.13 index would keep the wrong `name:` targets).
- **Tests:**
  - `namespace { QString f(); }` and `namespace { QString f() { return {}; } }` produce no node
    labelled `QString`; the second produces `func f`.
  - `namespace { int helper() { return 1; } }\nint top() { return helper(); }` produces
    `func helper` and the call resolves to `symbol:<file>:helper` with kind `func` (today it ends at
    a class node).
  - `namespace A::B { int f() { return 1; } }` → a class node labelled `B`; remove the `xfail` of
    invariant fixture 4.
  - Golden parity: for one fixture with an anonymous namespace, a named namespace and a nested
    `A::B` namespace, the set of `(kind, label)` class nodes is identical under tree-sitter and
    `CORTEX_FORCE_REGEX=1`.
  - Add to `--field-shape` (`scripts/perf/synthetic_repo.py`, `write_field_shape`): an anonymous
    namespace at the top of one in three derived `.cpp` files, opening with a `QString` helper;
    pin in `tests/test_synthetic_field_shape.py` that the index has 0 class nodes labelled
    `QString`/`QStringList`/`QMap`. Keep every other profile byte-identical.
- **Goldens expected to change:** any graph golden whose fixture has an anonymous or nested
  namespace (check `tests/goldens/graph/*.json`); name each in the CHANGELOG.
- **Acceptance:** suite green in both backends; on the `--field-shape` repo, 0 class nodes named
  after a Qt/std type.

### Task 1.3 — `impacted_tests` does not walk calls resolved only by "production over mock" (§C fix 3)

- **Files:** `src/cortex/callresolve.py`, `src/cortex/impacted_tests.py`, `tests/test_impacted_tests.py`.
- **Change:**
  1. Record when `locality()` step 2 decided an edge. Give `_Index` a per-edge trace:
     `self.trace: set[str] | None = None`. In `locality()`, when step 2 is active and *narrowed*
     the pool (removed at least one auxiliary candidate), do `self.trace.add("production")` if
     `self.trace is not None`. In `_resolve_into`, set `index.trace = set()` before `_resolve_one`
     and, when the edge resolves to exactly one candidate and `"production" in index.trace`, write
     `edge.metadata["preferred"] = "production"`. Reset `index.trace = None` afterwards.
     Add `"preferred"` to `_RESOLUTION_KEYS` so `_reset` clears it.
  2. In `impacted_tests._structural`, skip a reverse `calls` edge whose
     `metadata.get("preferred") == "production"` (treat it like the unresolved `name:` edge it was in
     0.12.1). Other consumers (call graph, `dead_code`, `cortex_query`) keep using the resolved target.
  3. Inheritance is unaffected: `impacted_tests` already attaches rewritten `inherits` edges by label.
- **Test** (the review's §C layout): `src/ops.cpp` calls `new Step(...)`; `src/steps/step.h` and
  `tests/header-mocks/steps/step.h` both define `class Step`; `tests/opstest.cpp` calls a function of
  `ops.cpp`; `tests/CMakeLists.txt` lists both. A change to `src/steps/step.h` does **not** map to
  `tests/opstest.cpp`; a change to `src/ops.cpp` does. Run it against 0.13.0 first (it maps).
  Also assert the `calls` edge still targets the real `Step` (the graph keeps the production answer).
- **Acceptance:** on the `--field-shape` repo, `tests_to_run`, `tests_by_changed_file` and
  `tests_run_all` for `HEAD~3`, `HEAD~10`, `HEAD~30`, `HEAD~100` are identical to 0.12.1's (the
  0.13.0 CHANGELOG claimed this; now it is tested). Write that as a test only if it runs in under
  30 s; otherwise paste the comparison into the commit body.

### Task 1.4 — Audit buckets for the remaining misses (§A fix 3, §E fix 3)

- **Files:** `scripts/perf/audit_unresolved_calls.py`, its test if one exists (grep `audit_unresolved`
  in `tests/`).
- **Change:** add buckets, checked before the existing ones in this order:
  `scoped_namespace` (scope label names only namespace nodes in the repository), `external`
  (the edge carries `metadata.external`, filled by Task 3.2; until then the bucket is empty),
  `not_cpp_<family>` (caller outside the C family: report JS/Java/Python separately). Keep `chain`.
  Add `--all` to bucket every unresolved edge instead of a sample (the reviewer's exact counts).
- **Acceptance:** documented in the module docstring; `--all` on the `--field-shape` repo prints
  `scoped_namespace 0` after Task 1.1.

---

## Phase 2 — `cortex_risk`

### Task 2.1 — Truncation order: count before detail, strict priority, monotone in budget (§B)

- **Files:** `src/cortex/risk.py` (`truncate_risk_result`, comments on `FILES_FLOOR`,
  `_RISK_DROP_ORDER`), `tests/test_risk_truncate.py`, `tests/test_risk_shape.py`, `CHANGELOG.md`.
- **New give-up order** (each step only while still over budget; steps 0 and 1 unchanged):
  0. `uncovered_changed_files` → `UNCOVERED_FLOOR`;
  1. map re-encoded as `test_table` (lossless);
  2. the secondary buckets in `_RISK_DROP_ORDER`, one at a time;
  3. `tests_to_run.reason`;
  4. `files.detail` (compact every entry);
  5. `directives` (longest fitting prefix, down to none);
  6. **`files` down to `FILES_FLOOR`** (moved here from before step 2);
  7. `tests_to_run` down to `TESTS_TO_RUN_FLOOR`;
  8. `uncovered_changed_files` below its floor;
  9. the map's per-file caps, then its changed files (unchanged `shorten_map`);
  10. `tests_to_run` below its floor, map fitted first (unchanged block);
  11. `files` below `FILES_FLOOR`;
  12. the map whole.

  Equivalently, from most to least protected above the floors: map > `tests_to_run` > `files` count
  > `directives` > `files.detail` > reasons > buckets. That is the decision for §B fix 2 (see §1:
  `files` cannot outrank `tests_to_run` without breaking the 42-tests acceptance). If the owner
  prefers directives over extra files, swap steps 5 and 6 — both orders are monotone; nothing else
  changes.
- **Code changes:**
  1. Move `if over(): shorten("files", files_probe, floor=FILES_FLOOR)` to right after the
     `shorten("directives")` block.
  2. Delete the end-of-function refill (`kept_files = …; if not over() and kept_files < len(all_files): …`).
     With greedy longest-prefix cuts in priority order it is unnecessary, and it is what makes
     `files` non-monotone: it hands `files` the crumbs left after a higher-priority key was cut,
     and the crumbs shrink as that key grows.
  3. Right after `all_files = list(output.get("files") or [])`, set
     `output["returned_count"] = len(all_files)`, so the key is part of every fit (today it is first
     added inside `files_probe`; without the early cut it would be added after the last fit check
     and push a fitting result over budget — observed at budget 1000 on the small-range fixture).
  4. Rewrite the docstring's numbered list to the order above and delete "Whatever room is left …
     goes back to `files`". Update the `FILES_FLOOR` comment ("cut to this many entries after
     `directives` and before `tests_to_run`; below it only once everything else is at its floor").
- **Tests** (`tests/test_risk_truncate.py`):
  1. New fixture `_small_range_result()`: 17 changed files, each with 4 `missing_cochange`
     entries and 7 `components`; 20 `missing_qt_pairs` with 30-character reasons; 30 directives;
     a small map. Detail must cost more than one compact row (assert it: compact `files` < 0.5 ×
     detailed `files` in tokens). On 0.13.0 it gives 17 files at 3000 and 11 at 4000.
  2. `test_truncation_is_monotone_in_budget` over `_small_range_result()`, `_large_diff_result()` and
     `_bench_result()`, `budget in range(500, 20001, 250)`. Helper `_shape(out)` returns
     `files = len(files)`, `mapped = len(tests_by_changed_file)`, `cap` (`inf` when
     `tests_by_changed_file_more` is absent, else the largest kept per-file count), `tests =
     len(tests_to_run)`, `compact = "files.detail" in omitted_fields`. For consecutive budgets
     b1 < b2 where neither result has `minimal_payload`, assert `files`, `cap`, `tests` do not
     decrease and `compact(b2) ⇒ compact(b1)`. Assert `mapped` does not decrease **except** across
     the step-10 transition, i.e. when `tests(b1) < TESTS_TO_RUN_FLOOR <= tests(b2)` (below the
     tests floor the map is deliberately fitted first; on `_large_diff_result` this is the 750 → 1000
     step, 10 → 9 entries); there assert `mapped(b2) >= 1`.
  3. Update tests that encode the old order: `test_files_are_a_ranked_prefix_and_given_up_last`
     (comment and the "below the floor only once directives and tests are gone" assertion still
     hold; check), `test_bench_range_*` pins (re-measure with and without `tiktoken`; keep each
     lower bound at the smaller of the two), `DROP_ORDER` in `tests/test_risk_shape.py`. Every
     changed number goes into the commit body with its old value.
  4. `test_every_collection_key_is_reducible` and the `omitted_fields` exactness test must pass
     unchanged.
- **Prototype evidence** (scratch, not committed): code changes 1–3 alone, with no test edits, pass
  all 39 existing tests of `tests/test_risk_truncate.py` and `tests/test_risk_shape.py`, and the
  sweep in test 2 finds 0 `files` violations on all three fixtures (0.13.0: 4, 7 and 5). So step 3
  of the test list is expected to be small; if many existing tests fail, the change is not the one
  described here.
- **Acceptance:**
  - Monotonicity test green with and without `tiktoken`.
  - `_bench_result()` at 2000 and 4000: map and `tests_to_run` counts unchanged from 0.13.0
    (prototype, with `tiktoken` installed: map 98 / 117, tests 10 / 38; re-measure without it);
    `files` at 4000 >= at 2000.
  - On the `--field-shape` repo, for `HEAD~3`, `HEAD~10`, `HEAD~30`, `HEAD~100`: `files` at 4000 >=
    at 2000, and the map and `tests_to_run` at 4000 are what 0.13.0 returns.
  - Field acceptance (§B) is the reviewer's run: list it under "Not verified" until then.

---

## Phase 3 — Honest accounting for out-of-repo targets

### Task 3.1 — Classify out-of-repo `inherits` bases (§D fix 1)

- **Files:** `src/cortex/callresolve.py`, new `src/cortex/externals.py`,
  `tests/test_inherits_resolution.py`.
- **`externals.py`** (pure functions, no graph access):
  - `EXTERNAL_PREFIXES = ("std::", "testing::", "boost::", "Qt::", "QtQuick", "QtQml")`;
  - `QT_CLASS_RE = re.compile(r"^Q[A-Z][A-Za-z0-9]*$")` (`QObject`, `QThread`, `QAbstractListModel`);
  - `GTEST_BASES = {"Test", "TestWithParam"}` (external only when the file includes a `gtest`
    header, see below);
  - `QML_BUILTINS`: the Qt Quick / Controls / Layouts types the review lists plus the common
    rest: `Item, Rectangle, Column, Row, Grid, Flow, QtObject, Text, Image, MouseArea, Loader,
    Repeater, ListView, GridView, Flickable, ColumnLayout, RowLayout, GridLayout, StackLayout,
    Button, Label, TextField, Popup, Dialog, Page, Pane, Control, ApplicationWindow, Window,
    WebEngineView` — keep it a sorted frozenset with a comment that it only applies when no
    repository QML component has the label;
  - `classify_base(label: str, written: str, file_imports: Iterable[str], family: str) -> str | None`
    returns a rule name (`"qt"`, `"std"`, `"gtest"`, `"qml_builtin"`, `"qml_import"`, `"prefix"`)
    or `None`. `written` is the base as the extractor wrote it (`name:testing::Test`), so prefixes
    are checked on it. `"qml_import"`: a QML base written with an import alias (`Controls.TextField`)
    or any QML base when the file imports a module that is not a repository directory.
- **Resolver change** (`_resolve_inherits_into`): when `candidates` is empty — no class with the
  label exists in the edge's language family — call `classify_base` and, when it returns a rule,
  set `edge.metadata["external"] = rule`. Do **not** rewrite the target. Add `"external"` to
  `_RESOLUTION_KEYS` (and confirm `_reset_inherits` clears it through that tuple). The `imports`
  of the edge's file come from `_Index.imports` (resolved) plus the `module:` import targets;
  add a `module_imports: dict[path, list[str]]` to `_Index` built in the same edge loop.
- **Tests:** `class W : public QObject` → `metadata.external == "qt"`, target still `name:QObject`;
  `class T : public ::testing::Test` → `"prefix"`; bare `Test` with `#include <gtest/gtest.h>` →
  `"gtest"`, without it → no `external`; QML root `Item {}` with no `Item.qml` → `"qml_builtin"`;
  with an in-repo `Item.qml` → resolves as today, no `external`; a class deriving from an in-repo
  `QWidgetBase` (matches `QT_CLASS_RE` but exists in the repo) → resolves to `symbol:`, no
  `external`. Incremental: adding a repository class `QObject` (the old fake-node case) re-resolves
  the edge (`_inherits_affected` already fires on the label) and drops `external`.
- **Field-shape:** add derived classes so about 25% of all classes derive only from an external base
  (`QObject`, `QThread`, `testing::Test`); pin the share in `tests/test_synthetic_field_shape.py`.

### Task 3.2 — Classify out-of-repo `calls` (§E fix 1)

- **Files:** `src/cortex/externals.py`, `src/cortex/callresolve.py`, `tests/test_calls_cpp.py`.
- **Rules** (`classify_call(md: dict, family: str) -> str | None`, C family only; return `None`
  for other families — they are reported per language in Task 3.3):
  - `scope` or `receiver_type` starts with an `EXTERNAL_PREFIXES` entry or matches `QT_CLASS_RE`
    (`QString::number`, `Qt::red`, `m_list->append()` with `receiver_type = QList<int>`) → `"qt"`/`"std"`;
  - `receiver_kind == "new"` or a free call whose callee matches `QT_CLASS_RE` (`QVariantMap(...)`) → `"qt"`;
  - free callee matching `^q[A-Z]\w*$` or `^Q_[A-Z_]+$` (`qWarning`, `qobject_cast`, `Q_ASSERT`) → `"qt_macro"`;
  - free callee in `LIBC_STD_NAMES` (a sorted frozenset: `printf fprintf snprintf sprintf malloc calloc
    realloc free memcpy memmove memset memcmp strlen strcmp strncmp strcpy strncpy strcat strstr
    strchr atoi atol strtol strtoul strtod abs fabs sqrt pow floor ceil exit abort assert getenv
    fopen fclose fread fwrite fflush close open read write select poll usleep sleep time clock
    move forward swap make_shared make_unique get tie min max`) → `"libc"`.
- **Resolver change:** in `_resolve_into`, when `outcome is None` (no candidate at all — never for
  ambiguous edges), set `edge.metadata["external"] = rule` from `classify_call`. Only when the
  callee has no definition anywhere in the family (`free_by_label`, `classes_by_label`,
  `qualified_defs` all miss it): a repository function named `min` or `close` must never be marked
  external.
- **Tests:** one call per rule ends `name:` with the right `external`; a repository function
  `close()` called unqualified keeps `same_file`/`repo_unique` resolution; a call to an undefined
  `myHelper()` gets no `external`.

### Task 3.3 — `cortex_status` resolution buckets and the gate definitions (§D, §E, §H.3)

- **Files:** `src/cortex/store.py` (`resolution_counts`), `src/cortex/mcp/tools.py` (status schema
  text, if it documents the keys), `tests/test_mcp_status.py`, goldens
  `tests/goldens/mcp/shapes/cortex_status.detailed.json` and `tests/goldens/mcp/tools_list.json`
  (if the schema text changes).
- **Change:** keep the existing counts and add, in the same SQL pass (a second
  `GROUP BY` on `json_extract(metadata_json, '$.external') IS NOT NULL`):
  ```json
  "resolution": {
    "calls":    {"symbol": 41928, "name": 100603, "external": 11726,
                 "name_external": 46000, "in_repo_symbol_share": 0.42},
    "inherits": {"symbol": 888, "name": 587, "module": 440,
                 "name_external": 980, "in_repo_symbol_share": 0.95}
  }
  ```
  `name_external` counts `name:`/`module:` edges carrying `metadata.external` (they are still inside
  `name`/`module`, so existing readers keep working). `in_repo_symbol_share =
  symbol / (symbol + name + module − name_external)`, rounded to 4 places, `null` when the
  denominator is 0. For `calls` also add `name_by_family: {"c": …, "js": …, "java": …, "py": …}`
  computed from `json_extract(metadata_json, '$.source_file')` suffix in SQL — if that makes
  `cortex_status` detailed slower than 1 s on the `--modules 800` perf repo, leave `name_by_family`
  to `audit_unresolved_calls.py` instead and say so.
- **CHANGELOG:** state the gates as "in-repo `inherits` resolved to `symbol:` >= 80%
  (`in_repo_symbol_share`, external bases excluded from the denominator)" and "in-repo `calls`
  >= 60%" — named exactly like that, per §H.3.
- **Acceptance:** on the `--field-shape` repo `inherits.in_repo_symbol_share` >= 0.95; the reviewer's
  numbers stay "not verified".

---

## Phase 4 — Resolver fixes for duplicated basenames

### Task 4.1 — Quoted includes resolve relative to the including file first (§D fix 2)

- **Files:** `src/cortex/structural/regex_backend.py` (`resolve_local_import`),
  `src/cortex/structural/treesitter_backend.py` (call site), `tests/test_resolve_local_import_index.py`,
  `tests/test_ingest_incremental.py`.
- **Change:** `resolve_local_import(target, known_paths, from_path: str | None = None)`. When
  `from_path` is given and the include is quoted (the C/C++ call sites know this from the
  `preproc_include` node: `string_literal` vs `system_lib_string`; pass `from_path` only for the
  quoted form), try in order: `<dir of from_path>/<target>`, then `<each ancestor dir>/<target>`
  from nearest to the repository root, then the existing exact match and unique-basename match.
  Normalise `..` segments with `posixpath.normpath` and reject results that escape the root. Leave
  QML and other callers unchanged (no `from_path`).
- **Tests:** two `stepbase.h` (`src/steps/stepbase.h`, `tests/header-mocks/stepbase.h`);
  `tests/header-mocks/steps/stepa.h` includes `"stepbase.h"` → `file:tests/header-mocks/stepbase.h`;
  `src/steps/stepa.h` includes `"stepbase.h"` → `file:src/steps/stepbase.h`; `#include "steps/stepbase.h"`
  from `src/app/x.cpp` → `file:src/steps/stepbase.h` (ancestor rule); `#include <stepbase.h>` keeps
  the old behaviour (`module:`). Incremental: index, then add `src/app/stepbase.h`, refresh
  incrementally: document in the test that an unchanged includer keeps its old resolution until it
  is re-extracted (the same limitation as basename uniqueness today), and assert a full ingest
  gives the new one. Bump nothing (`GRAPH_FORMAT_VERSION` 7 from Task 1.2 covers it if this lands
  in the same release; if 4.1 ships separately, bump again).
- **Goldens expected to change:** graph goldens with a quoted include whose basename is duplicated
  (`imports` edge `module:` → `file:`).

### Task 4.2 — `locality()` step 4: nearest directory (§D fix 3)

- **Files:** `src/cortex/callresolve.py`, `tests/test_inherits_resolution.py`.
- **Change:** step 4 keeps the candidates whose directory shares the **longest** common path prefix
  (in segments) with `from_path`'s directory, instead of exactly the same directory. Same
  "only if it leaves one" rule.
- **Tests:** the §D shape: caller `tests/header-mocks/steps/stepa.h`, candidates
  `tests/header-mocks/stepbase.h` and `src/stepbase.h`, quoted include whose basename is duplicated
  and (to isolate this task from 4.1) an include that stays `module:` → the mock base. Existing
  locality tests unchanged.
- **Field-shape:** one mock tree whose mock base sits one directory above the mocks that use it;
  pin that their `inherits` edges resolve to it.
- **Acceptance:** `--field-shape` `inherits` ambiguous count does not increase; the new mock tree
  resolves 100%.

---

## Phase 5 — Query ranking (§F)

Every task in this phase changes ranking, runs the eval gate (rule 4) and the five synthetic tasks of
Task 5.0, and is its own commit. Order is by risk, lowest first. **Stop rule:** if a task fails the
eval gate after two honest tuning attempts within its stated parameter range, revert it, record the
numbers under "Not met", and continue with the next task. Do not retune an earlier task to make a
later one pass.

### Task 5.0 — Synthetic query fixtures (§F "Synthetic fixtures")

- **Files:** `evals/run_evals.py` (a new `FIELD_GOLD_TASKS` tuple, kept out of `GOLD_TASKS` like the
  QML/spread/companion tasks, run in all four eval modes), `tests/test_bundle_field_tasks.py`.
- Build the five repositories with `_write`/`_commit_all` exactly as the review describes:
  hub flood (four `core*.cpp` of ~3k lines whose comments use every common English word and contain
  hundreds of `deviceX()` methods, plus `gadgetwaitchargemax.cpp`; task "slow when the charge max
  wait setting is on"; gold `gadgetwaitchargemax.cpp` at budgets 900 and 4000), tie-break (30
  cross-file one-line virtuals under `a/…`, one relevant callee in `z/ops/resetop.cpp`), override
  chain (T1 shape; gold `secondmanager.cpp` and `accessory.cpp` with `why: override` then `calls`),
  constructor flood (300-line constructor calling 100 resolved helpers; no helper without a task
  term packed), vocabulary ("Gizmos replug removed on iOS re-enumerates"; no `os`/`re` term,
  `gizmo` matches).
- Generate the hub text deterministically (fixed word list, fixed seed); keep the whole fixture set
  under 2 MB on disk and its eval run under 60 s.
- Mark each test `xfail(strict=True)` with the task that should fix it:
  hub flood → 5.7/5.8/5.9, tie-break → 5.1, override chain → 5.3, constructor flood → 5.4,
  vocabulary → 5.5.
- **Acceptance:** all five fail on `f476f21` for the reasons the review gives (assert on the
  failure, e.g. the hub files fill the bundle).

### Task 5.1 — Callee ties break on relevance, then hop, then path (§F cause 4)

- **Files:** `src/cortex/bundle.py`.
- `_callee_scores` returns `(score, seed, hop)`; extend it to `(score, seed, hop, relevance)` and
  store `relevance` in the item's `call_expansion` route. In the final candidate sort, replace
  `(-item.score, item.path, item.item_id)` by
  `(-item.score, -relevance(item), hop(item), item.path, item.item_id)`, where `relevance` and `hop`
  default to `0` and `0` for items without `call_expansion`. Ties only: no score changes.
- Remove the tie-break `xfail`. Eval gate.

### Task 5.2 — Relevance as a small score factor and IDF-weighted (§F fix 4, first bullet)

- **Files:** `src/cortex/bundle.py`.
- Change `relevance()` to return IDF-weighted relevance: name hit → `1.0`; body hit →
  `min(1.0, Σ w(t in body) / max_w)` floored at `RELEVANCE_FILE_HIT`; file-only → `RELEVANCE_FILE_HIT`;
  else `RELEVANCE_FLOOR`. Then scale the callee score by `CALLEE_RELEVANCE_MIN + (1 −
  CALLEE_RELEVANCE_MIN) × relevance` with `CALLEE_RELEVANCE_MIN = 0.85` (a callee that shares no
  word still keeps >= 85% of its share; 0.13.0's 0.25–1 range lost the companion fixture).
  Tuning range: 0.75–0.95. Keep the "one-line virtuals at the floor" behaviour unless the name
  carries a task term (§F fix 4, last bullet): change the trivial branch to return `0.0` for a
  one-line virtual with overriders **only when** Task 5.3 is in (overriders are then walked from
  the base method itself), otherwise leave it.
- Eval gate; companion fixture recall must stay 1.0.

### Task 5.3 — Overriders are walked (§F cause 6, fix 5)

- **Files:** `src/cortex/bundle.py` (`_callee_scores`, `_select_seeds`, `SEED_FILES`).
- In the BFS, after `offer(overrider, …)`, also `queue.append((overrider, hop))` when the overrider
  is not settled (same hop as the base method: the virtual call is one call). The overrider's own
  callees then follow the normal hop budget.
- `SEED_FILES` 3 → 5 (the best function span of each of the top 5 files is a seed; `CALLEE_SEEDS`
  stays 6, so raise it to 8 only if the override-chain task still fails, and say so).
- Remove the override-chain `xfail`. Eval gate. Measure `_call_graph_links` + walk time on the
  `--modules 800` perf repo; cold query time within +20% of 0.13.0.

### Task 5.4 — No constructor seeds; `new X` reaches the constructor at query time (§F cause 5)

- **Files:** `src/cortex/bundle.py`.
- A symbol is a constructor when it is a function whose label equals its class's label (its
  `class_of()` holder or its `qualifier`). `_select_seeds` skips constructors like class nodes.
- In `_call_graph_links`, build `constructors_of: class id -> constructor ids` (same-label functions
  qualified by or nested in the class; the class's own `.cpp` siblings via the qualifier). In the
  walk, when a callee is a class node, enqueue its constructors at the same hop instead (keep the
  class itself as a candidate only through `bases_of`, as today). Do **not** change
  `callresolve._resolve_new` (that would move `dead_code` and impact).
- Remove the constructor-flood `xfail`. Eval gate.

### Task 5.5 — Tokeniser fixes (§F cause 8, fix 6)

- **Files:** `src/cortex/bundle.py` (`_split_identifier`, `STOPWORDS`, `_tokenize_query`),
  `tests/test_bundle_v2.py` or the tokenizer tests that exist for `_tokenize_text`.
- Changes, each with a unit test:
  1. A token of the form lowercase-prefix + uppercase run of >= 2 letters (`iOS`, `macOS`,
     `tvOS`) is kept whole (`ios`, `macos`) and not split. Implement in `_split_identifier` before
     the camel split.
  2. Add the instruction words to `STOPWORDS`: `find then very all after because every too shows
     show come back during handles handle path re`. (`re` covers `re-enumerates`.) Only for the
     query side (`drop_stopwords=True`), as today.
  3. Light stemming **for matching only**: a `_stem(term)` that strips `ies→y`, `es` (after s, x,
     z, ch, sh), `s` (not after s/u/i), `ed`, `ing` when the remainder is >= 4 letters. Apply it to
     both sides where term sets are intersected (`task_terms`, file/span/name token sets) by
     building stemmed sets alongside the raw ones; do not change `_tokenize_text`'s output, which
     path aliases and FTS also use. Weights (`_term_weights`) are computed on the stemmed sets.
  4. CamelCase singular: a query part that is a plural of a symbol-label word matches it through
     the stemming above (`GizmoPods` → `gizmo`, `pod`). Concatenated forms (`Gizmopod` vs
     `GizmoPods`) match through the stemmed concatenation of adjacent query parts: add
     `stem(part_i + part_{i+1})` as an extra query term with the weight of its rarer part.
- Remove the vocabulary `xfail`. Eval gate.

### Task 5.6 — `pathalias` splits `_` stems; platform variants are demoted (§F fixes 7, 8)

- **Files:** `src/cortex/pathalias.py`, `src/cortex/bundle.py`, `src/cortex/config.py` (optional map).
- `pathalias`: a stem with `_` is split on `_` first and each part goes through the existing rules
  (`splittable`, `segment_stem`); the aliases are the union. Unit test with
  `wm_devicechange_win.cpp`.
- Platform demotion: `PLATFORM_SUFFIXES = {"win": {"windows", "win", "win32"}, "mac": {"mac",
  "macos", "osx", "darwin"}, "linux": {"linux"}, "android": {"android"}, "ios": {"ios"}}`. A file
  whose stem ends in `_<key>` (or a directory segment equal to a key) is multiplied by
  `PLATFORM_DEMOTION = 0.5` unless a task term is in that key's set; overriders in such files are
  ranked after the others in `overriders()`. Optional `.cortex` config `platform_terms: {product:
  platform}` adds product names to a set (document in `README.md`'s config section).
- The T2 "USB" ↔ OS-API vocabulary gap is **not** fixed in code: document the existing `synonyms`
  config with this example in the CHANGELOG.
- Eval gate.

### Task 5.7 — Length-normalised file text score (§F cause 2, fix 2) — the main lever

- **Files:** `src/cortex/bundle.py` (`_score_text` call for files, `SYMBOL_FILE_SCORE_SHARE` use).
- For **file** candidates only (spans keep `_score_text`), divide the text part of the score by
  `1 + LENGTH_NORM × log2(max(1, tokens(file) / median_tokens))`, where `median_tokens` is the
  median token count of code files that share at least one task term, and `LENGTH_NORM = 0.5`
  (tuning range 0.25–1.0). The name/path bonuses are not divided.
- Cap what a span inherits from its file: `file_share = SYMBOL_FILE_SCORE_SHARE × min(file score,
  SPAN_FILE_SHARE_CAP)` with `SPAN_FILE_SHARE_CAP = 2 × NAME_MATCH_BONUS`.
- Hub-flood fixture: report the gold file's rank before/after in the commit body.
- Eval gate. If it fails within the tuning range, try the alternative (term density: overlap weight
  / `sqrt(distinct tokens)` scaled to the median file) once, then apply the stop rule.

### Task 5.8 — IDF-weighted, proportional name bonus (§F cause 1, fix 1)

- **Files:** `src/cortex/bundle.py` (file `name_bonus`, span `name_bonus`).
- File: `name_bonus = NAME_MATCH_BONUS × (NAME_BONUS_BASE + (1 − NAME_BONUS_BASE) × Σ w(t ∈ stem ∪
  aliases ∩ T) / Σ w(T))` when any stem/alias term matches, with `NAME_BONUS_BASE = 0.5` (tuning
  0.3–0.7) so a single exact stem hit still beats keyword-dense docs (the original reason for the
  bonus). Symbol-name hits on the file: contribute at most `SYMBOL_NAME_FILE_SHARE = 0.5` of the
  same formula (they no longer earn the whole bonus for the file); the span keeps its own full
  bonus.
- Span: same formula over the symbol's label terms.
- Eval gate. Hub-flood gold must now pack at 900 and 4000 (remove its `xfail` if 5.9 is not
  needed).

### Task 5.9 — Spans compete on their own score; the per-file span share scales (§F cause 3, fix 3)

- **Files:** `src/cortex/bundle.py` (`pack`, `FILE_SPAN_SHARE`).
- When a file candidate does not fit whole, stop selecting its spans at the file's score (the loop
  over `symbol_items_by_path[item.path]` in `pack`); its spans are packed only when their own turn
  in `candidates` comes. Keep the skeleton fallback for a file that has no span candidate.
- `file_cap = max(0.1, 1 / n_matching_files) × budget`, where `n_matching_files` counts code files
  with a positive score, capped below by the current 0.25 when fewer than 4 files match.
- Eval gate; remove the hub-flood `xfail`. Acceptance for the phase: on the hub fixture no more than
  25% of the bundle tokens go to files with no IDF-weighted stem or alias hit.

### Phase 5 acceptance

- All five Task 5.0 tasks pass in all four eval modes (or are listed under "Not met" with numbers).
- Eval gate against `f476f21` passes for the phase as a whole.
- Cold query time on the `--modules 200/800` perf repos within +20% of 0.13.0 (procedure rule 5).
- T1–T4 on the field repository: "not verified" until the reviewer's run.

---

## Phase 6 — Smaller items (§G)

### Task 6.1 — `cortex_path` returns the best candidate with alternatives

- **Files:** `src/cortex/pathfind.py`, `src/cortex/mcp/tools.py`, `tests/test_pathfind.py`,
  `tests/goldens/mcp/shapes/cortex_path*.json` (if the shape changes).
- When a bare label matches several symbols, rank them with `callresolve._Index.locality`-style
  rules (production over auxiliary, header over nested-in-`.cpp`, then path) and use the first;
  return `alternatives: [ids…]` (at most 8) and keep `ambiguous: true` so old clients still see it.

### Task 6.2 — `cortex_risk` returns a partial payload when the deadline hits

- **Files:** `src/cortex/risk.py`, `src/cortex/deadline.py` users, `tests/test_risk.py`.
- Compute and keep the ranked `files` and `uncovered_changed_files` before the expensive test
  selection; on `DeadlineExceeded` inside test selection, return them with `partial: true`,
  `tests_run_all: true`, `tests_run_all_reason: "time budget exceeded during test selection"`, and
  pass the result through `truncate_risk_result`. Add `partial` to `_RISK_SCALAR_KEYS`.
- Test with a deadline mocked to expire inside test selection.

### Task 6.3 — `cortex_overview` reports `dead_code` progress

- **Files:** `src/cortex/mcp/tools.py` (overview text), `tests/test_overview_budget.py`.
- When the dead-code scan stopped early, the overview text says
  `dead_code: scanned N of M candidates; call again to resume` (the resume state already exists).

---

## Phase 7 — Release

### Task 7.1 — 0.14.0

- Version bump in `pyproject.toml`, plugin manifests and the goldens that pin it (see the 0.13.0
  release commit for the list; `tests/test_plugin_manifests.py` checks them).
- `CHANGELOG.md` `## 0.14.0`: every task's bullet; the gates named as in Task 3.3; a "Not met / not
  verified" section listing every field acceptance item of the review (§A–§F) as "not verified"
  until the reviewer's run, plus any Phase 5 task that hit the stop rule; "Graph format 7: an index
  from an older format is rebuilt in full by its next refresh".
- Mark this plan's status line "implemented in 0.14.0" with outcomes and deviations, as
  `docs/plan-0.12.1-review.md` does.

---

## 4. Deferred (not in 0.14.0)

- **Build-context include paths (§C fixes 1–2).** Ingest `target_include_directories` /
  `include_directories` from CMake for test targets and resolve same-label classes per build
  context. Needs: a CMake model that survives generator expressions and variables, a per-target
  include order stored on `target:` nodes, a rule for files compiled into several targets, and an
  incremental story (a `CMakeLists.txt` change re-resolves every edge from files of that target).
  Task 1.3 restores 0.12.1 parity in the meantime.
- **Member-type inference for `m_*` receivers** (§E, 13.6k calls) and **chained calls** (14.9k):
  bucketed by Task 1.4, not fixed.
- **Per-language call resolution** for JS/Java/Python (§E, 13.9k): reported by Task 3.3, not fixed.

## 5. Reviewer run (unchanged procedure)

The release candidate goes to the reviewer with the script and pinned ranges of the 0.12.1 plan.
Ask for: `dead_code` added/removed vs 0.12.1 and 0.13.0; call edges `symbol:` → `name:` vs 0.12.1;
`cortex_risk` `files` counts at 2000/4000 on the four ranges; `cortex_status` `resolution` block;
T1–T4 gold files; cold time. Items stay "not met" until that run says otherwise.
