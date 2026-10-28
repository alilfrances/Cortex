# Plan: Cortex 0.16.0: C++ calls that resolve, and a calls share that counts what can

Status: **planned**, not implemented.
Input: the reviewer's `scripts/perf/audit_unresolved_calls.py <REPO> --all` table on 0.15.2 (field index):

| line | numbers |
|---|---|
| calls by target | symbol 43414, name 99117, external 11726 |
| top unresolved buckets | external 34406, free_not_in_repo 25212, chain 14871, not_cpp_js 9335, not_cpp_java 4241 |
| member buckets | no_enclosing_class 1426, not_declared 3316, type_external 473, type_wrapped 35, ambiguous 2238, method_not_found 1704 |

Base: `main` at 0.15.2 (`16edb4a`). Target: **0.16.0**. **`GRAPH_FORMAT_VERSION` 8 → 9**: Phase B changes what
resolution writes, so an index must be rebuilt. Since 0.15.0 the SessionStart hook does that rebuild in the
background.

Owner instruction: fix all of it so the reviewer can accept. The calls target is **per language** (owner decision):
C/C++ ≥ 0.6; JS and Java are reported without a target until their resolution work is planned; the combined figure
stays as information.

The rules of `docs/plan-0.15-field-review.md` §0 apply unchanged: setup, checks, goldens, eval gate, failing test
first, one commit per task, a `CHANGELOG.md` bullet per task, and no model names. Work on branch `cortex-0.16.0`.

**Rules for every Phase B task (resolver changes):**
1. **Failing fixture first.** A small hand-written C++ repository in the task's test reproduces the bucket's shape.
   The commit body pastes `audit_unresolved_calls.py --all` on it before and after.
2. **No resolved call may regress.** `tests/test_call_target_invariant.py` and the frozen call-target lists in
   `tests/fixtures/calls_invariant` must stay green. A call that resolved to `symbol:` before must not become
   `name:`, nor resolve to a different symbol, unless the task says so and lists each case.
3. **Measure.** Run the audit on the `--field-shape` synthetic repository
   (`python scripts/perf/synthetic_repo.py /tmp/field-repo --modules 40 --commits 120 --field-shape`, indexed into a
   fresh `CORTEX_DATA_DIR`) and on `tests/fixtures/calls_cpp`, before and after. Paste both into the commit body.
4. **Eval gate.** The plan-0.15 §0.4 gate: no row's `recall` or `file_recall` decreases, and the means of
   precision, precision@3, recall, file_recall and symbol_recall are ≥ the baseline's. `eval_compare.py` only
   gates `recall` and `file_recall`, so check the other means yourself.
5. **Decision rule.** A task that breaks rule 2 or 4 and cannot be fixed within the task is dropped and recorded
   under "Not met" with its numbers.

---

## 1. Verdict on the table

- **Where 0.4 comes from.** The reviewer's 0.4 is `in_repo_symbol_share` from `CortexStore.resolution_counts`
  (`src/cortex/store.py`, around line 1388): 43414 / (43414 + 99117 − 34406) = **0.402**.
- **Accounting (Phase A).**
  - `free_not_in_repo` (25212) are plain calls to a name no repository symbol has (`qDebug()`, `qMax()`, C
    functions, moc-generated code). They cannot resolve, yet they are in the denominator. Excluding them gives
    0.524.
  - JS and Java (`not_cpp_js` 9335, `not_cpp_java` 4241) are deferred languages in the same denominator. C/C++ alone
    is about 0.63.
- **Real C++ gaps (Phase B), largest first:**
  - chained calls: 14871;
  - member calls, 9192 in total:
    - `not_declared` 3316
    - `ambiguous` 2238
    - `method_not_found` 1704
    - `no_enclosing_class` 1426
    - `type_external` 473
    - `type_wrapped` 35
- **Leave alone:**
  - `type_wrapped` (35) is too small to be worth code.
  - `type_external` (473) is already out-of-repo by nature.

---

## 2. Phase A: report the share per language and count what can resolve

### Task 1: calls to names no repository symbol has count as out-of-repo

- **File:** `src/cortex/store.py`, `resolution_counts` (it serves `cortex_status`, so keep it fast and never load the
  whole graph).
