# Plan: Cortex 0.15.1 — follow-up to the 0.15.0 field run

Status: **planned**, not implemented.
Input: the reviewer's findings on 0.15.0:
1. Call expansion is missing from the packed `cortex_query` bundle on the §1.4 task.
2. Recall is flat, and no metric scores what the packer chose.
3. Six `typedef struct {…} Name;` types are reported dead although they are used as types.
4. `calls` resolution share is 0.4, against a 0.6 target.
5. `cortex-explorer` lists `Grep`/`Glob`, "which don't exist on native builds".

Base: `main` at 0.15.0 (`d066947`). Target: **0.15.1**. No graph format change: nothing here changes extraction or
resolution output.

The rules of `docs/plan-0.15-field-review.md` §0 apply unchanged: setup, checks before every commit, goldens,
eval gate, failing test first, one commit per task, a `CHANGELOG.md` bullet per task, and no model names. Read
that section first. Work on branch `cortex-0.15.1`.

---

## 1. Verdict

| # | Finding | Verdict | Plan |
|---|---|---|---|
| 1 | Call expansion missing from the packed bundle | **Real; cause not reproduced.** 0.15.0 Tasks 4.2/4.3 fix the reproduced shape: 0 → 3 call/override items in every mode. Synthetic variants also pack all callees, and so does the MCP `cortex_query` path: 6 or 30 QML screens; screens large enough to fill 8000 tokens; screens rooted on a repo base component; same-file QML helper calls. Reading the code gives two gaps that fit the field report. (a) `reaches` accepts any outgoing `calls` edge, and a QML same-file call resolves to an `external:qml:<same file>:fn` symbol node. So UI spans can still pass the 0.15.0 check and take seed slots, while the walk from them finds nothing in another file. (b) Callees score from their seed (×0.9 per hop, ×0.85–1 relevance), so enough keyword items above that can push every computed callee out of the packed bundle ("missing from the packed bundle"). Nothing in the output tells (a) from (b), or either from a third cause. | Tasks 1 (diagnostics), 3 (gap a), 4 (gap b) |
| 2 | Flat recall; no packing metric | Same root as 1. The evals score file recall/precision only. | Task 2 |
| 3 | `typedef struct` reported dead | **Confirmed, not a 0.15 regression.** On 0.15.0, `typedef struct {…} Rec;` used as `Rec r;` is reported `low` ("grep refs in code/config"), exactly like a named `struct Named` that has always been reported. Using a type creates no graph edge, and 0.15.0 Task 1.4 credits only classes with a called member. Before 0.15 these six nodes carried a fake label (Task 2.1), so they look new. | Task 5 |
| 4 | `calls` share 0.4 vs 0.6 | **Not a regression** (0.14.0: 38.1%). 0.15.0 shipped the audit that says why `receiver_member` joins fail. | Not in 0.15.1. Needs the reviewer's `scripts/perf/audit_unresolved_calls.py REPO --all` table. |
| 5 | `Grep`/`Glob` "don't exist" | **Incorrect for this agent.** Claude Code's tools reference ("Glob tool behavior") says native macOS/Linux/WSL builds leave Glob and Grep out by default, and that they come back when "a subagent lists `Glob` or `Grep` in its `tools` field and leaves out `Bash`", which is exactly `cortex-explorer`'s list. | Task 6 documents it; no tool-list change. |

---

## 2. Tasks (in order, one commit each)

### Task 1 — Call-expansion diagnostics in the bundle (finding 1)

- **Files:** `src/cortex/bundle.py`, `src/cortex/models.py` (if `RetrievalBundle` needs a field),
  `src/cortex/mcp/tools.py` (`_call_query`, `_concise_query_bundle`).
