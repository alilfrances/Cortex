# Resume log: Cortex 0.15.1 (paused 2026-10-26)

Delete this file in 0.15.1 Task 7 (release). It exists only to resume quickly after a pause.

## Where things stand

- **0.15.0 is released:** PR #38 was squash-merged into `main` as `d066947` via admin bypass, after CI passed
  (`test`, CodeQL `analyze`, CodeQL).
- **0.15.1 plan:** `docs/plan-0.15.1-fixes.md`, committed on branch `cortex-0.15.1` (`ddd39db`, pushed).
  - It covers the reviewer's five findings on 0.15.0 and seven tasks.
  - Findings 4 (`calls` share 0.4) and 5 (`Grep`/`Glob`) get no code fix; the reasons are in the plan's verdict.
  - The plan's §0 rules are in `docs/plan-0.15-field-review.md` §0.
- **Work in progress:** the agent doing Tasks 1–4 was stopped partway through **Task 1** (call-expansion
  diagnostics). Its uncommitted diff is saved on branch **`cortex-0.15.1-wip`** (`db3a96b`, pushed). That diff
  touches `src/cortex/bundle.py` (+90), `src/cortex/mcp/tools.py` (+14) and `tests/test_bundle_call_expansion.py`
  (+111).
  - **Not validated:** goldens, the full suite and the regex set were not run.
  - The agent's last step: "add a budget-drop test, then run goldens to see which change".
- **Release branch is clean:** `cortex-0.15.1` holds only the plan commit (plus this log).

## How to resume

1. **Environment** (a fresh container has none of this):
   ```bash
   cd /home/user/Cortex && git fetch origin && git checkout cortex-0.15.1 && git pull origin cortex-0.15.1
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e ".[llm,watch,dev,tokens]" build twine
   python -m cortex runtime setup     # must report "ready": true
   ```
2. **Bring in the WIP:** `git cherry-pick --no-commit origin/cortex-0.15.1-wip`. Review the diff against plan Task 1,
   finish it, and commit it as Task 1 with a proper message. After 0.15.1 merges, delete the WIP branch:
   `git push origin --delete cortex-0.15.1-wip`.
3. **Delegate the rest to Sonnet 5.5,** in two background agents, run one after the other:
   - **Agent A: Tasks 1 (finish), 2, 3, 4.**
     - Give it the "Context you can rely on" facts below.
     - Rules:
       - failing test first; for Tasks 3 and 4, a strict xfail proven with Task 1's `expansion` block;
       - the eval gate on Tasks 3 and 4, with the `--compare` output pasted into each commit body;
       - run the golden tests for real;
       - one commit per task, with a `## Unreleased` CHANGELOG bullet each;
       - finish with the full suite and the regex-fallback set;
       - push, and open no PR.
   - **Agent B: Tasks 5, 6, 7** (data-only struct fix, README note, 0.15.1 release plus every pre-PR check).
     - It runs the suite without `tiktoken` in a **separate** venv (`/tmp/venv-notok`), never by uninstalling from
       `.venv`.
     - Task 7 also deletes this log file.
4. **Review each agent's commits before starting the next.** Check `git diff`, and re-run the full suite myself on
   the final head if the agent squashed or amended anything after its last full run.
5. **Ship:** open a PR `cortex-0.15.1` → `main` using `.github/pull_request_template.md`, subscribe to PR activity,
   fix CI if red, then **squash-merge via admin bypass** once CI is green. The user asked for exactly this flow for
   0.15.0. Afterwards, unsubscribe and delete any fallback check-in.

## Context you can rely on (verified on `d066947`)

- **Seeds:** `bundle.py` passes `raw_source_scores` to `_select_seeds(..., reaches=...)` around line 1593. Today's
  `reaches` is `bool(links.calls_from.get(id)) or bool(links.bases_of.get(class_of(id) or ''))`.
  - A QML same-file call resolves to an `external:qml:<path>:fn` node with `granularity="symbol"`, so it counts as
    reaching. Task 3 closes this.
- **Callee items:** created around lines 1620–1672. The owner file item receives the same `call_expansion` route,
  so a callee packed through its whole file keeps its label.
- **Packing:** the nested `pack()` / `select` / `select_span` / `over_file_cap` / `inside_selected_span`, around
  lines 1690–1790, with `item_cap = budget * ITEM_BUDGET_SHARE`.
- **0.15.0 fixtures:** `evals/run_evals.py` has `_build_field_seed_markup_repo(base, *, ui_calls=False)` and
  `_build_field_seed_markup_calls_repo`; the tests are in `tests/test_bundle_field_tasks.py`.
- **No reproduction yet.** These synthetic probes all packed every callee on 0.15.0, so Tasks 3 and 4 need
  constructed fixtures (levers are in the plan):
  - 6 or 30 QML screens;
  - screens filling an 8000-token budget;
  - screens rooted on a repo `BaseScreen.qml`;
  - same-file QML helper calls.
- **MCP query:** `cortex_query` is `_call_query` in `mcp/tools.py` (~821); the concise form is
  `_concise_query_bundle` (~794); the why-renderers are `_bundle_why` / `_compact_why`.
- **Evals:** rows are computed around line 1300 in `evals/run_evals.py`. `scripts/perf/eval_compare.py` has
  `METRICS` and `GATED = ("recall", "file_recall")`.
- **Dead code:** a `typedef struct {…} Rec;` used as `Rec r;` is reported `low` ("grep refs in code/config") on
  0.15.0, exactly like a named struct. The 0.15.0 Task 1.4 helper `_classes_with_live_members` in `deadcode.py` is
  the model for Task 5's `_data_only_types`.
- **Baseline on 0.15.0:**
  - full suite: 2373 passed, 58 skipped (about 12–15 min locally, 19 min in CI);
  - regex-fallback set: 299 passed, 367 skipped;
  - coverage floors: all 16 met;
  - eval means: precision 0.5108, precision@3 0.6471, recall 0.9375, file recall 0.9779, symbol recall 0.9118.

## User preferences seen in this session

- Plans are written by me and implemented by Sonnet 5.5 subagents. I review each agent's result before starting the
  next one.
- The owner defaults in plan §4 apply unless the user says otherwise.
- PR flow: open the PR, track CI, fix as needed, squash-merge via admin bypass.
- Commits end with the session attribution trailers; no model names in code, docs or commit bodies otherwise.
