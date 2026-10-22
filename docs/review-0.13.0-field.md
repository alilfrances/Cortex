# Cortex 0.13.0: field review and fix plan (fourth pass)

**Reviewer environment:** private C++/Qt/QML repository, about 3.7k indexed files, 161k nodes and 622k edges. Linux, Python 3.12, without `tiktoken` or `regex`, so the stdlib token estimate was used.

**Method:**
- Plugin versions 0.10.1, 0.11.0, 0.12.1 and 0.13.0 were snapshotted.
- Each version ran against a fresh `CORTEX_DATA_DIR` with a full refresh. Each cold call ran in its own process, with `CORTEX_TOOL_TIMEOUT_SECONDS=30`.
- All runs used a frozen `git clone --local` at one commit. HEAD and the `git status --porcelain` hash were recorded before and after each run.
- Uncapped `dead_code` ran with the timeout at 0.
- Root causes were traced in the 0.13.0 source against the 0.13.0 and 0.12.1 indexes, opened read-only. All `file:line` references are to the 0.13.0 tree.

## 0. Summary

| # | Item | Status in 0.13.0 | Root cause (one line) | Section |
|---|---|---|---|---|
| A | `dead_code` adds 32 false positives; 227 `Ns::f()` call edges lost | **regression** vs 0.12.1 | new `locality()` step 3 keeps only the header's `namespace` node; members live in the `.cpp` one | §A |
| B | `cortex_risk` returns fewer ranked `files` at a larger budget | **defect** in new truncation order | `files` is cut first (detailed) and refilled last; extra budget buys detail, directives and tests instead | §B |
| C | `tests_by_changed_file` gains 2 false mappings | **regression** vs 0.12.1 (changelog says "unchanged") | "production over mock" rewrites ambiguous `new X` calls to the real class; the test target compiles the mock copy | §C |
| D | gate: `inherits` resolved to `symbol:` ≥ 80% | **not met**: 46% (888 / 1,915) | ~980 of 1,027 unresolved are out-of-repo bases (Qt, gtest, std, Qt Quick) counted as `name:`/`module:` instead of `external:`; plus 2 resolver bugs | §D |
| E | `calls` resolution | 27% `symbol:`, no movement from 0.12.1 (~26%) | ≥ 46k of 100.6k `name:` calls are external library calls; anonymous-namespace extractor bug creates fake classes | §E |
| F | gate: T1 second manager `.cpp` + override base `.cpp` packed | **not met** (gold 1/5, same as 0.10–0.12) | seed misses the routing function; overriders are offered but never walked; flat scores tie and are broken by path | §F |
| G | gate: T2/T4 fix-site file packed | **not met** (0/3, 0/2; T3 also 0/3) | binary, saturated name bonus; no length normalisation; hub files spill spans to the 25% cap ×4 | §F |

**What 0.13.0 did fix, verified on the field repository:**
- The `cortex_risk` map and tests at budgets 2000 and 4000 on the pinned 100-commit range: map 39 entries, 10 then 42 of 42 tests.
- `inherits` now ends at the base symbol in `cortex_relations`, `cortex_impact` and `cortex_path`.
- 354 `dead_code` false positives were removed.
- The upgrade refresh from a 0.12.1 index rebuilds fully, takes 66 s and gives identical counts.
- Cold query time is unchanged (13–14 s).

**Process (repeat of the 0.12.1 request, still the main lever).** Every not-met item above passed on `--field-shape` and failed on the field repository. Each failure traces to a property the profile does not have:

| Property | `--field-shape` | Field repository |
|---|---|---|
| Out-of-repo bases (`QObject`, gtest `Test`, QML `Item`) | absent or `module:` only | ~95% of unresolved `inherits` |
| Namespace split across `.h`/`.cpp` (`namespace Ns {` in both) | absent | 5+ namespaces, 227 call edges |
| Header mocks placed *ahead of* the real headers on the test target's include path | absent (mocks only as same-label classes) | yes |
| Hub implementation files of 200–300 KB containing almost every English word in comments and identifiers | absent | 4–6 such files win every query |
| Anonymous `namespace {}` at the top of `.cpp` files | absent | dozens |

Add each of these to `--field-shape` before the corresponding fix is accepted (§H). Release-note gates stay "not met" until the reviewer's run says otherwise.

---

## A. Regression: `Ns::f()` calls to namespace free functions no longer resolve

