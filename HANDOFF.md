# Cortex — Operational Continuation Log

**Branch:** `claude/cortex-rtk-repowise-analysis-zh2g8p` (upstream: `origin/claude/cortex-rtk-repowise-analysis-zh2g8p`; push here only, never to `main`)
**Repo:** `/Users/alilkuizon/Personal Projects/Cortex`
**Plans:** `IMPROVEMENT_PLAN.md` (Cortex-scope items, authoritative) and `RTK_PLAN.md`
(rtk-derived interception/shell-output items, split out 2026-07-17 for a separate plugin).
Read each item's section and acceptance criteria before doing work.

This file is an operational continuation log. Preserve the checkout and the pending
uncommitted work described below.

---

## 2026-07-17 scope split: rtk items removed from Cortex

Decision (user-directed): rtk-style functionality — shell-output compression and
interception of built-in agent tool calls — is out of Cortex's codebase-intelligence scope.
Consequences, all currently **uncommitted in the working tree**:

1. **P1-8 code reverted.** `git revert --no-commit fdaa182` was applied (one CHANGELOG
   conflict resolved by keeping the P2-3 bullet and replacing the P1-8 bullet with a removal
   note). Deleted: `hooks/pre-tool-use.py`, `hooks/README.md`,
   `tests/test_pre_tool_use_hook.py`. Reverted hunks in: `hooks/hooks.json` (PreToolUse
   block), `evals/run_evals.py` (`run_hook_adoption_replay` + `--hook-replay`),
   `tests/test_evals.py`, `tests/test_plugin_manifests.py`, `.claude-plugin/plugin.json`,
   `install.sh`, `README.md`, `skills/cortex/SKILL.md`, `hooks/session-start.py`,
   `CHANGELOG.md`. The implementation remains recoverable from commit `fdaa182` and is
   referenced as the porting base in `RTK_PLAN.md`.
2. **`IMPROVEMENT_PLAN.md` cleaned.** rtk research section, gap-table column/rows, and the
   full P1-8/P2-1/P2-4 item texts removed; each item slot now holds a pointer to
   `RTK_PLAN.md`. §2.1 rewritten: interception is out of scope; Cortex covers adoption via
   incentives (P0-2/P1-1) and substitution (P2-5). Wave 3 no longer contains P1-8/P2-1/P2-4.
3. **`RTK_PLAN.md` created.** Contains the rtk research summary, the interception insight,
   items R1 (ex-P1-8), R2 (ex-P2-1), R3 (ex-P2-4), the `fdaa182` implementation/revert
   history with its measured replay evidence (precision/recall 1.0, warm median 1.21 ms),
   and a suggested order for building it as a separate plugin.

**Verification:** after the revert, `python3 -m pytest tests/ -q` = **347 passed /
4 skipped** (HEAD baseline with P1-8 + P2-3 was 359/4; the delta is exactly the 12 removed
hook tests). No `src/cortex/` module was touched by the revert — `fdaa182` never modified
`src/`.

**Next action for this split:** review the uncommitted diff, then commit it as one focused
change (docs split + P1-8 revert) with the required trailers, and push. Ask the user before
committing — nothing here is committed yet.

---

## Current committed state

HEAD is `1e71f25eb4cfbec97e6aa8723ea86cee2945ab28` (`feat: add diff-aware risk analysis`,
P2-3). Committed test baseline at HEAD: **359 passed / 4 skipped** (becomes 347/4 once the
P1-8 revert above lands).

### Landed and pushed

| Item | Commit | What is available |
|---|---|---|
| Wave 0 through P1-6 | Earlier commits on this branch | Qt fixture/evals, incremental ingest, savings ledger, FTS/RRF search, cache, tokenizer mechanism, Qt resolution, `_meta`, read modes, and `cortex_read_file`; retain the invariants and eval baselines below. |
| P1-2 | `756e563be14dc9f67bb933a787e58e4526273338` | Churn × complexity hotspot analytics and persisted hotspot metadata. |
| Tokenizer test hardening | `52188114cfe9abae70aa7024745c7597e22d53d7` | Isolates the stdlib tokenizer fallback path. |
| P1-1 | `d2582679a43d391a98b7f14e6cfc7bcf73e78313` | Batched `cortex_context` cards, including Qt-aware triage. |
| P1-7 | `ae0e7c53a1d7a437db348de23b80879a5d945847` | Optional local semantic retrieval using the managed Model2Vec path; local-only, no network during runtime ingest/query/eval. |
| P1-8 | `fdaa182db353c491b27e58924783b0265c3ebd5a` | **Superseded — reverted in the working tree by the 2026-07-17 scope split; see above and `RTK_PLAN.md`.** |
| P2-3 | `1e71f25eb4cfbec97e6aa8723ea86cee2945ab28` | Diff-aware `cortex risk` / `cortex_risk` (committed after this log previously described it as pending; check `git log` if push status is unclear — verify with `git status -sb`). |

---

## Remaining order and blockers

Continue in this dependency order (rtk items no longer apply):

1. **Land the scope-split commit** described above (user confirmation required).
2. **P2-2 — dead-code report.** Preserve P0-4 Qt meta-object semantics: emits/connects/
   handles edges must receive credit so signals and slots are not falsely reported as dead;
   retain the P0-2 grep tier.