- **Rule** (mirrors the audit's `free_not_in_repo` in `scripts/perf/audit_unresolved_calls.py` `classify`). A
  `calls` edge is **not in repo** when all of these hold:
  - its target starts with `name:`;
  - it has no `metadata.external` and no `metadata.ambiguous`;
  - it has no receiver (`receiver_kind` absent or `"none"`);
  - it has no scope (the fields the audit's `_scope_label` reads);
  - it has no `receiver_type`;
  - its callee (`metadata.callee`, else the target label) is not the label of **any** symbol node
    (`graph_nodes.granularity = 'symbol'`).

  The "any symbol" test is wider than the audit's free-function index, so the count can only be conservative.
  Compute it at report time, with `json_extract` on the `name:` calls edges and one `SELECT DISTINCT label` set.
- **Output:** keep every existing key and value, including `in_repo_symbol_share`, unchanged. Add `name_not_in_repo`,
  and `resolvable_share` = `symbol / (symbol + name − name_external − name_not_in_repo)` to 4 places (`None` when
  the denominator is 0).
- **Tests** (find the file with `grep -rn resolution_counts tests`), failing first:
  - a plain call to a repository function is not counted;
  - `qDebug()` with no definition is counted;
  - `obj.f()` with a receiver is not counted;
  - `Ns::f()` with a scope is not counted;
  - a plain call named like a repository method is not counted;
  - `resolvable_share` equals the formula, and `in_repo_symbol_share` is unchanged.
- **Performance:** time `resolution_counts` on the indexed `--field-shape` repository before and after. It must stay
  under 1 s there, or under 2× its old time, whichever is larger. Otherwise join against a temporary label table
  instead of filtering in Python.

### Task 2: the share per language family

- **File:** `src/cortex/store.py`, `resolution_counts`, which already computes `name_by_family` with
  `callresolve.language_family` on `metadata.source_file`.
- **Change:** add `calls.by_family.<family>` with `symbol`, `name`, `name_external`, `name_not_in_repo` and
  `resolvable_share`, for every family that occurs. A family's `symbol` count is its resolved calls edges by
  `source_file` family. Keep `name_by_family`.
- **Tool description:** update the `cortex_status` description in `src/cortex/mcp/tools.py` (around line 251) in one
  sentence, so it names `resolvable_share` and `by_family`.
- **README:** in the `cortex_status` section, list the three figures, the not-in-repo rule, and how the target is
  read: C/C++ ≥ 0.6; JS/Java reported without a target.
- **Tests:** a mixed `.cpp` + `.js` repository. Assert the per-family counts, that the family counts sum to the
  overall counts, and the per-family `resolvable_share`.
- **Goldens:** `tests/goldens/mcp/tools_list.json` (description), plus any `cortex_status` payload or shape golden
  that includes `resolution`. Regenerate, review, and list them.

### Task 3: the audit prints the same figures

- **File:** `scripts/perf/audit_unresolved_calls.py`.
- **Change:** after its summary, print one line per family (`symbol`, `name`, `name_external`, `name_not_in_repo`,
  `resolvable_share`), taken from `CortexStore.resolution_counts`. Do not reimplement it.
- **Test** (`tests/test_audit_unresolved_calls.py`): the printed line matches `resolution_counts`.

---

## 3. Phase B: resolve more C++ calls (`GRAPH_FORMAT_VERSION` 8 → 9 in the first Phase B commit)

Bump `GRAPH_FORMAT_VERSION` in `src/cortex/store.py` in Task 4's commit, with a `# 9 = 0.16.0: ...` line that later
Phase B tasks extend.

### Task 4: `member:no_enclosing_class` (1426): classes the extractor misses or misnames

- **Background.** The call site's `enclosing_class` (`call_sites.py`, `frame.class_name`, around line 131) names a
  class that has no class node, or one the resolver cannot find (`callresolve._enclosing_classes`).
- **Investigate first** with fixtures, then fix only the shapes that fail on 0.15.2:
  1. **Export or attribute macro between `class` and the name:** `class Q_DECL_EXPORT Foo : public QObject {`,
     `class MYLIB_EXPORT Foo {`. tree-sitter often takes the macro as the name. This is very common in Qt
     libraries.
  2. **Nested class:** `class Outer { class Inner { void f(); }; };` with out-of-line `void Outer::Inner::f()`.
  3. **Class template:** `template <class T> class Box { void f(); };` with `template <class T> void Box<T>::f()`.
  4. **Class inside a namespace,** defined out of line as `void Ns::Foo::f()` in a `.cpp` that does not reopen the
     namespace.
- **Fixes may touch:**
  - `structural/treesitter_backend.py`: class naming. For shape 1, when the `name` field's text is an
    ALL-CAPS-with-underscores identifier, or is followed by a second identifier before `:` or `{`, take the second;
    check what the tree actually looks like first.
  - `call_sites.py`: the frame's class name (last segment, template arguments stripped).
  - `callresolve._enclosing_classes` (strip `<...>`, use the last `::` segment).
- **Tests:** a fixture per failing shape, asserting that a member call inside the method resolves (`this` tier),
  and the class node's label.

### Task 5: `member:method_not_found` (1704): the method lives in an external base

- **Background.** The receiver's type is one repository class, but neither it nor its repository bases has the
  method. Typically it is inherited from a framework base (`QWidget::update()`, `QObject::deleteLater()`).
- **Change** (`src/cortex/callresolve.py`): when a member call's type resolves to repository class(es), no member
  is found through the repository bases, **and** some class on the inheritance path has a base that is not a
  repository class, mark the edge `metadata.external = "inherited"`, keeping the `name:` target. 0.14.0 Task 3.2
  introduced the `external` metadata for exactly this kind of call. When every base is in the repository, keep
  today's `name:` with no `external` mark.
- **Tests:**
  - `class W : public QWidget { void f() { update(); } }` and `w->update()` from outside: both marked external;
  - a class whose bases are all in the repository and that lacks the method: not marked;
  - the audit's `member:method_not_found` count on the fixture drops to 0;
  - `name_external` and `resolvable_share` (Task 1) move accordingly.

### Task 6: `member:ambiguous` (2238): several candidate classes

- **Background.** The member's declared type, or the enclosing class, names a label shared by several classes
  (same name in different directories or modules), so the resolver keeps `name:`.
- **Change:** narrow the candidate classes with the locality rules already used for free functions. In order:
  1. a class defined in a file the calling file includes, directly or through its header sibling (`impl_siblings`);
  2. the nearest directory (the 0.14.0 longest-common-prefix step).

  Resolve only when exactly one class remains. Reuse `index.prefer` / `index.narrow` (`callresolve.py` around lines
  381–410); do not write a third locality rule.
- **Tests:**
  - two `Foo` classes in `a/` and `b/`, where the caller includes `a/foo.h`: resolves to `a`'s method;
  - neither included, and equally near: stays ambiguous;
  - `tests/test_call_target_invariant.py` stays green.

### Task 7: `member:not_declared` (3316): members the extractor does not record

- **Background.** The enclosing class and its bases declare no member of that name in `member_types`
  (`call_sites.cpp_member_types` → `CppCalls.class_fields`).
- **Investigate first** with fixtures for these declaration shapes. Fix only those that fail on 0.15.2:
  1. **several declarators:** `Foo *m_a, *m_b;`
  2. **qualifiers:** `const Foo *m_a;`, `Foo *const m_a;`, `mutable Foo m_a;`, `static Foo *s_inst;`
  3. **namespaced or nested type:** `ns::Foo *m_a;`, `Outer::Inner m_a;` (record the last segment)
  4. **smart pointer or template:** `QPointer<Foo> m_a;`, `std::unique_ptr<Foo> m_a;`, `QScopedPointer<Foo>
     m_a;`. Record `Foo` (the first template argument when the wrapper is a known pointer wrapper). This also
     covers the 35 `type_wrapped`.
  5. **default member initialiser:** `Foo *m_a = nullptr;`, `Foo m_a{};`
  6. **member declared in a base class that lives in another header:** check that the base walk in
     `callresolve._member_type` reaches it.
  7. **Qt pimpl:** `Q_D(Foo); d->bar();` and `d_ptr->bar()`. `Q_D(Class)` declares `ClassPrivate *const d`. Record
     `d` as `ClassPrivate` in that function's local types (in `call_sites.py`) when `Q_D(X)` appears, and resolve
     against the class `XPrivate` if it exists.
- **Tests:** one fixture per failing shape, asserting the `member_types` entry and that the call resolves.

### Task 8: `chain` (14871): resolve calls on a call's result

- **Background.** `a->b()->c()` and `a.b().c()` are emitted with `receiver_kind = "chain"` and the receiver text,
  and are never resolved. The inner call `a->b()` is emitted as its own edge on the same line.
- **Change** (in `src/cortex/callresolve.py`, as a pass after the existing tiers, so the inner edges are already
  resolved):
  1. **Return types.** For C/C++ `func` symbols, parse the return type from the signature text: the part before the
     function name, with storage and specifier keywords (`static`, `virtual`, `inline`, `constexpr`, `explicit`,
     `const` at the front), `*`, `&` and the `Class::` qualifier stripped. Keep the last identifier segment. Store
     it in the resolver's index, not on nodes (no extraction change). Skip `void` and `auto`, and treat built-in
     types as non-class.
  2. **The chain's receiver.** If the receiver text ends with a call `NAME(...)`, find the edge for callee `NAME`
     from the same source symbol whose `lineno` is the chain edge's `lineno`.
     - If it resolved to one `symbol:`, take that symbol's return type.
     - If the receiver ends with a field (`a->m_x->c()`), use the `member_types` of the receiver's resolved type
       (one level; `this->m_x` and `m_x` as today).
  3. **Resolve.** If the type is one repository class, resolve the callee in it and its bases with the existing
     `declared_type` / `inherited` logic. Record `metadata.resolution = "chain"`.
  4. **Leave it alone otherwise.** If the type is not a repository class, leave the edge `name:`. If that class has
     an external base and no member, Task 5's rule marks it external.
  5. **Ambiguity.** Multiple candidates are kept ambiguous, as in the other tiers.
- **Do not** follow a chain deeper than 4 links. A chain whose inner call is itself a chain resolves only when the
  inner one did; process edges in `lineno`/column order, or iterate twice.
- **Incremental refresh.** `ingest_incremental` re-resolves affected edges. Make sure a changed return type
  re-resolves the chain edges whose inner callee is in the changed file. Extend the "changed labels" set with the
  callees of chains whose inner call targets a changed symbol, or re-resolve all chain edges of a file whose
  callees changed. Add a test: change a return type in one file, run an incremental refresh, and check the chain in
  another file follows.
- **Tests:**
  - `mgr->current()->apply()`, where `Item* Manager::current()` and `void Item::apply()`: resolves to `Item::apply`;
  - `widget.layout()->addWidget()`, where `layout()` returns a framework type: stays `name:` (or is external per
    Task 5);
  - a 3-link chain;
  - an ambiguous inner call: stays `name:`;
  - the incremental test above.
- **Measure** the audit `chain` count before and after on the fixtures and the `--field-shape` repository. There is
  no numeric target: report what moved.

### Task 9: Release 0.16.0

- **Version:** 0.16.0 in every place `git show --stat 16edb4a` shows (`pyproject.toml`, both plugin manifests,
  `marketplace.json`, `src/cortex/__init__.py`, `tests/goldens/mcp/initialize.json` and its assertion in
  `tests/test_golden_mcp_shapes.py`, the README version line).
- **`GRAPH_FORMAT_VERSION` 9:** the comment lists Tasks 4–8's resolver changes. `tests/test_graph_format_upgrade.py`
  stays green.
- **CHANGELOG:** a `## 0.16.0 — <date>` section in the style of 0.15.2. It must contain:
  - the input table and the per-language decision;
  - each task, with its before/after audit numbers;
  - the golden list;
  - the graph format sentence (rebuilt once, in the background from SessionStart);
  - "Not met" (anything dropped, plus JS/Java resolution, deferred);
  - "For the reviewer's next run": after the automatic rebuild, run `audit_unresolved_calls.py <REPO> --all` and send:
    - the per-family line for `c`;
    - the combined `resolvable_share`;
    - the bucket counts for `chain` and the `member:*` buckets.

    The target: `by_family.c.resolvable_share` ≥ 0.6 (expected about 0.63 from accounting alone, more from Phase B).
- **Plan:** set the Status line of this file to "implemented in 0.16.0", with the outcomes.
- **Pre-PR checks**, reporting each result line:
  - the full suite;
  - the regex-fallback set;
  - goldens, actually run;
  - coverage floors;
  - `python evals/run_evals.py` (do not commit `evals/RESULTS.md`);
  - `python -m build --outdir /tmp/cortex-dist .`, then remove `build/` and `*.egg-info`;
  - the suite without `tiktoken` in `/tmp/venv-notok`.

  Known caveats:
  - `tiktoken`'s `o200k_base` encoding cannot be downloaded in this sandbox, so local runs use the stdlib estimate
    while CI uses real counts. Tests that depend on tight budgets must pin the counter with
    `tests.golden_utils.force_stdlib_tokenizer`.
  - The wall-clock test `test_resolving_2000_symbols_stays_under_half_a_second` exceeds its limit only under
    coverage; re-run it without coverage. Phase B adds resolver work, so if that test fails *without* coverage,
    profile and fix it; never raise the limit.
  - The tree must be clean at the end.