### Observed
- `dead_code`, full repository with no time cap: 13,242 findings drop to 12,920. 354 were removed, but **32 were added**, all `low`.
- The changelog says "Nothing was added".
- All 32 additions are namespace-scope free functions that other files call as `Ns::f(...)`. Examples: a `toString`/`fromString` pair, a plist-to-XML helper, a version comparator, a layout list, compress and archive helpers.
- `cortex_relations relation=calls direction=in symbol=f` shows the change for one of them:
  - 0.12.1: three callers end at `symbol:lib/ns.cpp:f`, with `resolution: scoped`.
  - 0.13.0: the same three callers end at bare `name:f`, with no `candidates` and no `ambiguous` flag.
- Across all call edges, 244 went from resolved to unresolved between 0.12.1 and 0.13.0. 227 of them are `receiver_kind: scoped`, and every scope label is a namespace opened in both a `.h` and a `.cpp`.
- The other 17 were `declared_type` calls that used to resolve to a mock. That change is intended and correct.

### Root cause (`src/cortex/callresolve.py`)
1. A namespace opened in both files produces two "class" nodes with the same label:
   - one in `ns.h`, spanning the declarations; the header declarations get no symbol nodes;
   - one in `ns.cpp`, containing the `f` definition. That definition has no `qualifier` metadata.
2. `_resolve_scoped` (`:577`) passes both nodes to `prefer()`.
3. In `prefer()` (`:354-361`), neither node is in the caller's file. The caller's include of `ns.h` often stays `module:` (an ambiguous basename), so the include step keeps nothing. Control falls through to the new `locality()`.
4. `locality()` step 3 (`:378`, "declared in a header, not nested in an implementation file") keeps only the `.h` node.
5. `class_members()` (`:215-220`) finds no member `f` in the `.h` node. The fallback `qualified_defs` is only filled from nodes with `qualifier` metadata (`:208-209`), so it is empty too, and the call returns None.
6. In 0.12.1, `prefer()` ended with `return near or candidate_ids`. Both nodes survived and the `.cpp` one matched.

Step 3's rationale ("a .cpp-local class is not visible to any other file") is true for classes and false for namespaces. Namespaces are open, and their members are visible wherever the declaration is.

### Fix
1. In `ClassIndex.__init__` (`:202-209`), when a symbol's container satisfies `is_namespace()` (`:211`), also index it under `qualified_defs[container.label][symbol.label]`. All same-label namespace nodes then share one member table, so the narrowing in `locality()` no longer matters for them. `overrides.find_overriders` benefits from the same index.
2. Defence in depth: in `locality()`, skip steps 3 and 4 when every candidate `is_namespace()`.
3. Report the remaining scoped misses in `scripts/perf/audit_unresolved_calls.py` as their own bucket (`scoped-namespace`). A regression of this kind is then visible in one command.

### Tests
- Repository layout:
  - `lib/ns.h` holds `namespace Ns { int f(); }`.
  - `lib/ns.cpp` defines `int f() {…}` inside `namespace Ns {…}`.
  - `app/caller.cpp`, in another directory, includes `"ns.h"` and calls `Ns::f()`.
  - Assert the target is `symbol:lib/ns.cpp:f` with `resolution: scoped`.
- Variants:
  - the include is indirect;
  - there is a second `ns.h` basename elsewhere, so the include stays `module:`;
  - the namespace is nested, `A::B::f()`.
- `dead_code` on the same repository must not report `f`.
- **Invariant test, the important one.** For a fixed fixture set, `resolve_call_endpoints` in release N must not turn any 0.12.1 `symbol:` call target into `name:`, unless the old target was a mock and the new candidate is production. The check is a frozen list of (edge, expected target).

### Acceptance (field repository)
- 0 call edges go from `symbol:` to `name:` versus 0.12.1, apart from the 17 mock-to-production cases.
- `dead_code` added-versus-0.12.1 equals 0.

---

## B. Defect: `cortex_risk` returns fewer ranked files at a larger budget

### Observed
Ranked `files` count at budget 2000 and 4000:

| range | files at 2000 | files at 4000 | form at 4000 |
|---|---|---|---|
| 3 commits | 17 | 15 | detailed (15 keys per entry) |
| 10 commits | 22 | 10 | detailed |
| 30 commits | 10 | 31 | compact |
| 100 commits | 12 | 10 | compact |