- **Change:** `generate_bundle` records an `expansion` dict and returns it in the bundle JSON. Detailed
  `cortex_query` output carries it in full; concise output carries a one-line summary string under
  `expansion_summary`, e.g. `"seeds 6 (cross-file 1), callees 3, packed 3"`. Contents:
  - `seeds`: a list of `{"id", "path", "score"}` for the chosen seeds.
  - `seed_refusals`: counts by reason (`class`, `constructor`, `trivial`, `no_reach`), so the reviewer sees why
    the large C++ file's spans did or did not seed.
  - `callees_computed`: `len(callees)` from `_callee_scores`; `callee_files`: distinct files among them.
  - `callees_packed`: how many computed callees ended in the packed bundle, as their own span or through their
    owner file packed whole (the `call_expansion` route on the file item counts).
  - `callees_dropped`: counts by reason, recorded in `pack()`:
    - `budget`: did not fit the remaining budget or `item_cap`;
    - `file_cap`: `over_file_cap`;
    - `inside_selected_span`;
    - `not_reached`: the loop ended or the budget was exhausted before the item's turn.

  When no symbol has resolved calls (`links.calls_from` and `links.bases_of` empty), emit
  `{"seeds": [], "callees_computed": 0, ...}` with the same keys, so the shape is stable.
- **Cost:** counters only, with no extra pass over the graph. `tests/test_deadline.py` and the bundle timing
  tests must stay green.
- **Tests** (`tests/test_bundle_call_expansion.py`):
  - on the companion fixture, `expansion.callees_computed >= 1` and `callees_packed >= 1`;
  - on `field_seed_markup_repo` (0.15.0 Task 4.1), the seed list contains a `src/itemmodel.cpp` symbol;
  - a repo with no resolved calls gives the empty shape;
  - concise `cortex_query` carries `expansion_summary`.
- **Goldens:** `cortex_query` payload/shape goldens change (one new key). Regenerate, review that only that key
  changed, and list them.

### Task 2 — Packing-quality metrics in the evals (finding 2)

- **Files:** `evals/run_evals.py` (row computation around line 1300 and the RESULTS tables),
  `scripts/perf/eval_compare.py`.
- **Change:** each row gets:
  - `gold_token_share`: tokens of packed items whose path is in `expected_files`, divided by `total_tokens`
    (0 when `total_tokens` is 0);
  - `expansion_items`: the number of packed items with a `call_expansion` route.

  Add both to the per-mode summary and the RESULTS table, and to `eval_compare.METRICS`, but **not** to `GATED`,
  so the gate rule is unchanged. `--compare` prints them like the other metrics.
- **Tests:** `tests/test_evals.py` (or the file that covers `run_evals` rows): the two values on a hand-built row.
  `tests/test_eval_baseline.py` golden changes only if it snapshots the column set; regenerate and list it.
- From here on, every eval-gate paste in a commit body includes the two new means.

### Task 3 — A seed must reach another file (finding 1, gap a)

- **File:** `src/cortex/bundle.py`, the `reaches=` lambda passed to `_select_seeds`.
- **Change:** replace "has any outgoing `calls` edge or its class has a base" with a helper
  `_reaches_beyond_file(symbol_id, links, file_of, class_of)`. It is true when either of these holds:
  - Following `links.calls_from` breadth-first through callees **in the same file** (the walk's own 0-hop rule;
    cap the visit at 200 nodes) reaches a callee whose file differs from the seed's, or a function that has
    overriders (`links.overridden_by`). Nodes whose id starts with `external:` never count as reaching another
    file. `file_of` must give them their own `source_ref`, so the walk treats them as same-file.
  - The seed's class has a base class (`links.bases_of`; unchanged).

  Update the `SEED_FILES` comment: "a span that cannot reach a function in another file …". Record refusals as
  `no_reach` in Task 1's counters.
- **Fixture first** (`evals/run_evals.py`, `tests/test_bundle_field_tasks.py`): add
  `_build_field_seed_markup_local_repo`, a variant of `_build_field_seed_markup_repo` in which:
  - every screen function calls a same-file helper of 4+ lines (as in `ui_calls=True`);
  - there are at least 8 screens, each with a **higher raw score** than `src/itemmodel.cpp`. Make the model file
    small enough to escape length normalisation, and give it fewer task words than the screens, while its
    `onTypeChanged` still calls `entry->detach(...)` into `baseitem.cpp`.

  Then all six seed slots go to screen spans on 0.15.0, even with Task 4.3, and the packed bundle has no
  call-expansion item. Use Task 1's `expansion.seeds` to confirm that. Assert in all four modes that a
  call-expansion item starts from `src/itemmodel.cpp`, as `xfail(strict=True, reason="0.15.1 Task 3")` first.
  If you cannot make it fail on 0.15.0 (the C++ seed keeps winning), report the scores you reached and keep the
  change only if the eval gate passes and a unit test on `_reaches_beyond_file` covers the `external:qml` case.
