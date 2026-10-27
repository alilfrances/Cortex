# Plan: Cortex 0.15.2: callees that compete for the bundle

Status: **implemented in 0.15.2**, except where noted. Outcomes and deviations (details in `CHANGELOG.md`):
- Task 1 as planned: `budget` is split into `too_large` and `budget_exhausted`; no golden changed.
- Task 2: compact entries are in, and `expansion` carries `callees_compact`. Variant 1 as written (reserve the whole
  `COMPACT_CALLEE_SHARE` up front) failed the eval gate at 0.1 and at 0.05: two rows of `web_service` at budget 180
  lost `integrations/slack.py` (recall means 0.9375 -> 0.9252, file_recall 0.9779 -> 0.9681). Variant 2 (leftover
  only) passes the gate but the fixture leaves no leftover, so it adds no entry. Shipped: variant 1 reserving only
  what the best entries would take, and only when the share holds `COMPACT_MIN_ENTRIES` = 3 of them; no row changes
  and every mean equals the baseline. Fixture `field_callee_fanout_repo`: the model function reaches the twenty
  callees through five same-file stage functions (the walk follows four other-file callees per function), and its
  comment carries the task's words so that the gold file survives the keyword screens.
- Task 3: released as 0.15.2.
- Not verified: every field acceptance item (needs the reviewer's run).
Input: the reviewer's 0.15.1 run of the §1.4 task (`cortex_query`, detailed), read from the new `expansion` block:

| budget | callees_computed | callee_files | seeds_cross_file | callees_packed | reserved | callees_dropped |
|---|---|---|---|---|---|---|
| 8000 | 119 | 33 | 3 | 3 | true | budget 116 |
| 4000 | 119 | 33 | 3 | 1 | false | budget 118 |

Base: `main` at 0.15.1 (`b7dd89e`). Target: **0.15.2**. No graph format change.

The rules of `docs/plan-0.15-field-review.md` §0 apply unchanged: setup, checks, goldens, eval gate, failing test
first, one commit per task, a `CHANGELOG.md` bullet per task, and no model names. Work on branch `cortex-0.15.2`.

---

## 1. Verdict

- **Seeds and walk are fixed** (0.15.1 Task 3). 119 callees over 33 files from 3 cross-file seeds; 0.14 found 2.
- **Packing is the bottleneck.** At 8000 the normal passes packed **no** callee. The 3 packed ones are the
  0.15.1 Task 4b reservation, which caps at exactly 3. At 4000 the passes packed 1 by chance, so the reservation
  (which runs only at zero) did not fire. For comparison, 0.13 packed 16 call items and 11 override items for this
  task at 8000.
- **Diagnostic gap.** `callees_dropped.budget` merges two causes that need different fixes:
  - `item.token_count > min(budget - total_tokens, item_cap)` because the span is larger than `item_cap`
    (0.4 × budget) or larger than the budget, i.e. too big to pack whole;
  - because the budget was already spent when the callee's turn came, i.e. it ranked too low.

  The near-identical drop at half the budget (116 vs 118) points to the second cause, but does not prove it.
- **Fix design.** Make callees visible cheaply instead of competing with full bodies. A callee that is not packed
  gets a **compact entry**: its declaration line(s) plus the call route, roughly 15–30 tokens instead of
  hundreds, taken from a small bounded share of the budget and placed after the normal items. That shows the call
  structure across the callee files without pushing out keyword files. It works whichever cause dominates.

---

## 2. Tasks (in order, one commit each)

### Task 1: split the `budget` drop reason

- **File:** `src/cortex/bundle.py`, where `pack` / `select_symbol` set `turned_away[...] = 'budget' if
  item.token_count > allowed else 'file_cap'` (around line 1883), plus `CALLEE_DROP_REASONS` (line 132).
- **Change:** replace `budget` with two reasons:
  - `too_large`: `item.token_count > item_cap`, or `> budget`;
  - `budget_exhausted`: any other `item.token_count > allowed`.

  Update `CALLEE_DROP_REASONS`, `_empty_expansion` and `_expansion_summary`. The constants' comment says what each
  reason means.
- **Tests** (`tests/test_bundle_call_expansion.py`): update the existing budget-drop test. Add one case per reason:
  - a callee span larger than `item_cap` → `too_large`;
  - a small callee that ranks below items which fill the budget → `budget_exhausted`.
- **Goldens:** none expected (`expansion` is nested). Run them to confirm.

### Task 2: compact entries for callees that do not fit whole

- **File:** `src/cortex/bundle.py`.
- **New constants** (module level, with a comment in the style of the neighbouring ones: why they exist, and the
  tuning range):
  - `COMPACT_CALLEE_SHARE = 0.1`: share of the budget for compact entries; tuning range 0.05–0.15;
  - `COMPACT_CALLEES_MAX = 24`;
  - `COMPACT_LINES_MAX = 3`: declaration lines per entry.
- **Compact entry:** for a computed callee, build a `BundleItem` with:
  - `kind='symbol'` and `item_id=f'callee-compact:{callee_id}'`;
  - `path` = the callee's file;
  - `content` = one comment line naming the route (`calls: <seed label> -> <callee label>`, or
    `override: <via> -> <callee>`, using the same labels `_compact_why` prints), followed by the callee's
    `_declaration_lines` (at most `COMPACT_LINES_MAX`, each stripped of trailing whitespace, then
    `ELISION_MARKER`);
  - `token_count` from `count_text_tokens`;
  - `score` = the callee's score;
  - `metadata` = the callee's `call_expansion` route plus `compact: True`, `node_id`, `span_start` and `span_end`.

  Use the source lines the callee span item already used. Do not re-read files.
- **Compact pass:** runs after the normal passes and after the Task 4b repack, on every bundle where
  `callees_computed > 0`.
  - Go through the callees not packed by Task 1's "packed" test, in `tie_order`.
  - Skip a callee whose file is selected whole, or whose span lies inside a selected span.
  - Select its compact entry while all three hold: entries < `COMPACT_CALLEES_MAX`; the entries' tokens stay within
    `COMPACT_CALLEE_SHARE × budget`; `total_tokens` stays within `budget`.
  - The entries go to the **end** of the selected items, so no earlier item's position changes.
  - Record `callees_compact` (the count) in `expansion`, and add it to `_empty_expansion` and `_expansion_summary`.
  - A compact callee is no longer counted in `callees_dropped`: it is counted as compact, not packed.
- **Where the budget comes from:** decide by the eval gate, in this order, and stop at the first that passes:
  1. **Reserve up front.** When the walk computed callees, run the normal passes with
     `budget - int(COMPACT_CALLEE_SHARE × budget)`, then the compact pass with the full budget. Hand any share the
     compact pass leaves unused back with one extra uncapped `pack()` pass, so a bundle with few callees loses
     nothing.
  2. **Leftover only.** Do not reserve. The compact pass uses whatever budget the normal passes left. Bundles that
     did not compute callees are untouched in both variants.

  Record in the commit body which variant shipped and both `--compare` outputs.
- **Rendering:**
  - Concise `cortex_query` items for compact entries keep `content` and `why`. `_bundle_why` / `_compact_why` already
    render the `call_expansion` route as `calls:` / `override:`, so check that they do for these items.
  - In markdown output, an entry renders like a skeleton item.
  - Add `"compact": true` to the item in the detailed form only.
- **Fixture first** (`evals/run_evals.py`, `tests/test_bundle_field_tasks.py`): `field_callee_fanout_repo`.
  - Built from `field_seed_markup_flood_repo` (0.15.1).
  - Add a model function that calls 20 functions, each in its own `.cpp`, with bodies of 15–25 lines so their
    spans are not tiny. Keep a virtual call with two overriders.
  - Budget: 3000 and 6000.
  - Assert in all four modes: `callees_computed >= 20`; `callees_packed + callees_compact >= 12`; at least one
    `override:` route; the gold keyword file is still packed.
  - Mark it as a strict xfail first. Paste the `expansion` block before the change, which should show most
    callees in `budget_exhausted` or `too_large`, and after.
- **Gate** (decided now, before measuring):
  - no row's `recall` or `file_recall` decreases;
  - the means of `precision`, `precision_at_3`, `recall`, `file_recall` and `symbol_recall` are each ≥ the
    baseline's;
  - `gold_token_share` may fall by at most 0.02 (an informational metric since 0.15.1, and compact entries of
    non-gold files dilute it by construction);
  - `expansion_items` is expected to rise and must not fall.

  `scripts/perf/eval_compare.py` prints `EVAL GATE: pass` on `recall` / `file_recall` alone, so check the other
  means yourself.
- **Decision rule:** if neither variant passes the gate, drop the compact pass. Ship Task 1 alone and record the
  numbers under "Not met".

### Task 3: Release 0.15.2

- **Version:** 0.15.2 in `pyproject.toml`, both plugin manifests, `.claude-plugin/marketplace.json`,
  `src/cortex/__init__.py`, `tests/goldens/mcp/initialize.json` and its assertion in
  `tests/test_golden_mcp_shapes.py`, and the README version line. See `git show --stat b7dd89e` for every place.
- **CHANGELOG:** a `## 0.15.2 — <date>` section in the 0.15.1 style. It must contain:
  - the input table above;
  - each task, with the golden list;
  - "no graph format change";
  - "Not met" (anything dropped);
  - "For the reviewer's next run": the §1.4 task's `expansion` block at 8000 and 4000 (`callees_packed`,
    `callees_compact`, the split `callees_dropped`), plus yes/no answers to two questions: are the base-class
    `.cpp` and a subclass `.cpp` among the `calls:` / `override:` items, and roughly how many items and QML screens
    are in the 8000 bundle. No file contents are needed.
- **README:** the `cortex_query` section mentions compact callee entries and `callees_compact`.
- **Plan:** set the Status line of this file to "implemented in 0.15.2", with the outcomes.
- **Pre-PR checks** (as in 0.15.1), reporting each result line:
  - the full suite;
  - the regex-fallback set;
  - goldens, actually run;
  - coverage floors;
  - `python evals/run_evals.py` (do not commit `evals/RESULTS.md`);
  - `python -m build --outdir /tmp/cortex-dist .`, then remove `build/` and `*.egg-info`;
  - the suite without `tiktoken`, in the separate venv `/tmp/venv-notok`.

  The wall-clock test `test_resolving_2000_symbols_stays_under_half_a_second` is known to exceed its limit only
  under coverage instrumentation. Re-run it without coverage before calling it a failure. The tree must be clean
  at the end.