At 2000 every entry is compact and `files.detail` is listed in `omitted_fields`. At 4000 on the small ranges, the entries carry the full detail: `components`, `hotspot`, `fan_in`, `churn` and `missing_cochange`. The `missing_qt_*` and `missing_cochange(s)` buckets are also kept.

### Root cause (`src/cortex/risk.py`, `truncate_risk_result`)
1. `:1134-1135` cuts `files` to `FILES_FLOOR` while the entries are still detailed.
2. `:1136-1141` drops `_RISK_DROP_ORDER` buckets one at a time and stops as soon as the result fits.
3. `:1145-1148` compacts `files` (`files.detail`) only if the result is *still* over budget at that point.
4. `:1198-1202` gives leftover room back to `files`, but in whatever form survived step 3.

This gives two mechanisms:
- **Small ranges:** at 4000, dropping buckets alone makes the result fit, so `files` stays detailed and stays at its floor. The refill adds nothing, because a detailed entry costs about 110 tokens and a compact one about 23. At 4000 on the 3-commit range, compacting all 17 files costs about 400 tokens. That is less than the detail and buckets kept instead.
- **Large ranges:** `files` sits at its floor from step 1. `shorten("directives")` (`:1152`) and the `tests_to_run` step (`:1175`) then use all the remaining room: 35 directives (about 720 tokens) and 42 tests (about 1,200 tokens). The refill at `:1199` finds no room left. At 2000, directives are cut to 0 and the small remainder gives 2 more files.

`files`, which the changelog calls the tool's main output, is cut first and refilled last. Above the floor, its count is whatever room happens to be left, and that leftover does not grow with the budget.

### Fix
1. **Count before detail.** Move the `files.detail` compaction ahead of the first `shorten("files")`, applied whenever `files` would otherwise be cut. Detail is restored at the end only if every ranked file fits compact and there is room left.
2. **Refill `files` (compact) right after the bucket drops**, before `directives` and `tests_to_run` are shortened. Directives and tests then size against the room left after `files`. If directives are meant to outrank `files` above the floor, state that order in the docstring. Even then, keep the invariant below for the form: a larger budget never returns detailed entries when it returns fewer of them.
3. **Monotonicity invariant, as a test rather than an example.** In `tests/test_risk_truncate.py`, sweep `budget in range(500, 20001, 250)` over `_large_diff_result` and a new small-range fixture with heavy per-file detail. For every b1 < b2, assert:
   - `len(files)` does not decrease;
   - the number of changed files with a map entry does not decrease;
   - the per-file test cap does not decrease;
   - `len(tests_to_run)` does not decrease;
   - `files.detail` omitted at b2 implies it is omitted at b1.

The small-range fixture must have detail large enough that compacting is worth more than one detailed row. Otherwise it cannot reproduce the 3-commit case.

### Acceptance (field repository)
- On the 3-, 10-, 30- and 100-commit ranges, the `files` count at 4000 is at least the count at 2000.
- The 100-commit range keeps map 39 and 42 tests at 4000.

---

## C. Regression: `tests_by_changed_file` maps changed headers to tests that never compile them

### Observed
On the 100-commit range, 0.13.0 maps two changed step headers to tests that 0.12.1 did not map:
- `src/steps/stepA.h` maps to two operation tests;
- `src/steps/stepB.h` maps to one operation test.

`tests_to_run` is unchanged, because those tests were already selected through other files. The changelog says the map is unchanged; that held on the profile but does not hold here.

### Root cause
- The production sources build `new StepA(...)`. In 0.12.1 those `calls` edges were `name:`, `ambiguous: true`, with two candidates:
  - the real `src/steps/stepA.h`;
  - a mock at `tests/<suite>/header-mocks/steps/stepA.h`.
- The 0.13 `prefer()` ladder, step 2 ("production over mock"), rewrites them to the real class.
- `impacted_tests.select_tests_with_coverage` (`impacted_tests.py:584-604`) seeds the changed header's symbols. `_structural` then walks reverse `calls` edges (`:469-489`) at depth 3, within `STRUCTURAL_MAX_DEPTH`: real header ← production step function ← production operation ← test case.
- **But the test target's include path lists the `header-mocks` directory ahead of the source tree.** The production `.cpp` compiled into the test binary includes the mock copy. A change to the real header cannot affect that test, so the mapping is a false positive.