3. **P2-5 — `cortex-explorer` exploration sub-agent.** Depends on the landed P1-1
   `cortex_context`. Note: the P1-8 advisory-hook "safety net" mentioned in older notes no
   longer exists inside Cortex.

**Moved out of Cortex** (do not implement here): P2-1 distill/expand, P2-4 missed-savings
discovery, P1-8 redirect hook — all now in `RTK_PLAN.md` for a separate plugin. P2-4's
former data source (`<CORTEX_DATA_DIR>/<repo-hash>/usage.jsonl`) is no longer written by
anything in this repo.

### Non-blocking follow-ups to retain

- **Tokenizer calibration:** P1-4's per-kind mechanism is landed, but stdlib `CALIBRATION`
  factors remain provisional 1.0 placeholders. Measure with `pip install tiktoken &&
  python3 evals/calibrate_tokenizer.py` when network egress is available, then bake measured
  factors into `src/cortex/tokenizer.py`; never fabricate measurements.
- **Hash nondeterminism:** `PYTHONHASHSEED` can reorder tight-budget/skeleton bundle rows.
  Find the unstable sort/dict iteration and make ordering deterministic; compare
  precision/recall rather than incidental file order or latency until fixed.

---

## Process and invariants

1. Work one implementation item at a time on this branch. Shared modules (`store.py`,
   `mcp/tools.py`, `bundle.py`) make concurrent edits and source-level verification unsafe.
2. Read the relevant plan section and named files before editing. Leave delegated
   implementation work uncommitted for independent review; do not create side branches or
   PRs unless requested.
3. Independently verify before committing: full tests, relevant evals, actual diff, and a
   functional spot-check. Never accept a worker summary alone.
4. Commit one reviewed item at a time, then push with `git push -u origin <branch>`. Keep
   `evals/RESULTS.md` out of focused commits when it is only nondeterministic eval churn.
5. Partial work from a killed session is valuable. Resume or finish it; do not reset it to
   recover a clean tree.
6. Core remains stdlib-only. Optional dependencies (tiktoken, Model2Vec/numpy, tree-sitter,
   watchdog, LLM providers) stay behind extras and soft imports.
7. Schema changes use `CortexStore._migrate_existing_schema` for idempotent ALTERs or
   `CREATE TABLE IF NOT EXISTS` in `initialize_schema`.
8. Any MCP/CLI surface change updates `README.md`, `CHANGELOG.md`, `skills/cortex/SKILL.md`,
   and `hooks/session-start.py` as applicable.
9. Preserve C++/QML/Qt parity on the stdlib regex backend; never make a feature depend on
   tree-sitter. Retrieval-affecting changes require eval verification.
10. Keep all local-only/no-network claims honest. Explicit setup may fetch the optional
    semantic model once; normal ingest, query, hooks, risk analysis, and eval execution must
    not fetch or contact a remote service.

### Branch, push, and commit trailers

Push only to the current feature branch, never `main`:

```bash
git push -u origin claude/cortex-rtk-repowise-analysis-zh2g8p
```

Every eventual commit requires the existing trailers:

```text
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pqcey2qb6tUr5a37nzkbTP
```

Keep commit subjects/bodies operational. Do not create a PR unless the user asks.

---

## Retrieval/eval baselines

Existing-task pagerank precision/recall baselines (compare these, not token/latency jitter):

- `Trace password login token issuance and session audit`: **0.333 / 1.000**
- `How does rank_nodes score and order graph nodes`: **0.250 / 0.333**
- `Where is the token budget applied when packing output`: **0.250 / 0.333**
- `fix the stale index detection in the auto refresh path`: **0.111 / 0.333**
- `qt_app — Where is the deviceConnected signal emitted…`: **0.571 / 0.667**

Token/latency columns and tight-budget/skeleton file ordering vary run to run because of the
known hash/order nondeterminism. Evaluate precision/recall first, and preserve/restore
`RESULTS.md` after non-retrieval checks.

---

## Key file map

- `src/cortex/mcp/tools.py` — MCP definitions/dispatch, `_format_payload`/`_build_meta`,
  freshness, `_estimate_baseline`, usage ledger, query cache, `cortex_risk` integration.
- `src/cortex/risk.py` — P2-3 local Git parsing, score calculation, directives, and
  deterministic budget truncation.
- `src/cortex/bundle.py` — bundle ranking/fusion and skeleton rendering; inspect for
  hash-order follow-up.
- `src/cortex/fusion.py` — RRF rank fusion, including optional semantic input.
- `src/cortex/store.py` — schema, `tool_usage`, `query_cache`, `source_fts`, semantic
  vectors, and Qt graph fetches.
- `src/cortex/ingest.py` — full/incremental ingest and semantic synchronization.
- `src/cortex/structural/{regex_backend,treesitter_backend}.py` — C++/QML/Qt extraction and
  resolved cross-file relations.
- `src/cortex/tokenizer.py` — per-kind counting, provisional calibration, optional exact
  tokenizer path.
- `hooks/session-start.py` — the only remaining hook (SessionStart, advisory, fail-open).
- `evals/run_evals.py` — retrieval matrix, optional semantic-on checks, latency helpers
  (hook adoption replay removed with the P1-8 revert).
- `evals/RESULTS.md` — historical eval output; restore unrelated churn before focused
  commits.
