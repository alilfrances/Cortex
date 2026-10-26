# Plan: Cortex 0.15.0 — 0.14.0 field review (fifth pass)

Status: **implemented in 0.15.0**, except where noted. Outcomes and deviations (details in `CHANGELOG.md`):
- Phases 1, 3, 4 and 5: as planned. Task 1.4 (optional) was done under its decision rule: 13 class findings
  disappear across the fixtures, none is added. Task 5.4 took the owner default (`haiku`). Task 5.5, 5.6
  and 5.7 as planned. Task 5.3 as planned (3,722 characters for the five tools).
- Deviations: Task 1.1 also changed the `cortex_dead_code.multilang_repo` goldens (they had recorded the
  namespace finding; the plan listed none). Task 2.1 leaves a call operator defined in an anonymous struct,
  `bool operator()(int a)`, labelled `a` (a `func`, not a `class`; a separate naming limit of `operator`
  definitions). Task 4.1's `ui_calls` variant needs `onItemTypeChanged` to call the helper as well, because
  with the plan's shape Task 4.2 alone already restored the expansion; Task 4.3's `max(...)` fallback was
  not needed. Task 5.2b: the hook already imported `cortex.ingest` at the top of `_repo_status`; the import
  now sits below the format check. Task 6.1: `_member_type` was split into `_member_type_candidates` and a
  wrapper so the audit reuses the lookup.