### Fix
1. Ingest test-target include directories from the build system. The CMake scanner already emits `target:` nodes; extend it to record `target_include_directories` and `include_directories` for test targets.
2. When choosing between a real class and a same-label class under a mock directory, apply the order per *build context*:
   - for edges that come from a source compiled into a test target whose include path puts the mock directory first, prefer the mock;
   - otherwise keep production first.
3. Fallback when no build data is available: keep the 0.12.1 behaviour of leaving the edge `name:` and ambiguous, for the impacted-tests walk only. The call graph and `dead_code` can still use the production preference. In effect, `impacted_tests` should not walk an edge whose resolution came from `locality()` step 2.

### Test
- `src/ops.cpp` calls `new Step`.
- `src/steps/step.h` and `tests/header-mocks/steps/step.h` both define `Step`.
- `tests/CMakeLists.txt` puts `header-mocks` ahead of `src`.
- `tests/opstest.cpp` drives `ops.cpp`.
- Assert that a change to `src/steps/step.h` does not map to `opstest.cpp`, and that a change to `src/ops.cpp` does.

---

## D. Gate not met: `inherits` resolved to `symbol:` ≥ 80% (measured 46%)

### Observed
`cortex_status` (detailed) on the field repository reports `inherits` as symbol 888, name 587 and module 440.

### Root cause: mostly accounting, plus two resolver bugs
- **`module:` (440): all QML.** About 430 of them derive from Qt Quick or Controls built-ins such as `Item`, `Column`, `Rectangle`, `QtObject` and `ColumnLayout`. Resolution is partitioned by language family (`callresolve.py:822-832`), so a QML base can only match a QML component in the repository. The remaining ~10 are:
  - a vendor controls module that is imported but not in the repository;
  - `WebEngineView`;
  - a base reached through an import alias (`Controls.TextField`);
  - one base defined nowhere.
- **`name:` (587):**

| Bucket | Count | Shape |
|---|---|---|
| Qt classes | 486 | `QObject` 406, `QThread` 36, `QAbstractListModel` 12, … |
| gtest fixtures | 57 | `Test`, `testing::Test` |
| Ambiguous between the real class and a mock | 35 | one step base class accounts for 34 |
| std | 5 | `runtime_error`, `exception`, `variant<…>` |
| Base not in the index | 4 | |

- **Ambiguous step base (34):**
  - The mock header's `#include "stepbase.h"` stays `module:`, because that basename exists twice.
  - `locality()` step 2 is skipped, because the caller is itself under `tests/`.
  - Step 4 requires exactly the same directory. The caller is in `header-mocks/steps/` and the mock base is in `header-mocks/`.

### Fix
1. **Classify out-of-repo bases as `external:`.**
   - In `_resolve_inherits_into` (`callresolve.py:759`): when no class in the caller's language family has the label, and the label matches an external-type rule, rewrite the target to `external:<label>`. The same applies to a label with a known external prefix (`Q[A-Z]`, `std::`, `testing::`) or a QML type imported from a module outside the repository. Calls already use the `external` bucket.
   - Report `inherits.external` in `cortex_status.resolution`.
   - Define the gate as `symbol / (symbol + name + module)`, with externals excluded from the denominator, and state that in the changelog. On the field repository this gives about 888 / (888 + ~50) ≈ 95%.
2. **Resolve quoted includes relative to the including file first.** Try its own directory, then its parents, before the basename lookup. That is the compiler's own rule for `#include "x.h"`. This turns the duplicate-basename `module:` includes into `file:` edges, and the 34 step-base cases then resolve through the include step of `prefer()`.
3. **Change `locality()` step 4** from "same directory" to "longest common directory prefix with the caller". This keeps `header-mocks/steps/x.h` with `header-mocks/base.h` rather than `src/base.h`.

### Tests
- A class deriving from `QObject` resolves to `external:QObject`.
- A QML file whose root is `Item`, with no `Item.qml` in the repository, resolves to `external:Item`.
- A class whose base has a mock twin in a sibling directory, where the include is quoted and the basename is duplicated, resolves to the twin in the nearest directory.
- Add these shapes to `--field-shape`: about 25% of classes derive from an external base, and one mock tree has the mock directory one level up.

---

## E. `calls` resolution stays at 27% on the field repository (98% on the profile)

### Observed
`calls` resolve as symbol 41,928, name 100,603, external 11,726 and file 102. These are exact counts over all 100.6k unresolved calls, not a sample:

