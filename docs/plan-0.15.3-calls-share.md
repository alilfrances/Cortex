# Plan: Cortex 0.15.3: a calls share that counts what can resolve, per language

Status: **planned**, not implemented.
Input: the reviewer's `scripts/perf/audit_unresolved_calls.py <REPO> --all` table on 0.15.2 (field index):

| line | numbers |
|---|---|
| calls by target | symbol 43414, name 99117, external 11726 |
| top unresolved buckets | external 34406, free_not_in_repo 25212, chain 14871, not_cpp_js 9335, not_cpp_java 4241 |
| member buckets | no_enclosing_class 1426, not_declared 3316, type_external 473, type_wrapped 35, ambiguous 2238, method_not_found 1704 |

Base: `main` at 0.15.2 (`16edb4a`). Target: **0.15.3**. **No graph format change:** nothing here changes what
extraction or resolution writes. The new classification is computed when the share is reported.

The rules of `docs/plan-0.15-field-review.md` §0 apply unchanged: setup, checks, goldens, failing test first, one
commit per task, a `CHANGELOG.md` bullet per task, and no model names. Work on branch `cortex-0.15.3`.

---

## 1. Verdict

- **Where 0.4 comes from.** The reviewer's 0.4 is `in_repo_symbol_share` from `CortexStore.resolution_counts`
  (`src/cortex/store.py`, around line 1388): `symbol / (symbol + name - name_external)` = 43414 / (43414 + 99117 −
  34406) = **0.402**.
- **It counts calls that can never resolve.** `free_not_in_repo` (25212) are plain calls (`f()`, no receiver, no
  scope) to a name that **no symbol in the repository has**: `qDebug()`, `qMax()`, C library functions,
  moc-generated code. They cannot resolve to repository code, but they stay in the denominator. Excluding them gives
  43414 / 82913 = **0.524**.
- **It mixes languages.** JS and Java resolution are deferred, yet `not_cpp_js` 9335 and `not_cpp_java` 4241 sit in
  the same denominator. On C/C++ alone the share is about **0.63**.
- **Decision (owner):** the calls target becomes **per language**:
  - C/C++ ≥ 0.6;
  - JS and Java are reported without a target until their resolution work is planned;
  - the combined figure stays as information.
- **Not in this release:** resolving chained calls (`a->b()->c()`, 14871, the largest real gap) and the `member:*`
  buckets (9192 in total). They change resolver output, so they need a graph format change and a minor release
  (0.16).
- **The no-data guess would have been wrong.** `type_wrapped` is 35, so teaching the resolver about template
  wrappers would have gained almost nothing.

---

## 2. Tasks (in order, one commit each)

### Task 1: count calls to names defined nowhere in the repository as out-of-repo

