# Qt Connect Resolution Precedence Plan

## Context

Cortex is a local-first repository context engine whose graph layer models C++/Qt declarations, definitions, `emit` sites, `connect()` wiring, QML handlers, and cross-file relationships. This plan completes the uncommitted tree-sitter qualifier parity work while restoring the established Qt contract that a class-qualified `connect()` endpoint resolves to the class's Qt declaration when both a header declaration and a `.cpp` definition exist.

### History and root cause

1. Commit `553dd57` introduced the `qt_app` eval fixture in `evals/run_evals.py`. It deliberately uses normal Qt layout: `DeviceModel::onDeviceConnected` is declared as a slot in `include/DeviceModel.hpp`, defined in `src/DeviceModel.cpp`, and referenced by a pointer-to-member `connect()`. Its gold symbol is the header declaration.
2. Commit `6046896` implemented P0-4 from `IMPROVEMENT_PLAN.md`: `QtSymbolIndex` and `_resolve_qt_edges` resolve Qt signal/slot placeholders to `qt: signal`/`qt: slot` declaration nodes. `tests/test_qt_relations.py::test_connects_resolves_both_endpoints_to_real_signal_slot_symbols` locked in header-declaration resolution. Downstream risk and dead-code logic relies on resolved Qt-tagged nodes.
3. Commit `c299c87` implemented Item 5 from the literal repository-root `plan.md`. Pointer-form connects became `name:Class::member`; regex-parsed out-of-line definitions gained `metadata["qualifier"]`; and `graph.py::resolve_connect_endpoints` resolved only when the set of matching files produced exactly one symbol. Its regression fixture covered a header signal plus a `.cpp`-only slot definition, but not the common declaration-plus-definition case.
4. Merge commit `e6e31ef` combined both designs. `_resolve_qt_edges` still handles `module:` placeholders, while pointer-form connects now reach the later generic resolver as `name:` endpoints. The two resolution contracts therefore meet in `resolve_connect_endpoints`.
5. `FOLLOWUP_TS_QUALIFIER.md` identified that the default tree-sitter C++ path did not mirror the regex qualifier metadata and could label `void Cls::slot()` as `Cls`. The current uncommitted changes in `src/cortex/structural/treesitter_backend.py` fix that structured declarator extraction, and `tests/test_qt_connects.py` adds initial tree-sitter coverage.
6. Correct tree-sitter extraction exposes both `symbol:include/DeviceModel.hpp:onDeviceConnected` (Qt-tagged declaration) and `symbol:src/DeviceModel.cpp:onDeviceConnected` (qualified definition). The generic uniqueness rule treats the pair as ambiguous and leaves `name:DeviceModel::onDeviceConnected` unresolved. Before the fix, tree-sitter's name bug accidentally hid the `.cpp` member candidate, so the header-only result appeared unambiguous. The regex backend can expose the same latent conflict because it already supplies qualifier metadata.
7. Direct verification of the current worktree, with Apple Git's system templates disabled for sandbox compatibility, produced `396 passed, 1 failed, 4 skipped`; the sole failure is the historical P0-4 assertion that the connect target resolve to `include/DeviceModel.hpp`. The focused qualifier suite passes (`tests/test_qt_connects.py`: 9 passed).

The recommended policy is tiered rather than globally ambiguous: for `name:Cls::member`, prefer exactly one class-qualified node tagged `qt: signal` or `qt: slot`; if no Qt declaration candidate exists, resolve exactly one ordinary candidate (including an out-of-line definition); if the highest applicable tier has multiple candidates, keep the endpoint unresolved. This preserves both P0-4's declaration contract and Item 5's `.cpp`-definition fallback.

### File map