- **Unit test** (`tests/test_overrides.py`): a QML-like span whose only callee is an `external:qml:<same file>:f`
  node does not reach; a span calling a same-file helper that calls a function in another file does.
- **Gate:** the eval gate, plus `test_hub_flood_*` and `test_a_hub_constructor_does_not_flood_*`.

### Task 4 — Computed callees get a guaranteed share of the bundle (finding 1, gap b)

- **File:** `src/cortex/bundle.py`, the packing section (`pack`, `select_span`, around lines 1700–1790).
- **Change:** before the normal packing passes, select the best `EXPANSION_RESERVED_ITEMS = 3` candidates that
  carry a `call_expansion` route, best `tie_order` first.
  - Skip any that `inside_selected_span` / `over_file_cap` refuse, or that do not fit.
  - Stop once they use `EXPANSION_BUDGET_SHARE = 0.2` of the budget.
  - Select each item through the same `select` / `select_span` path, including the "whole file when it fits"
    rule, so the result has the same shape as a normal pass.
  - Then run the existing passes unchanged.
  - Add module-level constants with a comment in the style of the neighbouring ones: why they exist (the
    callees score from their seed and can be outscored by keyword items that merely share words), and the
    tuning range (2–4 items, 0.1–0.3 share).
- **Fixture first:** a variant where callees are computed but not packed on 0.15.0. Use Task 1's
  `callees_computed > 0` and `callees_packed == 0` to prove it. To build it, make the keyword items outscore
  the callees and fill the budget:
  - give the screen files names carrying the task's words (name bonus), e.g. `ui/ItemTypeRemoved{k}.qml`;
  - make the screens large enough that a dozen fill the budget;
  - use a tight budget (try 1500 and 3000).

  Assert as `xfail(strict=True, reason="0.15.1 Task 4")`. If no variant fails on 0.15.0, do not ship Task 4:
  record the attempts under "Not met" and rely on Task 1's diagnostics.
- **Gate:** the eval gate. **Decision rule:** if any gated row drops, try `EXPANSION_RESERVED_ITEMS = 2` and
  `EXPANSION_BUDGET_SHARE = 0.1` once; if it still drops, drop the task and record the measurements.

### Task 4b — Reserve room for callees only when packing left none (finding 1, gap b; added after Task 4)

Task 4 reproduced the field symptom:
- **Fixture:** `field_seed_markup_flood_repo` at budget 1500.
- **On 0.15.0:** `callees_computed` 3, `callees_packed` 0, `callees_dropped` {budget 3}.

Its unconditional reservation fixed the fixture, but failed the gate: mean precision@3 went 0.6471 → 0.5882
(0.6176 with 2 items / 0.1 share). The rows that changed (`python_app`, `web_service`) already packed their
callees: the `expansion_items` mean did not move. The reservation only reordered which files came first.