- Added after the Phase 5 review: SessionStart says "a rebuild is already running" (instead of "run
  cortex_refresh") when the background rebuild is skipped because the ingest lock is held.
- Phase 6: the audit buckets landed; on the `--field-shape` repository every member bucket is 0 (all 64
  unresolved calls are `external`), so the next release's choice needs the table from the field index.
- Phase 7: the version is 0.15.0; `tests/goldens/mcp/initialize.json` carries the new `serverInfo.version`.
- Still not met / not done: every field acceptance item of §5 (not verified until the reviewer's run); the
  items of §3 (unresolved virtual call, plugin boundary, JS/Java/Python call coverage, partial test coverage
  under a deadline).
Input: `docs/review-0.14.0-field.md` ("Cortex 0.14.0: field review (fifth pass)", C++/Qt/QML repository,
3.7k files, 161k nodes, 622k edges). Section numbers (§1.1–§3.4) below refer to that document.
Base: `main` at 0.14.0 (`7365b10`).
Target release: **0.15.0**. `GRAPH_FORMAT_VERSION` 7 → 8, because Task 2.1 changes node ids (the rule is in
`CONTRIBUTING.md`, "Graph format version"). If Task 2.1 is dropped, no bump is needed and the release is 0.14.1.

This plan is written for an implementer who has not seen the investigation. Every task names the files,
the change, the tests and the acceptance check. Do the tasks **in order**. Each task is one commit and
leaves CI green. Where a task needs a judgement call, the decision rule is written into the task: follow it
rather than improvising. If a decision rule cannot be met, stop that task, leave it out of the commit, and
record what you measured under "Not met" in `CHANGELOG.md`. Do not loosen a gate to make it pass.

---

## 0. Rules for the implementer (read first)

1. **Setup** (once per session):
   ```bash
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ".[llm,watch,dev,tokens]" build twine
   python -m cortex runtime setup          # must report "ready": true
   ```
   Before the PR, run the suite once more **without** `tiktoken` (`pip uninstall -y tiktoken`). The reviewer
   runs with the stdlib token estimate.
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
   Before the PR, also run the coverage floors
   (`python -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json && python scripts/check_coverage_floors.py`),
   `python evals/run_evals.py` and `python -m build`.
3. **Goldens.** A task that does not list a golden must not change one. If one changes, stop and find out
   why. To regenerate:
   `CORTEX_UPDATE_GOLDENS=1 python -m pytest tests/test_golden_*.py tests/test_eval_baseline.py`. Then re-run
   without the variable, review the diff (it should contain only what the task intends), and add a
   `CHANGELOG.md` line naming each golden and why.
4. **Eval gate** (Phase 4 and any task that says so):
   ```bash
   git stash -u && python scripts/perf/eval_compare.py /tmp/before.json && git stash pop
   python scripts/perf/eval_compare.py /tmp/after.json
   python scripts/perf/eval_compare.py --compare /tmp/before.json /tmp/after.json
   ```
   The gate passes only if no row's `recall` or `file_recall` decreases and each metric's mean is >= the
   baseline's. Paste the `--compare` summary into the commit body. Never commit `evals/RESULTS.md`.
5. **Old-release comparisons.** Make a worktree once with `git worktree add /tmp/cortex-0130 f476f21` and run
   old code as `PYTHONPATH=/tmp/cortex-0130/src python ...`.
6. **Failing test first.** For every defect task, add the regression test in the same commit as the fix.
   Write it and run it against the unfixed code first, and paste the failure into the commit body.
7. Keep changes minimal and in the surrounding style: module-level constants with an explanatory comment,
   and long comments only where the code is non-obvious. Create a `## Unreleased` section at the top of
   `CHANGELOG.md`, and add a bullet to it for every task that changes behaviour, output shape or speed.
8. Do not put any model name or session link in commits, code or docs.

---

## 1. Verdict on the review

Every claim was checked against `7365b10`. Reproductions used hand-written mini-repositories, the test
fixtures and a 0.13.0 worktree. The field index itself is private and was not available.

| § | Claim | Verdict | Evidence / correction |
|---|---|---|---|
| 1.1a | `dead_code` reports namespace nodes; exclude them | **Valid, wider than stated** | Namespaces are `kind="class"` nodes whose signature starts with `namespace` (the same test as `callresolve._Index.is_namespace`, `callresolve.py:218`). Plain `namespace Plain {}` nodes are reported too, in 0.13.0 as well, so the exclusion removes more than the 37 "added" ones. 0.14.0 only relabelled `namespace A::B` from `A` to `B`, which created new labels; it did not create the defect. The fix needs no graph format change, because detection by signature works on existing indexes. Task 1.1. |
| 1.1b | 25 constructors and methods, "called in the source" | **Partly reproduced; not a 0.14 regression in any shape tried** | In-class calls (`helper()`, `this->helper()`, inline header methods, out-of-line methods inside `namespace A::B {}` and `namespace A { namespace B {} }`) all resolve (`this` tier) in both versions. `dead_code` on every C++ fixture (`calls_cpp`, `qt_calls_app`, `calls_invariant`, `calls_multilang`, `qml_full_repo`) adds no constructor or method finding from 0.13 to 0.14. Real false positives found, present in **both** versions: (1) out-of-line constructors `X::X()` in the `.cpp`, because `new X` / `X x;` resolve to the class node, never to the constructor; (2) out-of-line destructors `X::~X()`, reported with **high** confidence (grep finds no `~X` caller); (3) classes used only through declarations (`Local l; l.go();`). Items (1)–(2) are the likely bulk of the 25: 0.14.0 resolves more includes to files (Task 4.1 of the 0.14 plan), so more `new X` edges go to the header class. Tasks 1.2, 1.3 and (optional) 1.4. |
| 1.2 | Anonymous `struct {}` / `typedef struct {}` take a body identifier as label | **Valid, and wider** | Reproduced: `struct { bool operator()(int a, …) }` → class `a`; `typedef struct { QString name; } Rec;` → class `QString`. The same fallback hits `enum { kMax = 10 };` → class `kMax` and `union { … } u;`. The review's wording is slightly off: the namespace rule emits **no node**, not a node labelled `(anonymous)`. Correct rule: a `typedef` takes the typedef name (`Rec`, which is the name code actually uses); any other unnamed type emits no node. Node ids change, so `GRAPH_FORMAT_VERSION` 7 → 8. Task 2.1. |
| 1.3 | `uncovered_changed_files_total` depends on budget; compute it before truncation | **Symptom real, cause misattributed** | The truncator already takes the total from the full list before it cuts anything (`risk.py:1137`), and `analyze_risk` has no budget parameter. The described run (`tests_to_run` empty, no map, 604 = every changed file) is the **deadline** fallback at `risk.py:835-846`: test selection timed out (300-commit risk takes 84 s against `CORTEX_TOOL_TIMEOUT_SECONDS=30`), `partial: true` was set, and every reviewable changed file was listed as "uncovered". The review's principle still holds: files whose tests were never traced must not count as uncovered. Task 3.1. |
| 1.4 | Seed files ranked by length-normalised score; UI spans with no calls take all seeds | **Valid, confirmed** | `_select_seeds` receives `source_scores` (normalised, `bundle.py:1584`), while spans inherit `raw_source_scores` (`bundle.py:1524`). That is inconsistent with the documented intent of the 0.14 "R2" change. Reproduced on a synthetic fixture (six small QML screens dense with task words, one large C++ model file calling a virtual with two overriders): **0 call-expansion items in all four eval modes**. Prototyped both proposed changes: each alone gives 3 call/override items in every mode, both are eval-gate neutral (all means unchanged), and the 128 bundle/override/eval tests pass. Tasks 4.1–4.3. |
| 2 | Plugin boundary; unresolved virtual call; `calls` 38%; T1 gold | **Deferred / corrected** | Plugin boundary: no static fix, agreed. Unresolved virtual call: the proposal needs a symbol for a non-pure virtual **declaration** in a header (only `= 0` declarations get one since 0.14), so it is a format change; fixture first, next release (§3). `m_*` members: the review calls this the cheapest gain because `member_types` is "already in metadata". In fact the resolver **already** joins `receiver_member` with cross-file `member_types` (`callresolve.py:578`). The 13.6k misses are cases where that join fails, so the next step is an audit bucket (Task 6.1), not new inference. T1: drop it from acceptance, as the reviewer asks. |
| 3.1 | SessionStart says the next read call rebuilds an outdated index; it does not | **Valid** | `hooks/session-start.py:103` against `mcp/tools.py:546-553`: with a call deadline set, a format-outdated index is answered as-is. Background rebuild: **valid but not safe today.** A full ingest writes in five transactions (`ingest.py:616-620`: `reset_repo`, sources, commits, graph, version), so a concurrent reader can see an empty index. The write must become one transaction first; that also stops a crash mid-write from leaving an empty index. Tasks 5.1, 5.2. The `~/.local/bin/cortex` `ModuleNotFoundError` is a stale local install (`install.sh` creates no such shim), but the skill depends on it; Task 5.7 adds a self-locating CLI launcher. |
| 3.2 | All 15 tools deferred behind ToolSearch; mark five `anthropic/alwaysLoad` | **Valid** | Claude Code documents per-tool `_meta["anthropic/alwaysLoad"]: true` in the `tools/list` entry. Measured: the five tools come to 3,722 chars of schema (vs 10,964 for all 15). An upfront tool's description is cut at 4,096 chars, and each of these is about 100. Task 5.3. |
| 3.3a | `cortex-explorer` has no `model:` | **Valid** | Frontmatter `model` accepts `haiku`/`sonnet`/`opus`/`inherit`/full id. Frontmatter **beats** `CLAUDE_CODE_SUBAGENT_MODEL`; only the per-invocation `model` parameter overrides it. Owner decision (default below). Task 5.4. |
| 3.3b | `Grep`/`Glob` unavailable; replace with `Bash` | **Incorrect** | Claude Code docs: "On macOS, Linux, and WSL, a subagent can also receive the Glob and Grep tools when the main conversation doesn't have them." Keep them. Adding `Bash` would break the agent's read-only contract. No change; `tests/test_plugin_manifests.py:95` already pins them. |
| 3.4a | SessionStart text duplicates the MCP instructions | **Valid** | It repeats the tool preference list in `SERVER_INSTRUCTIONS`. The one thing it adds is the `cortex-explorer` delegation hint, which cannot move to the server instructions because Codex shares them and has no such agent. Keep one short clause. Task 5.1. |
| 3.4b | `cortex_references` returns locations without text | **Valid** | Concise items are `{"text": "path:line", "origin", "access"}`. `deadcode` also calls `find_references` and fits its results to a budget, so snippets must be **opt-in** or dead-code tiers could change. Task 5.5. |
| 3.4c | `_meta.saved_tokens` has no observable basis | **Partly valid** | The basis is documented in `_baseline_tokens` (`mcp/tools.py:2920`): a full read of every distinct referenced file. For `cortex_references` the realistic alternative is grep output, not reading whole files. Task 5.6. |
| 4 | Verified unchanged | Accepted | — |

---

## 2. Phases

### Phase 1 — `dead_code` false positives (no format change)

#### Task 1.1 — Never report namespace nodes (§1.1a)

- **File:** `src/cortex/deadcode.py`.
- **Change:** add a module-level helper next to `_is_pure_virtual`:
  ```python
  _NAMESPACE_SIGNATURE_RE = re.compile(r"^\s*(?:inline\s+)?namespace\b")

  def _is_namespace(node: GraphNode) -> bool:
      """A C++ `namespace X {` node (a `class` node in the graph): a scope, never something code calls."""
      return node.kind == "class" and bool(_NAMESPACE_SIGNATURE_RE.match(node.signature or ""))
  ```
  In the candidate loop of `analyze_dead_code` (next to the `_is_pure_virtual` skip, around line 490) add
  `if _is_namespace(node): continue` with a one-line comment. Do **not** add the C++ suffix check: the regex
  backend emits the same signature, and no other language has a `namespace X {` class node.
- **Tests** (new `tests/test_deadcode_cpp_scopes.py`; Phase 1 tests share this file): a mini-repo built like
  `tests/test_deadcode_overrides.py::_findings`, with
  - `lib/ns.h`: `namespace Outer::Inner { int used(); }` and `namespace Plain { int p(); }`
  - `lib/ns.cpp`: the definitions, with `Plain::p` calling `Outer::Inner::used()`
  - `app/main.cpp`: calling `Plain::p()`
  - `inline namespace v1 { int q() { return 0; } }` somewhere

  Assert that no finding has symbol `Inner`, `Plain`, `Outer` or `v1`, and that `q` is still reported
  (a real unreferenced function).
- **Acceptance:** on `tests/fixtures/calls_invariant`, the `nested_namespace/lib/ab.cpp B` finding is gone and
  no other finding changes.

#### Task 1.2 — Never report out-of-line constructors or destructors (§1.1b)

- **Why:** `new X` and `X x;` resolve to the class node, never to `X::X()` in the `.cpp`, and nothing ever
  calls `X::~X()` by name. A constructor is live exactly when its class is, and the class carries any finding.
  In-class constructors and destructors share the class node already, because `seen_symbols` deduplicates by
  label, so only out-of-line definitions have their own node.
- **File:** `src/cortex/deadcode.py`.
- **Change:**
  ```python
  def _is_special_member(node: GraphNode) -> bool:
      """An out-of-line C++ constructor `X::X()` or destructor `X::~X()`: run by `new X`, `X x;` or scope
      exit, which resolve to (or never name) the class, so the class node carries any finding."""
      qualifier = node.metadata.get("qualifier") if isinstance(node.metadata, dict) else None
      return (
          node.kind in _METHOD_KINDS
          and node.source_ref.lower().endswith(_CPP_SUFFIXES)
          and bool(qualifier)
          and node.label in (qualifier, f"~{qualifier}")
      )
  ```
  Skip these in the candidate loop next to Task 1.1's skip.
- **Check first:** confirm the label of `Foo::~Foo() {}` is `~Foo` and its `metadata.qualifier` is `Foo` on both
  backends (tree-sitter: yes, measured). If the regex backend labels it differently, also match
  `node.signature` against `rf"\b{re.escape(qualifier)}::~?{re.escape(qualifier)}\s*\("` there. Do not change
  extraction.
- **Tests** (same file): `core/foo.h` declares `namespace app::core { class Foo { public: Foo(); ~Foo(); int x(); }; }`,
  `core/foo.cpp` defines all three, `ui/main.cpp` does `auto* f = new app::core::Foo(); f->x(); delete f;`.
  Assert there is no finding for `Foo` in `core/foo.cpp` and none for `~Foo`. Add a control method that is
  defined but never called (`int Foo::unused()`), which must still be reported.
- **Acceptance:** the fixture diff of Task 1.1's acceptance shows no other change.

#### Task 1.3 — Guard the in-class call shapes (§1.1b, regression fence)

These already pass on 0.13.0 and 0.14.0. The test makes sure the next change to `locality()` or namespace
handling cannot break them silently.
- **Test** (same file, `@TREE_SITTER_ONLY` from `tests.golden_utils`): methods called only from their own class,
  each of which must have **no** finding:
  - `app/inl.h`: inline `a()` calls inline `b()` and `this->c()`.
  - `app/widget.cpp`: out-of-line `Widget::run()` calls `helperOut()` (out-of-line) and `helperInline()`
    (inline in the header).
  - `core/bar.cpp`: inside `namespace app { namespace core { … } }`, `Bar::x()` calls `Bar::y()`.
  - `core/baz.cpp`: with `using namespace core;`, `Baz::x()` calls `Baz::y()`.
  - Every class is reached from another file through a typed local (`Inl i; i.a();`).
- **Follow-up for the reviewer** (put this in the PR description): ask for the 25 `(file, symbol, line)` triples
  from the 0.14.0 run, or the source shape of the three spot-checked ones. A shape that still fails after
  Tasks 1.1–1.2 becomes a new fixture in this file.

#### Task 1.4 — *(optional, owner decision; not in the review)* Credit a class with a live member

- **Why:** `Local l; return l.go();` resolves `go` (`declared_type`), but the class `Local` has no incoming edge
  and is reported, in both versions.
- **Rule:** a `class` node (not a namespace) is skipped when any `func`/`method` member has an incoming
  `calls` edge whose **source is not a member of the same class**. Members of a class are:
  - nodes in the same file whose span lies inside the class span, or
  - nodes whose `metadata.qualifier` equals the class label, when that label names exactly one class in the repo.

  Without the "not a member" condition, a dead class whose methods call each other would hide itself.
- **Test:** `Local`/`go` is not reported. A dead class `Island { void a() { b(); } void b() {} }` with no outside
  caller **is** still reported.
- **Decision rule:** if this changes any finding on the fixtures besides class nodes whose members are called
  from outside, drop the task and record it under "Not met".

### Phase 2 — Extraction

#### Task 2.1 — Anonymous C/C++ types get no fake label (§1.2) — `GRAPH_FORMAT_VERSION` 7 → 8

- **File:** `src/cortex/structural/treesitter_backend.py`. The naming branch is around line 576, where the
  `namespace_definition` rule already lives.
- **Change:** for `struct_specifier`, `class_specifier`, `union_specifier` and `enum_specifier` in
  `_CPP_SUFFIXES | {".c"}` with no `name` field:
  1. If `node.parent.type == "type_definition"`, the label is the typedef name: the first `type_identifier`
     under the parent's `declarator` field. For `typedef struct { … } Rec;` that is `Rec`. When several
     declarators are listed (`typedef struct {…} A, *PA;`), use the first plain `type_identifier`.
  2. Otherwise return `""`, so no node is emitted, exactly like an anonymous namespace. This covers
     `struct { … } var;`, `enum { kMax };`, an anonymous member struct and `union { … } u;`.

  Put this in a helper `_cpp_type_name(node, source) -> str` with a docstring that names the four shapes.
  Leave named specifiers (`struct Tag {…}`, `typedef struct Tag {…} TagT;` → `Tag`) unchanged.
- **Regex backend:** check that `regex_backend` emits no node for these shapes. If it emits one, record that in
  `CHANGELOG.md`; do not change it in this task.
- **`store.py`:** set `GRAPH_FORMAT_VERSION = 8` and extend the comment block:
  `# 8 = 0.15.0: unnamed C/C++ struct/class/union/enum emit no class node; a typedef'd one is labelled by its typedef name.`
- **Tests:** add to `tests/test_cpp_pure_virtual.py`'s neighbour or a new `tests/test_cpp_anonymous_types.py`
  (`@TREE_SITTER_ONLY`), using the source:
  ```cpp
  enum { kMax = 10 };
  union { int i; float f; } u;
  typedef struct { QString name; } Rec;
  struct { bool operator()(int a) const { return a; } } lessThan;
  struct Outer { struct { int x; } inner; };
  typedef struct Tag { int y; } TagT;
  ```
  Assert the class labels are exactly `{"Rec", "Outer", "Tag"}`, and that no node is labelled `kMax`, `i`,
  `QString`, `a` or `x`. Also assert a function defined inside the anonymous struct still yields at most one
  `func` node and never a class.
- **Goldens:** `tests/goldens` extraction snapshots may change if a fixture contains such a type. List each one
  in `CHANGELOG.md`. `tests/test_graph_format_upgrade.py` should pass unchanged, since it reads the constant.
- **Acceptance:** `python scripts/perf/synthetic_repo.py /tmp/field-repo --modules 40 --commits 120 --field-shape`,
  indexed: no `class` node whose signature starts with `struct {`, `enum {`, `union {` or `class {`.

### Phase 3 — Risk

#### Task 3.1 — A deadline in test selection does not report files as uncovered (§1.3)

- **File:** `src/cortex/risk.py`, the `except DeadlineExceeded:` branch around line 835.
- **Change:**
  - Set `uncovered_changed_files = []` there.
  - Add `result["untraced_changed_files_total"] = <count of the reviewable changed files>`, using the same
    filter the branch uses today (`status != "D"`, not a runnable test, not documentation). Set this key only
    when `partial` is true.
  - Add `"untraced_changed_files_total"` to `_RISK_SCALAR_KEYS` and `_RISK_MINIMAL_KEYS`, so
    `test_every_collection_key_is_reducible` passes and the minimal payload keeps it.
  - Update the comment above the branch: "no test was traced, so run everything; the changed files are counted
    as untraced, not uncovered".
- **Skill text:** in `skills/cortex-change-review/SKILL.md`, add one sentence: when `partial` is true, test
  selection ran out of time, `untraced_changed_files_total` counts the changed files whose tests were not
  traced, and the whole suite should run (`tests_run_all` is true).
- **Tests** (`tests/test_risk.py`): monkeypatch `cortex.risk.select_tests_with_coverage` to raise
  `DeadlineExceeded`. Assert:
  - `partial` is true;
  - `uncovered_changed_files == []`;
  - `untraced_changed_files_total` equals the number of reviewable changed files;
  - after `truncate_risk_result(result, b)` for `b in (500, 2000, 8000)`, `untraced_changed_files_total` is
    unchanged and `uncovered_changed_files_total` is absent.

  Add a second test without the deadline: `uncovered_changed_files_total` (when present) or
  `len(uncovered_changed_files)` is identical across budgets 500–20000 in steps of 500 on
  `tests/test_risk_truncate.py::_large_diff_result`. This pins what the reviewer asked for.
- **Goldens:** none expected (the golden runs do not hit the deadline).
- **Reviewer note** (for the PR description): the 604-at-2000 run carried `partial: true`; the total did not
  depend on the budget.

### Phase 4 — Query: call-expansion seeds (§1.4)

#### Task 4.1 — Fixture (strict xfail until 4.2)

- **File:** `evals/run_evals.py`. Add `_build_field_seed_markup_repo(base, *, ui_calls=False)` next to
  `_build_field_ctor_repo`, and a `FIELD_GOLD_TASKS` entry for each variant (`field_seed_markup_repo`,
  `field_seed_markup_calls_repo`). Shape (this exact shape reproduces the defect on `7365b10`: 0
  call-expansion items in all four modes):
  - `ui/ItemScreen{0..5}.qml`: each an `Item` with `property int itemType` and two JS functions of 4–5 lines
    (`onItemTypeChanged`, `removeItem`) whose comments and identifiers repeat "item type changed … removed".
    With `ui_calls=True`, `removeItem()` also calls a same-file helper `function hideItem() { … }` of 4+ lines,
    so the span has an outgoing `calls` edge.
  - `src/baseitem.h` / `.cpp`: `class BaseItem { virtual ~BaseItem(); virtual void detach(int slot); };`
    with a 4-line body.
  - `src/kinds/textentry.{h,cpp}` and `src/kinds/imageentry.{h,cpp}`: subclasses overriding `detach`.
  - `src/itemmodel.{h,cpp}`: `ItemModel::onTypeChanged(BaseItem* entry, int slot)`, commented "the item type
    changed: the old item is removed", which calls `entry->detach(...)`, plus 150 filler methods
    `refresh000`… of 6 lines each. The file must be > `LENGTH_NORM_FROM` × the median.
  - Task: `"item changes type and then the item is removed"`; expected files `src/itemmodel.cpp`,
    `src/baseitem.cpp`; budget 2000 / tight 2000.
- **Tests** (`tests/test_bundle_field_tasks.py`): add both builders to `BUILDERS`. In all four `MODES`, assert:
  - at least one item has `metadata.call_expansion` with `from` naming a symbol in `src/itemmodel.cpp`;
  - `src/kinds/textentry.cpp` or `src/kinds/imageentry.cpp` is packed through an `override` route.

  Mark `field_seed_markup_repo` `xfail(strict=True, reason="Task 4.2")` and `field_seed_markup_calls_repo`
  `xfail(strict=True, reason="Task 4.3")`. Update `test_the_fixtures_are_deterministic_and_small` if it counts
  repos, and the eval baseline golden if `test_eval_baseline.py` snapshots `FIELD_GOLD_TASKS`; list it.

#### Task 4.2 — A seed must be able to reach something

- **File:** `src/cortex/bundle.py`.
- **Change** (prototyped; eval gate passes with all means unchanged):
  ```python
  def _select_seeds(
      items, symbol_by_id, file_scores, constructors=(),
      reaches: Callable[[str], bool] | None = None,
  ) -> list[BundleItem]:
      ...
          if symbol.span_end - symbol.span_start + 1 <= TRIVIAL_SPAN_LINES:
              continue
          if reaches is not None and not reaches(symbol.node_id):
              continue  # no resolved call and no base class: the walk would return nothing from it
          eligible.append(item)
  ```
  At the call site (around line 1584) pass
  `reaches=lambda symbol_id: bool(links.calls_from.get(symbol_id)) or bool(links.bases_of.get(class_of(symbol_id) or ""))`.
  Extend the docstring and the `SEED_FILES` comment: "spans with no resolved outgoing call and no base class
  never seed".
- **Tests:** remove the Task 4.2 xfail. Add a unit test in `tests/test_overrides.py` next to the existing
  `_select_seeds` tests: two files, the higher-scoring one has a span with no calls; with `reaches` it is
  skipped and the other file's span seeds, and without `reaches` the result is unchanged.
- **Gate:** eval gate (§0.4) and the full bundle test set.

#### Task 4.3 — Seed files are ranked by the score their spans inherit

- **Change:** pass `raw_source_scores` instead of `source_scores` as `file_scores` at the `_select_seeds` call.
  Update the comment at `SPAN_FILE_SHARE_CAP` / `SEED_FILES`: length normalisation decides which whole file
  to pack; seeds are spans and use the same raw score their spans inherit.
- **Tests:** remove the Task 4.3 xfail (the `ui_calls=True` variant: the UI spans survive Task 4.2, and only this
  change brings the C++ seed back). Run `tests/test_bundle_field_tasks.py::test_hub_flood_*` and
  `test_a_hub_constructor_does_not_flood_*` explicitly: hubs score high on raw text, and these are the tests
  that would catch a hub regaining seed slots.
- **Gate:** eval gate. Prototype result on `7365b10`: neutral. **Decision rule:** if any hub or constructor test
  fails, or the gate fails, try `max(source_scores, 0.5 * raw_source_scores)` once. If that fails too, drop
  the task and record the measurements under "Not met".

### Phase 5 — Claude Code integration

#### Task 5.1 — SessionStart: state only, and a correct outdated message (§3.1, §3.4a)

- **File:** `hooks/session-start.py`, lines 95–105.
- **Change:** build the context as:
  - fresh: `"Cortex index is fresh ({n} indexed files). Multi-step exploration can go to the cortex-explorer agent."`
  - stale: `"Cortex index is stale ({n} indexed files); read/query tools refresh it incrementally, cortex_refresh forces it."` plus the agent clause
  - outdated: `"Cortex index was built by an older Cortex ({n} indexed files) and is answered as-is until rebuilt; run cortex_refresh (a full rebuild, about a minute on a large repository)."` plus the agent clause.
    Task 5.2 replaces the sentence after the semicolon when it starts a background rebuild.
  - Keep the missing-index and runtime-warning texts.

  Remove the "Prefer Cortex MCP tools … over raw Grep/Glob/Read" sentence: `SERVER_INSTRUCTIONS` already
  says it.
- **Tests** (`tests/test_hooks.py`):
  - `test_session_start_hook_emits_fresh_context`: drop the assertions on tool names, `Grep` and
    `single lookups direct`; keep `cortex-explorer` and the 500-char cap, and tighten the cap to 200.
  - `test_session_start_hook_reports_an_index_from_an_older_graph_format` (line 112): assert `"older Cortex"`,
    `"cortex_refresh"` and `"answered as-is"` are present, and that `"next read"` and `"rebuilds it in full"`
    are absent.
  - Line 142's test: the same.

#### Task 5.2 — Atomic full-ingest write, then a background rebuild from SessionStart (§3.1 "better")

Two commits.

**5.2a — one transaction for the full write.**
- **Files:** `src/cortex/store.py`, `src/cortex/ingest.py` (lines 616–620).
- **Change:**
  - Add `CortexStore.write_full_index(repo_root, fingerprint, sources, commits, nodes, edges, graph_version)`,
    which runs `reset_repo`, `save_sources`, `save_commits`, `save_graph` and `set_repo_graph_version` in
    **one** transaction.
  - Python's `with self.connection:` commits on exit, so nesting the existing methods would commit early.
    Factor each method's body into a private `_..._rows(...)` that executes without committing, keep the
    public methods as `with self.connection: self._..._rows(...)`, and call the private ones from
    `write_full_index` inside a single `with self.connection:`.
  - Leave FTS / semantic embedding sync after the commit, as today.
- **Test** (`tests/test_multiprocess_store.py` or a new file): index a repo; start a full re-ingest in a
  subprocess; in the parent, loop `fetch_graph` (fresh connection each time) until the child exits. Assert the
  node count is never 0 and is always either the old count or the new count. Make old ≠ new by adding a file
  before the re-ingest. Run it against the unfixed code first: it must catch the empty window, or the test is
  not testing the fix. If the window is too short to catch reliably, monkeypatch `save_graph` in the child to
  sleep 0.5 s before inserting.

**5.2b — background rebuild.**
- **Files:** `hooks/session-start.py`, the new `bin/cortex-cli.py` (Task 5.7 introduces it; do Task 5.7 first
  if you prefer, the order inside Phase 5 is free).
- **Change:** when the state is `outdated` and `CORTEX_SESSION_START_REBUILD` is not `"0"`:
  1. Skip if the ingest lock is held: try a non-blocking `fcntl.flock(LOCK_EX | LOCK_NB)` on
     `ingest_lock_path(repo_root)` and release it immediately. Read `store.ingest_lock_path` for the file. On
     Windows (`os.name == "nt"`), skip the background rebuild and keep the 5.1 message.
  2. Spawn `[sys.executable, <plugin root>/bin/cortex-cli.py, "refresh", "--full", str(repo_root)]` with
     `start_new_session=True`, `stdin`/`stdout`/`stderr` set to `subprocess.DEVNULL` and `close_fds=True`.
     Do not wait. The launcher runs runtime setup first; without it the rebuild could fall back to the regex
     backend and produce a worse index.
  3. Message: `"… was built by an older Cortex; a full rebuild has started in the background (about a minute
     on a large repository). Until it finishes, tools answer from the old index with stale: true."`
  4. The whole hook must still return within the 5 s timeout. Spawning is milliseconds; never import ingest
     code in the hook process.
- **Tests:**
  - `tests/test_hooks.py`: with `CORTEX_SESSION_START_REBUILD=0`, the output equals 5.1's.
  - With it unset, monkeypatch `subprocess.Popen` through a tiny wrapper module, or run the hook with a fake
    launcher path via an env override `CORTEX_CLI_LAUNCHER` that you add for tests, and assert that one spawn
    with `refresh --full` happens.
  - A held lock means no spawn.
- **Decision rule:** if 5.2a's test cannot be made to pass, do not ship 5.2b; keep 5.1's message.

#### Task 5.3 — Load the five lookup tools upfront (§3.2)

- **File:** `src/cortex/mcp/tools.py`, `_annotate_tool_definition` (around line 286).
- **Change:**
  ```python
  # Loaded upfront by Claude Code instead of behind ToolSearch: the cheap, reliable lookups a session
  # reaches for first (about 3.7k chars of schema of the ~11k for all tools). The rest stay deferred.
  _ALWAYS_LOAD_TOOLS = frozenset({
      "cortex_search_symbols", "cortex_references", "cortex_relations", "cortex_read_symbol", "cortex_impact",
  })
  ```
  and add `"_meta": {"anthropic/alwaysLoad": True}` to those definitions only. Other MCP clients ignore
  unknown `_meta` keys, and MCP allows `_meta` on a `Tool`.
- **Tests** (`tests/test_p2_mcp.py` or `tests/test_mcp_initialize_instructions.py`):
  - `tools/list` marks exactly those five;
  - each marked tool's `description` is <= 4096 chars;
  - the summed `json.dumps` size of the five is < 4500.

  If a `tools/list` golden exists, update it and list it.
- **README:** one sentence in the MCP section naming the five upfront tools and saying users can defer all of
  them with `"alwaysLoad": false` on the server entry.

#### Task 5.4 — `cortex-explorer` model pin (§3.3a)

- **File:** `agents/cortex-explorer.md`.
- **Change:** add `model: haiku` to the frontmatter (**owner default, see §4**). Add one line to the body: the
  parent can pass `model` on the Agent call for harder questions. Do **not** touch `tools` (§1 verdict 3.3b).
- **Test:** `tests/test_plugin_manifests.py::test_cortex_explorer_agent_definition` asserts
  `model: haiku` (or whatever value the owner picks).

#### Task 5.5 — `cortex_references` returns the line text (§3.4b)

- **Files:** `src/cortex/references.py` (`find_references`, line 475), `src/cortex/mcp/tools.py`
  (`_call_references`, line 2693).
- **Change:**
  - Add a keyword parameter `snippets: bool = False` to `find_references`. When true, each pulled hit gets
    `"snippet"`: the stripped source line, cut to 160 characters with `…` appended when cut. Read the line
    from the stored source (`store.fetch_source_content`, cached per path within the call) or from the grep
    hit's already-read text where available.
  - Attach the snippet **before** the budget fit, so `returned_count`/`truncated` stay honest.
  - `_call_references` passes `snippets=True`; `deadcode` keeps the default.
  - The `text` key keeps `path:line`, because `deadcode._line_location` parses it.
- **Tests:**
  - `tests/test_references*.py` (or a new test): a hit's `snippet` equals the source line; a long line is cut.
  - `deadcode` output is unchanged on `tests/fixtures/calls_cpp` (assert the findings equal a run with the
    parameter forced off).
- **Goldens:** `tests/goldens/mcp/shapes/cortex_references.*` and `payloads/*/cortex_references.*` change; list
  them.

#### Task 5.6 — `saved_tokens` for `cortex_references` uses a grep baseline (§3.4c)

- **File:** `src/cortex/mcp/tools.py`, `_baseline_tokens` (around line 2970).
- **Change:** for `cortex_references`, the baseline becomes the token count of the grep-equivalent output: one
  `path:line:<line text>` line per returned location, built from the same snippets. Update the docstring
  bullet. Add a short "How `_meta.saved_tokens` is computed" paragraph to `README.md` that copies the
  docstring's policy in two or three sentences.
- **Test:** for a references call, `saved_tokens` is absent or <= the size of the grep-equivalent text, and the
  ledger test that pins references savings (if any) is updated with a note.

#### Task 5.7 — Self-locating CLI launcher for skills (§3.1 note)

- **Files:** new `bin/cortex-cli.py`, `skills/cortex-change-review/SKILL.md`, `tests/test_plugin_manifests.py`.
- **Change:**
  - `bin/cortex-cli.py` mirrors `bin/cortex-mcp.py`: insert `src`, run `runtime.ensure_runtime()` and
    `configure_parser_environment()` in the same fail-open block (setting `CORTEX_FORCE_REGEX=1` when not
    ready), then call `cortex.cli.main()`. Write diagnostics to stderr only.
  - In the skill, replace "From a shell, `cortex risk --tests-only` …" with: "From a shell:
    `cortex risk --tests-only` (in a Claude Code plugin install without `cortex` on `PATH`:
    `python3 "${CLAUDE_PLUGIN_ROOT}/bin/cortex-cli.py" risk --tests-only`) prints the paths for a runner and
    exits 2 when everything should run."
  - Codex reads the same skill; the first form is the one it can use.
- **Test:** like `test_mcp_launcher_needs_no_pip_install`, run `python3 bin/cortex-cli.py --help` with a clean
  `PYTHONPATH` and no install, and assert exit 0 and usage text.

### Phase 6 — Investigation only (no behaviour change)

#### Task 6.1 — Why `receiver_member` joins fail (§2, `calls` 38%)

- **File:** `scripts/perf/audit_unresolved_calls.py` (+ its test `tests/test_audit_unresolved_calls.py`).
- **Change:** for unresolved C++ `calls` edges carrying `receiver_member`, add sub-buckets:
  - `member:no_enclosing_class` (`_enclosing_classes` empty)
  - `member:not_declared` (the class and its bases have no such member in `member_types`)
  - `member:type_external` (the declared type names no repository class)
  - `member:type_wrapped` (the declared type is a template wrapper such as `QPointer<T>`,
    `std::unique_ptr<T>` or `QScopedPointer<T>`, when `T` is a repository class)
  - `member:ambiguous`

  Reuse the resolver's own helpers; do not reimplement them.
- **Acceptance:** run it on the `--field-shape` repo and paste the bucket table into the PR. Ask the reviewer to
  run it on the field index. The next release picks the largest fixable bucket.

### Phase 7 — Release

- Set the version to `0.15.0` in `pyproject.toml`, `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`
  (`test_manifest_versions_match_pyproject`).
- Rename `CHANGELOG.md` `## Unreleased` to `## 0.15.0 — <date>`. Include the format bump sentence, the
  dropped/not-met tasks, the golden list, and the §1 corrections the reviewer should know about (§1.3 deadline,
  §3.3b Grep/Glob, §2 `member_types`).
- `README.md`: tool surface changes (references snippet, upfront tools, `untraced_changed_files_total`).
- Run every check in §0.2, the coverage floors, `python evals/run_evals.py` and `python -m build`.

---

## 3. Not in this release

- **Unresolved virtual call** (§2): needs `func` symbols for non-pure `virtual` declarations in headers (as
  0.14 did for `= 0`), then the existing `override` walk applies. This is a format change. Write the fixture
  first (`obj.isFoo()` with `obj` typed as the base, the base declaring `virtual bool isFoo() const;`, one
  inline override in another header), and plan it with Task 6.1's data.
- **Plugin boundary** (`QPluginLoader`): no static fix.
- **JS/Java/Python call coverage:** deferred, as the review says.
- **Partial test coverage under a deadline:** `select_tests_with_coverage` could return what it traced before
  the deadline instead of nothing. It is worth doing after Task 3.1, but it is a larger change to
  `impacted_tests.py`.

## 4. Decisions for the owner (defaults are what the plan does)

| Decision | Default | Alternative |
|---|---|---|
| `cortex-explorer` model (Task 5.4) | `haiku`: read-only structured lookups; the parent can pass `model` per call | `sonnet` for answer quality, or no pin (inherit) |
| Task 1.4 (class credited by a live member) | Do it, under its decision rule | Skip; class false positives stay as in 0.13/0.14 |
| Background rebuild (Task 5.2b) | On by default, `CORTEX_SESSION_START_REBUILD=0` turns it off | Off by default (opt-in) |
| Task 2.1 and the format bump | Ship it in 0.15.0, together with Task 5.2 so the forced rebuild is painless | Defer Task 2.1 and release 0.14.1 without a bump |

## 5. Field acceptance (for the reviewer's next run)

- `dead_code` vs 0.14.0: no namespace node and no `X::X` / `X::~X` finding; send any remaining constructor or
  method findings as `(file, symbol, line)`.
- No `class` node labelled after a body identifier of an unnamed struct/enum/union.
- `cortex_risk` on the 300-commit range: `uncovered_changed_files_total` is identical at every budget. A
  timed-out run says `partial: true` with `untraced_changed_files_total` and an empty uncovered list.
- The §1.4 task at budget 8000 has `calls`/`override` items again, with at least one seed in the large C++ file.
- SessionStart on an outdated index starts a rebuild (or, with the opt-out set, says to run `cortex_refresh`),
  and tool answers stay available during the rebuild.
- In a fresh session, the five lookup tools are present without a ToolSearch call.
- T1 is not weighed.