- `plan.md` — untracked repository-root historical plan; Item 5 introduced regex qualifier metadata and the generic uniqueness rule. Preserve it unchanged.
- `FOLLOWUP_TS_QUALIFIER.md` — untracked follow-up specification for tree-sitter qualifier/member parity. Preserve it unchanged; this plan supersedes only its disproven assumption that `graph.py` needs no adjustment.
- `HANDOFF.md` — operational rules: preserve pending work, maintain regex/tree-sitter Qt parity, independently verify, and leave delegated implementation uncommitted for review.
- `IMPROVEMENT_PLAN.md` — authoritative P0-4 contract and Qt parity constraints; connect endpoints must resolve to real signal/slot symbols across the header/implementation split.
- `src/cortex/structural/treesitter_backend.py` — current uncommitted `_cpp_member_for_definition` helper and C++ symbol emission changes.
- `src/cortex/structural/regex_backend.py` — regex qualifier metadata, Qt signal/slot tagging, and pointer-form connect extraction; behavior must remain compatible and this file should not require implementation changes.
- `src/cortex/structural/__init__.py` — tree-sitter-first dispatch with fail-soft regex fallback; tests must prove both paths.
- `src/cortex/graph.py` — `QtSymbolIndex`, `_resolve_qt_edges`, and `resolve_connect_endpoints`; the narrow resolver-precedence correction belongs here.
- `src/cortex/ingest.py` — full/incremental graph convergence calls the resolver over merged nodes and edges; no ingest change is expected, but incremental behavior must remain covered.
- `src/cortex/store.py` — persists Qt declaration metadata and supplies `QtSymbolIndex` for incremental ingest; no schema or store change is expected.
- `src/cortex/deadcode.py` — credits resolved `connects` edges and Qt metadata; declaration resolution protects against false dead-code findings.
- `src/cortex/risk.py` — treats resolved `qt: signal` endpoints as authoritative when producing signal-site directives.
- `tests/test_qt_connects.py` — focused regex/tree-sitter qualifier and connect-resolution regression suite; currently modified and the primary place for the new precedence matrix.
- `tests/test_qt_relations.py` — P0-4 integration contract; its existing `DeviceModel.hpp` assertion must pass unchanged.
- `tests/test_refresh_mode.py` — incremental resolver convergence coverage; must continue passing.
- `tests/test_deadcode.py`, `tests/test_risk.py`, and `tests/test_context_mcp.py` — downstream Qt graph consumers to include in focused verification.
- `evals/run_evals.py` — defines `qt_app` and its gold header symbols; do not change the fixture or expected symbol to conceal the resolver regression.
- `pyproject.toml` — tree-sitter C++ is optional under `[languages]`; the stdlib regex path remains mandatory.

## Goals

- Complete the structured tree-sitter C++ qualifier/member extraction already present in the worktree.
- Preserve member labels and IDs (`slot`, not `Cls`) and normalize nested `A::B::member` to qualifier `B`.
- Resolve a class-qualified Qt connect endpoint to the unique Qt-tagged declaration when a matching `.cpp` definition also exists.
- Fall back to a unique out-of-line definition when no Qt declaration exists, preserving Item 5 and `FOLLOWUP_TS_QUALIFIER.md` behavior.
- Preserve unresolved endpoints for genuine same-tier ambiguity.
- Prove equivalent behavior with the tree-sitter backend active and with tree-sitter forced off.
- Restore a fully green test suite and leave the complete change uncommitted for independent review.

## Non-goals

- Do not redesign or merge `QtSymbolIndex` and the generic resolver beyond the narrow precedence rule.
- Do not change `emit`, QML `handles`, or `instantiates` resolution.
- Do not change symbol ID format, labels, graph schema, persistence, or ingest architecture.
- Do not change `evals/run_evals.py`, the `qt_app` fixture, its gold header symbols, or the existing assertion in `tests/test_qt_relations.py`.
- Do not broaden qualifier extraction to QML, Swift, Rust, JavaScript, or other languages.
- Do not make tree-sitter mandatory or weaken the regex fallback.
- Do not alter the repository-root `plan.md`, `FOLLOWUP_TS_QUALIFIER.md`, or `HANDOFF.md`.
- Do not commit, push, create a branch, or open a pull request.

## Constraints & Facts