| Bucket | Count | Fix |
|---|---|---|
| Free function not in the repo (libc, std, third-party libraries) | 20,800 | `external` |
| Receiver typed as a Qt/std class | 19,882 | `external` |
| Chained `a.b().c()` | 14,871 | out of scope; count separately |
| Variable receiver of unknown type (9.5k are `m_*` members) | 13,650 | member-type inference from the class header (follow-up) |
| Not C++ (JS 9.3k, Java 4.3k, Python 0.2k) | 13,858 | report per language |
| Ambiguous | 3,815 | 1,367 are the extractor bug below |
| `Qt::`/`QString::`-style static calls | 3,406 | `external` |
| Constructor-like free calls (`QVariantMap(...)`) | 2,901 | `external` / type |
| Qt macros and globals (`qWarning`, `QStringLiteral`, `q*`) | 1,999 | `external` |
| Repository namespace scope (§A plus older misses) | 236 | §A |
| Other | ~2.9k | |

### Root causes
1. **Out-of-repo calls are counted as `name:`.** At least 46k of the 100.6k are Qt, std or C library calls. They cannot resolve, and they inflate the "unresolved" figure the gates are measured on.
2. **Extractor bug.**
   - `structural/treesitter_backend.py:90-97`, `_name_for_node`, falls back to the first identifier in the body when a node has no `name` field.
   - An anonymous `namespace { … }` is therefore labelled after the first type it contains.
   - On the field repository this yields 38 fake `class QString` nodes (plus `QStringList`, `QMap`, …).
   - They create 1,367 of the 3,815 ambiguous calls (`QString` 971, `QStringList` 396), and probably more false `dead_code` and `impact` edges.

### Fix
1. Add an external-name classifier to the call resolver: Q-prefixed types, `std::`, libc names, `q*`/`Q_*` macros, and receivers whose declared type is external. Emit `external:` instead of `name:`. Report `calls.external` separately, and gate on the in-repo share.
2. In `_name_for_node`, do not fall back to body identifiers for `namespace_definition`. Label the node `(anonymous)` and do not register it as a class.
   - Test: `namespace { QString f(); }` produces no `QString` class node.
   - Golden: the class-node count of the regex and tree-sitter backends must agree on that fixture.
3. Report the remaining buckets above through `audit_unresolved_calls.py` with the field repository's statistics, and extend `--field-shape` accordingly. A target of 66% `name:` is more honest than the profile's 11%.

### Acceptance
- In-repo `calls` resolve to `symbol:` ≥ 60% (`symbol / (symbol + name − external-classified)`).
- 0 class nodes named after a Qt/std type.

---

## F. Gates not met: T1–T4 query recall

### Observed (gold files found, field repository, cold, 30 s budget)

| task | gold | 0.10.1 | 0.11.0 | 0.12.1 | 0.13.0 |
|---|---|---|---|---|---|
| T1 re-attach of a peripheral demotes its entry, then removes it | 5 | 1 | 1 | 1 | 1 |
| T2 reconnect wait times out after a remote-initiated reset | 3 | 0 | 0 | 0 | 0 |
| T3 slow step when an optional monitoring setting is on | 3 | 0 | 0 | 0 | 0 |
| T4 transfer fails after a short socket read in a client | 2 | 0 | 0 | 0 | 0 |

0.13.0's `generate_bundle` was re-run against the read-only index, and it reproduces the recorded bundles exactly. The ranked candidates were then dumped before packing. In every task, 4–6 hub files win: a 200–300 KB device class implementation, a device header, a workflow item, an Android-side operation and a shared header.

### Root causes (`src/cortex/bundle.py`)
1. **The name bonus is binary and saturated** (`:903-907` for files, `:1118` for spans).
   - Any task term in the stem, the B.6 `path_aliases`, or *any symbol name in the file* adds `NAME_MATCH_BONUS` (+100). It is not weighted by IDF and does not count matches.
   - Every hub collects it through a common word such as `device` (weight 0.86) or `info` (1.00).
   - The T2 fix-site file matches three rare stem terms (weights 3.57, 2.79 and 2.01) and also gets exactly +100.
   - B.6 aliases are correct for the gold files (`<prefix> wait <noun> max`, `<prefix> get <noun> info step`) but feed only this saturated flag.