- **File:** `src/cortex/store.py`, `resolution_counts`.
- **Rule** (mirrors the audit's `free_not_in_repo`, `scripts/perf/audit_unresolved_calls.py` `classify`). A `calls`
  edge is **not in repo** when all of these hold:
  - its target starts with `name:`;
  - it has no `metadata.external` and no `metadata.ambiguous`;
  - it has no receiver (`metadata.receiver_kind` absent or `"none"`);
  - it has no scope (`metadata.scope`, `metadata.scope_label` or whatever field the audit's `_scope_label` reads;
    use the same fields);
  - it has no `metadata.receiver_type`;
  - its callee (`metadata.callee`, or the target label after `name:`) is **not the label of any symbol node** of
    the repository (`graph_nodes.granularity = 'symbol'`).

  The "any symbol node" test is deliberately wider than the audit's free-function index. A plain call that names a
  method or class of the repository (an in-class call, a constructor call) stays in-repo, so the count can only be
  conservative. The rule applies to every language family.
- **Output:** keep every existing key and value unchanged. `in_repo_symbol_share` keeps its formula, for continuity
  with earlier reviews. Add to the `calls` bucket:
  - `name_not_in_repo`: the count above;
  - `resolvable_share`: `symbol / (symbol + name - name_external - name_not_in_repo)` to 4 places, `None` when the
    denominator is 0.

  Do the work in SQL plus one pass in Python over the `name:` calls edges, using `json_extract` for the few metadata
  fields and a set of symbol labels from one `SELECT DISTINCT label`. Never load the whole graph:
  `resolution_counts` serves `cortex_status`, which must stay fast.
- **Tests** (`tests/test_mcp_status.py`, or the file that covers `resolution_counts`; find it with
  `grep -rn resolution_counts tests`), on a small hand-written C++ repository:
  - a plain call to a function defined in the repository: stays in `name` (or resolves) and is not counted;
  - a plain call to `qDebug()` that nothing defines: counted in `name_not_in_repo`;
  - a member call `obj.undefinedMethod()`: not counted (it has a receiver);
  - a call `Ns::f()` with a scope: not counted;
  - a plain call whose name matches a method of a repository class: not counted;
  - `resolvable_share` equals the formula; `in_repo_symbol_share` is unchanged from before the task.

  Write the test first and show it fails on the unchanged code.
- **Performance check:** time `resolution_counts` on `python scripts/perf/synthetic_repo.py /tmp/field-repo --modules
  40 --commits 120 --field-shape` (indexed) before and after. Report both numbers. It must stay under 1 s there, or
  under 2× its old time, whichever is larger. If it does not, move the label set to a temporary table and join
  instead of filtering in Python.

### Task 2: the share per language family

- **File:** `src/cortex/store.py`, `resolution_counts` (it already computes `name_by_family` with
  `callresolve.language_family` on `metadata.source_file`).
- **Change:** add `calls.by_family`: for each family that has calls edges (`c`, `js`, `java`, `py`, and any other
  family that occurs), the counts `symbol`, `name`, `name_external`, `name_not_in_repo` and `resolvable_share`, using
  Task 1's rules per family. A family's `symbol` count is its resolved calls edges by the family of their
  `source_file`. Keep `name_by_family` as is.
- **`cortex_status`:** check how `src/cortex/mcp/tools.py` (around line 2873) passes `resolution`. Update the
  `cortex_status` tool description (around line 251, which mentions `in_repo_symbol_share`) in one sentence, so it
  names `resolvable_share` and `by_family`. Keep the description under its current length plus about 200
  characters.
- **README:** in the `cortex_status` section, add a short paragraph:
  - the three figures (`in_repo_symbol_share`, `resolvable_share`, `by_family.<family>.resolvable_share`);
  - that a plain call to a name no repository symbol has counts as out-of-repo;
  - that the calls target is read per family: C/C++ ≥ 0.6; JS/Java reported without a target for now.
- **Tests:** a mixed repository (one `.cpp` and one `.js` file with resolved and unresolved calls). Assert per-family
  counts, that the family `symbol` counts sum to the overall `symbol`, that the family `name` counts sum to the
  overall `name`, and the per-family `resolvable_share`.
- **Goldens:** `tests/goldens/mcp/tools_list.json` (description) and any `cortex_status` payload or shape golden
  that includes `resolution` change. Regenerate, review that only the new keys and the description changed, and
  list them.

### Task 3: the audit prints the same figures

- **File:** `scripts/perf/audit_unresolved_calls.py`.
- **Change:** after its existing summary, print one line per family with `symbol`, `name`, `name_external`,
  `name_not_in_repo` and `resolvable_share`, taken from `CortexStore.resolution_counts` (no second
  implementation). With `--json`, if the script has it, include them under a `resolution` key.
- **Test** (`tests/test_audit_unresolved_calls.py`): the printed per-family line matches `resolution_counts` on the
  test repository.

### Task 4: Release 0.15.3

- **Version:** 0.15.3 in every place `git show --stat 16edb4a` / `b7dd89e` show (`pyproject.toml`, both plugin
  manifests, `marketplace.json`, `src/cortex/__init__.py`, `tests/goldens/mcp/initialize.json` and its assertion in
  `tests/test_golden_mcp_shapes.py`, the README version line).
- **CHANGELOG:** a `## 0.15.3 — <date>` section in the style of 0.15.2. It must contain:
  - the input table;
  - each task, with the golden list;
  - "no graph format change";
  - "Not met / not done": chained calls and the `member:*` buckets (resolver work, 0.16, needs a format change);
  - "For the reviewer's next run": `cortex_status` (or the audit) on the field index; send `by_family.c` (`symbol`,
    `name`, `name_external`, `name_not_in_repo`, `resolvable_share`) and the combined `resolvable_share`. The
    expectation from the table is roughly 0.52 combined and 0.63 for C/C++, to be confirmed.
- **Plan:** set the Status line of this file to "implemented in 0.15.3", with the outcomes.
- **Pre-PR checks** (as in 0.15.2), reporting each result line:
  - the full suite;
  - the regex-fallback set;
  - goldens, actually run;
  - coverage floors;
  - `python evals/run_evals.py` (do not commit `evals/RESULTS.md`);
  - `python -m build --outdir /tmp/cortex-dist .`, then remove `build/` and `*.egg-info`;
  - the suite without `tiktoken` in `/tmp/venv-notok`.

  Known caveats:
  - `tiktoken`'s `o200k_base` encoding cannot be downloaded in this sandbox (the proxy blocks
    `openaipublic.blob.core.windows.net`), so local runs always use the stdlib estimate, while CI uses real
    `tiktoken` counts. Any test whose packing depends on tight budgets must pin the counter with
    `tests.golden_utils.force_stdlib_tokenizer`.
  - The wall-clock test `test_resolving_2000_symbols_stays_under_half_a_second` exceeds its limit only under
    coverage.
  - The tree must be clean at the end.