- Working directory: /Users/alilkuizon/Personal Projects/Cortex
- Date: 2026-07-17
- Git branch: claude/cortex-rtk-repowise-analysis-zh2g8p @ e6e31ef (5 uncommitted change(s))
- User-selected endpoint policy: prefer the unique Qt declaration; use the unique `.cpp` definition only when no Qt declaration exists.
- User-selected packaging: finish the tree-sitter parity and resolver correction as one atomic, uncommitted change.
- User-selected scope: narrow class-qualified `connects` resolution only; preserve fixtures and other Qt resolution paths.
- User-selected completion state: leave all implementation changes uncommitted for review.
- Current tracked modifications are `src/cortex/structural/treesitter_backend.py` and `tests/test_qt_connects.py`; `FOLLOWUP_TS_QUALIFIER.md`, `plan.md`, and `pytest-of-alilkuizon/` are untracked. Preserve the two plan/spec files. Remove `pytest-of-alilkuizon/` only after confirming it is the generated pytest temp artifact, not user content.
- The core remains stdlib-only. `tree_sitter`/`tree_sitter_cpp` are optional extras and all tree-sitter-specific tests must skip cleanly when unavailable.
- Regex/tree-sitter C++/Qt parity is mandatory. A fix that passes only with the grammar installed is incomplete.
- The exact Step-0 source in `FOLLOWUP_TS_QUALIFIER.md` contains Qt's `signals:` label, which the installed `tree-sitter-cpp` grammar reports as a parse error. Use a grammar-valid direct extraction fixture to prove the tree-sitter path, while retaining a realistic header/implementation `build_graph` fixture that exercises normal fallback behavior for the header.
- The resolver must retain conservative ambiguity behavior. If more than one Qt declaration candidate exists, do not choose arbitrarily; if no Qt declaration exists and more than one ordinary candidate exists, keep `name:Cls::member` unresolved.
- Existing `GraphNode.metadata["qt"]` values `signal` and `slot` are the declaration-tier discriminator. Do not infer declaration status from filename extensions.
- Apple Git is `git version 2.50.1 (Apple Git-155)` and reads system templates under Xcode. In restricted test environments, set `GIT_CONFIG_NOSYSTEM=1` and point `GIT_TEMPLATE_DIR` at an empty temporary directory so fixture `git init` calls do not fail for unrelated template-copy permissions.
- Run mutating tasks that touch `tests/test_qt_connects.py` serially to avoid conflicting edits.

## Tasks

1. **Strengthen the focused regression matrix in `tests/test_qt_connects.py`.** Preserve the existing regex-fallback qualified-definition test, ambiguity test, and current tree-sitter-on test. Make the tree-sitter test prove direct tree-sitter extraction rather than allowing `build_graph` to silently fall back. Add grammar-gated assertions for `void Cls::slot() {}`, nested `void A::B::member() {}`, and an unqualified free function: labels/IDs must use the bare member, nested qualifier must be `B`, and the free function must have no qualifier. Add a realistic declaration-plus-definition case where a Qt-tagged header slot and its `.cpp` definition both exist; assert the connect target is the header declaration. Exercise this precedence once with tree-sitter active and once with `extract_treesitter_edges` forced to fail. Keep the existing no-header-declaration case asserting fallback to the `.cpp` definition, and keep genuine duplicate-definition ambiguity unresolved. **Acceptance:** the test file explicitly covers (a) declaration preferred over definition, (b) definition fallback, (c) same-tier ambiguity, (d) nested-scope normalization, (e) no invented free-function qualifier, and (f) both backend paths; tests fail only on the missing precedence behavior before Task 3.

2. **Review and finalize the current tree-sitter C++ extraction change in `src/cortex/structural/treesitter_backend.py`.** Keep the helper local to C++ `function_definition` symbol emission. Walk tree-sitter's declarator fields to the innermost `qualified_identifier`/`scoped_identifier`, derive the trailing member and last scope segment, and attach `metadata["qualifier"]` only when non-empty. Preserve existing naming fallback for unqualified functions and all non-C++ languages. Avoid text-parsing the full function signature and avoid changes to Qt regex extraction. **Acceptance:** all Task 1 extraction assertions pass; `symbol:Cls.cpp:slot` has label `slot` and qualifier `Cls`; `symbol:Nested.cpp:member` has qualifier `B`; unqualified functions and non-C++ symbols do not gain qualifier metadata.

3. **Add declaration-first precedence to `src/cortex/graph.py::resolve_connect_endpoints`.** Preserve the existing class-file eligibility map and class-qualified endpoint parsing. Retain each candidate node (or equivalent access to its metadata), not only its ID. For a candidate set, first inspect nodes tagged `qt: signal` or `qt: slot`: resolve when that declaration tier contains exactly one node; if it contains multiple nodes, remain unresolved. If the declaration tier is empty, apply the existing exactly-one-candidate rule across ordinary class-file/qualifier candidates; otherwise remain unresolved. Apply this only to `connects` endpoints beginning with `name:` and keep self-loop dropping unchanged. Do not modify `_resolve_qt_edges`, `QtSymbolIndex`, ingest, store, or regex extraction. **Acceptance:** `DeviceModel::onDeviceConnected` resolves to `symbol:include/DeviceModel.hpp:onDeviceConnected` despite the `.cpp` definition; the `.cpp`-only `Cls::slot` case still resolves to `symbol:Cls.cpp:slot`; duplicate declarations or duplicate definitions remain unresolved; unrelated edge relations are byte-for-byte unaffected.