2. **No length normalisation.** `_score_text` (`:687-698`) sums IDF over the *set* overlap with the whole file, so a 250 KB hub contains nearly every task word.
   - T2: hub text score 503 and 456, versus 212 for the gold file.
   - bm25 is length-normalised but enters only through RRF ×40 (`:137`, `:996`), worth ≤ 3 points.
   - Spans also inherit 0.25 × the file score (`:1110`, `:1124`): +113 to +151 per span in a hub, +40 to +75 in a gold file.
3. **Hub files spill their spans at the file's score** (`:1366-1382`).
   - A high-scoring file that does not fit whole packs all its spans up to `FILE_SPAN_SHARE` = 25% (`:95`).
   - Four hubs × 25% = 100% of the budget, so B.7 caps nothing when four hubs lead.
   - Every failing bundle is four hubs plus one or two small items.
4. **Callee scores are flat; ties are broken by path.**
   - Every hop-1 callee or overrider gets exactly `seed × 0.9` (`:283-288`). B.3 relevance only *selects* callees (`:297-301`, see the changelog deviation) and does not scale their score.
   - Relevance is almost always 1.0, because `device` occurs in nearly every body (`:1202`).
   - One-line virtuals with overriders are still followed at `RELEVANCE_FLOOR` (`:1201`).
   - Ties break on `(-score, path, item_id)` (`:1280`).
   - Field sizes of the ties: T2 has 121 candidates at exactly 457.46, T4 has 91 at 336.38, T1 has 93 at 252.11 and 43 at 337.34.
   - In T2 the seed *does* reach the fix-site file's methods. They rank 108th and 109th of 121 tied callees and lose the alphabetical tie to a file under `a…/`.
5. **Constructors seed; `new X` targets the class.**
   - `_select_seeds` (`:318-325`) drops class nodes but not constructors. In T1 the top seed is a 329-line manager constructor with 117 resolved callees, which floods accessors and unrelated singletons.
   - `new X(...)` resolves to the class node in the header, not to the constructor in the `.cpp`.
6. **Overriders are offered but never walked** (`:286-288`). An overrider is `offer()`ed but not enqueued, so its callees are unreachable.
   - The T1 route is: manager function → pure virtual on a base manager (resolved) → overrider in the second manager `.cpp` → accessor-base method (resolved).
   - It fails twice:
     - the routing function ranks 15th among spans, because its only task term is the generic one, so it is not one of the 6 seeds;
     - even as a seed, the overrider would be offered without being expanded.
7. **Override noise.**
   - The override ranker (`:1206-1217`) demotes only auxiliary paths.
   - It has no handling for platform variants (`_win`, `_mac`, `_linux`), so overriders of the base's one-line virtuals (`canBeX`, `usesY`) in unrelated platform files are packed in T1.
8. **Tokeniser noise.**
   - `_split_identifier` (`:615-617`) splits `iOS` into `os` (weight 1.87), and `re-enumerates` produces `re`.
   - `STOPWORDS` (`:28-32`) lacks instruction words: find, then, very, all, after, because, every, too, shows, come, back, during, handles, path.
   - There is no stemming (install/installs, remove/removed, report/reported).
   - A CamelCase compound in the task (for example `GizmoPods`) splits into two terms, while the code's singular one-token name (`Gizmopod`) matches neither.
9. **Vocabulary gap, T2.** The task says "USB", while the Windows fix site is named after the OS device-event API. Its stem contains `_`, so `pathalias` does not split it (`pathalias.py:47-49`).

### Fixes, ranked by expected impact
1. **Make the name bonus IDF-weighted and proportional** (`:907`, `:1118`): `NAME_MATCH_BONUS × Σ w(t ∈ stem ∪ aliases) / Σ w(T)`. Symbol-name hits count only for that symbol's span, not the whole file. *Expected:* T2, T3 and T4 fix-site files rank above the hubs on their own.
2. **Length-normalise the file text score.** Use bm25 or term density at `:911-917` instead of the set-overlap IDF sum, and cap the `file_share` that spans inherit (`:1110`).
3. **Spans compete on their own score.** At `:1369-1382`, do not spill a file's spans at the file's score. Scale `FILE_SPAN_SHARE` to `max(0.1, 1 / n_matching_files)`, so four hubs cannot take 100%.
4. **Callees:**
   - scale the callee score by IDF-weighted relevance (the plan's 0.25–1 range, against the companion fixture's regression);
   - break ties on relevance, then hop, then path;
   - drop constructor seeds;
   - map `new X` to the `.cpp` constructor;
   - stop following one-line virtuals at the floor unless the name carries a task term.