- **Change:** keep Task 4's constants and helpers (pull the symbol branch of `pack` into `select_symbol`, and add
  `reserve_expansion`). Run them only when needed:
  1. Pack as today: `pack(capped)`, then `pack()`.
  2. If `callees_computed > 0` and **no** computed callee is packed (Task 1's own "packed" test), reset the packing
     state: `selected`, `selected_ids`, `files_fully_selected`, `paths_with_symbols`, `total_tokens`,
     `doc_tokens`, `selected_spans`, `span_tokens`, `oversize_tokens` and `turned_away`. Then run
     `reserve_expansion()` followed by the same two passes.

     Factor the state into one small object or a reset helper; do not duplicate the passes. Bundles that already
     pack at least one callee must come out **byte-identical** to 0.15.1 Task 3's.
  3. Set `expansion['reserved'] = True` when the second packing ran (`False` otherwise; add the key to
     `_empty_expansion`), and include it in `_expansion_summary` only when true.
- **Tests:**
  - remove the strict xfail of `test_computed_callees_get_a_share_of_a_tight_budget`;
  - a test that a bundle which already packs a callee is identical with the second packing disabled (monkeypatch
    `EXPANSION_RESERVED_ITEMS = 0`);
  - a unit test of the reset (no item is selected twice and `total_tokens` stays within the budget).
- **Gate:** the full eval gate as plan-0.15 §0.4 states it: no row's `recall` or `file_recall` decreases, and
  **every** mean is at least the baseline's, precision@3 included. Paste the `--compare` output.
- **Decision rule:** if the gate fails, drop the task and record the numbers under "Not met". The diagnostics
  still ship.

### Task 5 — A data-only C/C++ type used in code is not dead (finding 3)

- **File:** `src/cortex/deadcode.py`.
- **Change:** add `_data_only_types(nodes) -> set[str]`: `class` nodes, not namespaces (`_is_namespace`), in a
  C/C++ file (`_CPP_SUFFIXES`), with **no member function**. That is: no `func`/`method` node in the same file
  whose span lies inside the class span, and no `func`/`method` anywhere whose `metadata.qualifier` equals the
  class label. In the candidate loop, when such a node's `_candidate_reason` returns the reference tier's
  `"low", "no incoming edges; grep refs in code/config"`, skip it instead of reporting it. A code reference
  outside its own definition is a use of a type (a declaration, parameter, `sizeof` or cast); a data type has no
  out-of-line definitions that would mention its name without using it.
- **Why "no member function":** a class whose only references are its own out-of-line `Foo::bar()` definitions
  would otherwise be credited by those definitions and hide a dead class.
- **Tests** (`tests/test_deadcode_cpp_scopes.py`):
  - `typedef struct { int id; } Rec;` in `lib/rec.h`, used as `Rec r;` in `app/main.c` and as a parameter in
    `lib/rec.c`: not reported. The same for a named `struct Named { int x; };` used as `struct Named n;`.
  - A data struct that nothing references: still reported, `high`.
  - A class with methods defined out of line in its own `.cpp` and no caller or reference elsewhere: still
    reported.
  - A data struct named only in a comment elsewhere: still reported, `medium`.
- **Decision rule:** across the `tests/fixtures` repositories, every finding that disappears must be a C/C++
  class with no member function and a code reference outside its definition. List them in the commit body.
  Goldens may change; regenerate and list each.

### Task 6 — Say why `cortex-explorer` lists `Grep`/`Glob` (finding 5)

- **Files:** `agents/cortex-explorer.md`, `README.md`.
- **Change:**
  - **README:** next to the agent description, add one sentence: on macOS/Linux/WSL Claude Code leaves Glob and
    Grep out of the default tool set but gives them back to a subagent that lists them and leaves out `Bash`,
    which is why `cortex-explorer` lists them and not `Bash` (it is read-only).
  - **Agent body:** leave the fallback paragraph as is. No tool-list change.
- **Test:** none beyond the existing `test_cortex_explorer_agent_definition`, which already pins the list.

### Task 7 — Release 0.15.1

- **Version:** 0.15.1 in `pyproject.toml`, both plugin manifests, `.claude-plugin/marketplace.json`,
  `src/cortex/__init__.py` and `tests/goldens/mcp/initialize.json` (plus its assertion in
  `tests/test_golden_mcp_shapes.py`), as 0.15.0's release commit did.
- **CHANGELOG:** a `## 0.15.1 — <date>` section in the 0.15.0 style, with:
  - each task;
  - the golden list;
  - "no graph format change";
  - "Not met" for anything dropped under its rule, plus findings 4 and 5 with their verdicts;
  - "For the reviewer's next run":
    - the §1.4 task's `cortex_query` detailed output, in particular the `expansion` block;
    - the six typedef names;
    - `audit_unresolved_calls.py REPO --all`.
- **Plan status:** set the Status line of this file to "implemented in 0.15.1", with the outcomes.
- **Pre-PR checks (all, as in 0.15.0):**
  - the full suite;
  - the regex-fallback set;
  - goldens actually run;
  - the coverage floors;
  - `python evals/run_evals.py` (do not commit `evals/RESULTS.md`);
  - `python -m build` (outside the repo, or clean up afterwards);
  - the suite without `tiktoken` in a separate venv.

  The tree must be clean at the end.