4. **Verify downstream and incremental contracts without changing their expectations.** Run the existing P0-4 relation test, incremental refresh test, dead-code tests, risk tests, and context tests. Inspect the resulting connect edge to confirm its endpoint node carries `metadata["qt"] == "slot"` in the declaration-plus-definition fixture. Confirm that the resolver remains idempotent when incremental ingest re-runs it over merged graph rows. **Acceptance:** all focused downstream tests pass unchanged, the existing `tests/test_qt_relations.py` header assertion is green, and no modifications appear in `src/cortex/ingest.py`, `src/cortex/store.py`, downstream consumers, or eval fixtures.

5. **Perform full verification and leave a cleanly reviewable uncommitted diff.** Run formatting/diff checks, the focused suite, the full suite with tree-sitter installed, and the eval harness to ensure the Qt gold task and existing precision/recall do not regress. Remove only generated pytest temp artifacts after verification. Review `git status` and the complete diff; expected tracked files are `src/cortex/structural/treesitter_backend.py`, `src/cortex/graph.py`, and `tests/test_qt_connects.py`. Preserve untracked `plan.md` and `FOLLOWUP_TS_QUALIFIER.md`. Do not commit or push. **Acceptance:** full pytest is green with no permission-derived failures, relevant eval precision/recall is unchanged or improved, `git diff --check` is empty, no unrelated tracked files changed, generated `pytest-of-alilkuizon/` is absent, and the implementation remains uncommitted.

## Verification

Run from `/Users/alilkuizon/Personal Projects/Cortex`.

```bash
# Review scope and whitespace first.
git status --short
git diff --check
git diff -- src/cortex/structural/treesitter_backend.py src/cortex/graph.py tests/test_qt_connects.py

# Confirm optional grammar availability; grammar-gated tests may skip when absent.
python3 -c "import tree_sitter, tree_sitter_cpp; print('tree-sitter C++ available')"

# Focused qualifier and endpoint policy.
python3 -m pytest tests/test_qt_connects.py -q

# Downstream contracts affected by resolved Qt endpoints.
EMPTY_GIT_TEMPLATE="$(mktemp -d)"
export GIT_CONFIG_NOSYSTEM=1
export GIT_TEMPLATE_DIR="$EMPTY_GIT_TEMPLATE"
python3 -m pytest \
  tests/test_qt_relations.py::test_connects_resolves_both_endpoints_to_real_signal_slot_symbols \
  tests/test_refresh_mode.py::test_incremental_refresh_recomputes_degrees_and_resolves_new_member \
  tests/test_deadcode.py \
  tests/test_risk.py \
  tests/test_context_mcp.py \
  -q

# Full regression suite. No failed test is acceptable.
python3 -m pytest tests/ -q

# Retrieval/eval regression without modifying evals/RESULTS.md.
python3 evals/run_evals.py --results /tmp/cortex-qt-connect-results.md

# Final repository review; remove only the generated pytest temp artifact if present.
rm -rf pytest-of-alilkuizon/
git diff --check
git status --short
git diff --stat
rm -rf "$EMPTY_GIT_TEMPLATE"
```

Manual checks:

1. Direct tree-sitter extraction of `void Cls::slot() {}` prints label `slot`, node ID `symbol:Cls.cpp:slot`, and qualifier `Cls`.
2. Direct extraction of `void A::B::member() {}` prints qualifier `B`; `void free_fn() {}` has no qualifier key.
3. A graph with a Qt header declaration plus `.cpp` definition resolves `name:DeviceModel::onDeviceConnected` to the Qt-tagged header declaration.
4. A graph with no member declaration but one qualified `.cpp` definition resolves to that definition.
5. Two Qt declaration candidates, or two ordinary definition candidates with no declaration candidate, remain class-qualified unresolved endpoints.
6. `tests/test_qt_relations.py` remains unchanged and passes, proving the historical P0-4 contract rather than weakening the fixture.
7. Final status contains no commit and no tracked modifications outside the three expected implementation/test files.