5. **Walk overriders.** Enqueue overriders in the 0-1 BFS (`:286-288`) within the hop budget, and seed on IDF-weighted span score rather than raw score. Raise `CALLEE_SEEDS` from 6, or add a second pass seeded from the best *function* span of each of the top 5 files.
6. **Tokeniser:**
   - keep known short tokens whole (`ios`, `usb`);
   - drop split parts shorter than 3 characters;
   - add instruction words to `STOPWORDS`;
   - apply light suffix stemming (`-s`, `-es`, `-ed`, `-ing`) to both query and index terms;
   - singularise CamelCase parts.
7. **Platform-variant demotion.** Demote overriders and files with a `_win`, `_mac` or `_linux` suffix unless the task names the platform. Allow an optional config map from product names to platforms.
8. **`pathalias`:** split `_` stems as well (`pathalias.py:47-49`).

### Synthetic fixtures (no field code; add to `evals/` and `--field-shape`)
- **Hub flood.**
  - Four `core*.cpp` files of about 3k lines, with comments using every common English word and hundreds of `deviceX()` methods.
  - Plus a small `gadgetwaitchargemax.cpp` declaring `GadgetWaitChargeMax`.
  - Task: "slow when the charge max wait setting is on".
  - The gold file must be packed at budgets 900 and 4000. This fails on 0.13.0 through causes 1–3.
- **Tie-break.** A seed calls 30 cross-file one-line virtuals under `a/…` and one relevant callee in `z/ops/resetop.cpp`. `resetop.cpp` must be packed.
- **Override chain (T1 shape).**
  - `Manager::onReattach` calls `base->createForCompanion()`, a pure virtual in `basemanager.h`.
  - `SecondManager::createForCompanion`, in `secondmanager.cpp`, calls `Accessory::assignCompanion`.
  - Gold: `secondmanager.cpp` and `accessory.cpp`, with `why: override` then `calls`.
- **Constructor flood.** A 300-line hub constructor that calls 100 resolved helpers. No helper may be packed unless its name carries a task term.
- **Vocabulary.**
  - Task: "Gizmos replug removed on iOS re-enumerates".
  - Code uses `Gizmo`, `reconnect` and `remove`.
  - Assert there is no `os` or `re` term, and that `gizmo` matches.

### Acceptance (field repository, reviewer run)
- T2 and T4: the fix-site file is packed (≥ 1/3 and ≥ 1/2). T3: ≥ 1/3. T1: ≥ 2/5, including the second manager `.cpp`.
- No task loses a gold file versus 0.13.0.
- No more than 25% of the bundle tokens go to files with no IDF-weighted stem or alias hit.
- Cold time is within +20% of 13 s.

---

## G. Smaller items
- **`cortex_path` with a bare symbol label is always ambiguous** on class names that repeat across files, for example a namespace-local helper class and the real class. Return the best candidate by `locality()` with an `alternatives` list instead of only `ambiguous`.
- **`cortex_risk` on a 300-commit range** exceeds a 30 s budget (85 s, the same in 0.12.1). That is not a regression, but it still returns no partial payload. Return the ranked `files` and `uncovered_changed_files` computed so far, with `partial: true`.
- **`cortex_overview` / `dead_code` under a 30 s budget** stop at about 7k of 156k candidates. Resume works, but a full repo-wide `dead_code` takes about 200 s uncapped. Report `progress` in the overview text, so callers know to resume.

## H. Release checklist for 0.13.1 / 0.14
1. Add to `--field-shape`, with each property pinned in `tests/test_synthetic_field_shape.py`:
   - a namespace split across `.h`/`.cpp`;
   - external bases (25% of classes);
   - QML roots on built-ins;
   - header-mocks ahead of the source tree on a test target's include path;
   - anonymous namespaces;
   - four hub files of at least 3k lines;
   - the T1 override chain.
2. Add the invariant tests:
   - no `symbol:` to `name:` call regressions (§A);
   - risk monotonicity across budgets (§B);
   - every collection key reducible (already exists).
3. Gates in the changelog are computed with external targets excluded from the denominator, and named as such.
4. Have the reviewer run the release candidate on the field repository. The script and pinned ranges are unchanged from the 0.12.1 plan. Items stay "not met" until that run reports otherwise.
