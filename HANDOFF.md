# Cortex Improvement Plan — Implementation Handoff

**Branch:** `claude/cortex-rtk-repowise-analysis-zh2g8p` (push here only; never to `main`)
**Plan:** `IMPROVEMENT_PLAN.md` at repo root — the authoritative spec. Every work item (P0-1…P2-5) has motivation, file refs, steps, and **acceptance criteria**. Read the item's section before implementing it.
**Repo:** `/home/user/Cortex` (Python, stdlib-only core; local-first code-context engine).

This document lets a fresh orchestrator agent continue the delegated implementation without re-deriving context.

---

## Current state (as of handoff)

Test baseline progression: started **185 passed / 8 skipped**, now **300 passed / 9 skipped**. Zero retrieval regressions at every step (evals verified each time). Last commit: `9f41a9d` (P1-6). 10 of the plan's ~18 work items are done.

### Committed & pushed (verified)
| Item | What landed | Tests after |
|---|---|---|
| Wave 0 | `qt_app` Qt/C++/QML eval fixture + 3 gold tasks in `evals/run_evals.py` | 185/8 |
| P0-3 | Fast-path incremental ingest (delta graph writes, O(changed) reads); fixed 2 real defects (stale structural edges, dangling cochange) | 190/8 |
| P0-1 | Token-savings ledger (`tool_usage` table) + `cortex saved` CLI; non-fatal recording; `_estimate_baseline` policy | 210/8 |
| P0-2 | FTS5 full-text (`source_fts`) + `fusion.py` RRF + `cortex_search_text` tool; semble-style definition/adaptive signals; Qt search parity | 254/8 |
| P1-3 | Fingerprint-keyed `query_cache` for query/impact/overview; kill-switch `CORTEX_QUERY_CACHE=0`; `gc` pruning | 265/8 |
| P1-4 | Tokenizer calibration MECHANISM + `[tokens]` tiktoken extra + binary-search truncation. **CALIBRATION factors are 1.0 placeholders** (egress-blocked) — see follow-up | 270/8 |
| P0-4 | Cross-file Qt signal/handler resolution (`QtSymbolIndex`/`_resolve_qt_edges`); fixed a real `connect()` mis-resolution; incremental-safe | 276/9 |
| P1-5 | `_meta` envelope on all 8 tools (one shared path); concise-only-when-noteworthy; folds in `saved_tokens`/`cached`; closed the P1-3 cache-freshness caveat | 284/9 |
| P1-6 | Read modes (`cortex_read_symbol` full/skeleton/signature) + new `cortex_read_file` raw-Read replacement; ledger-tracked; fixed 2 real `_render_skeleton` bugs (span-containment nesting, QML signal span overrun) | 300/9 |

### Remaining work (in dependency order)
**Wave 2 (remaining):**
- **P1-1** Batched triage tool `cortex_context(targets[])`. Dep: better after P1-2; **needs P0-4** (Qt relations in cards — DONE). Returns per-target cards (neighbors, cochange, Qt signals/slots/connects).
- **P1-2** Hotspot analytics (churn × complexity), per-language complexity keywords. New `hotspots.py`. Coordinate metadata storage with P0-3's delta path.
- **P1-7** Optional static-embedding retriever (`[semantic]` extra, Model2Vec). Deps: P0-2 fusion (DONE), P0-3 delta (DONE). **WILL likely hit the same egress block as P1-4** — the model download needs network. Expect "mechanism complete, model/factors pending" and flag it honestly; do NOT fabricate embeddings or bypass egress blocks.

**Wave 3:**
- **P0-4 is done** (was pulled early). 
- **P1-8** PreToolUse redirect hook for Read/Grep/Glob (advise mode first; enforce gated on P2-4 data). Dep: P1-6.
- **P2-3** Diff-aware risk `cortex risk <range>`. Deps: P1-2, P0-4.
- **P2-2** Dead-code report. Deps: P0-4 (Qt meta-object exclusions — MUST credit emits/connects/handles edges so slots/signals aren't false-flagged), P0-2 grep tier.
- **P2-1** `cortex distill`/`expand` shell-output compression. **Decision required first** (see plan §P2-1): keep delegating to external tokenslim vs build native. Ask the user.
- **P2-4** `cortex discover` missed-savings. Deps: P0-1, P1-6, P1-8 (log source).
- **P2-5** `cortex-explorer` exploration sub-agent in the plugin bundle. Dep: P1-1.

**Follow-ups (tracked as tasks #8, #9, do not block the plan):**
- Measure real tokenizer CALIBRATION factors: `pip install tiktoken && python3 evals/calibrate_tokenizer.py`, bake into `src/cortex/tokenizer.py` — needs network egress this sandbox blocks.
- PYTHONHASHSEED nondeterminism in ranking/bundling (reproduced on unmodified HEAD by two agents): tight-budget/skeleton eval rows reorder run-to-run. Find the unstable sort/dict iteration; make bundle ordering deterministic.

---

## Orchestration process (what has worked)

1. **One implementation agent at a time**, on the branch (NOT parallel). Reason: Wave items edit shared files (`store.py`, `mcp/tools.py`, `bundle.py`) and each agent *runs* the source to verify — concurrent agents editing importable modules cause spurious failures.
2. **Delegate to `general-purpose` subagents, model `sonnet`.** Prompt each with: read the specific plan section + named files; the exact steps; **hard verification gates**; and "leave changes uncommitted for review, don't create branches/commit."
3. **The orchestrator (you) independently verifies before committing** — never trust the agent's summary alone:
   - `python3 -m pytest tests/ -q` → must meet/exceed the prior test count, zero failures.
   - `python3 evals/run_evals.py` → spot-check existing-task precision/recall vs baseline (numbers below). Ignore file-order/latency jitter (that's the known nondeterminism).
   - Read the actual diff for the risky part (ranking changes, schema migrations, any relaxed/changed existing test — verify a changed test is legitimate, not weakened to pass).
   - A functional spot-check when useful (e.g. run a tool, inspect output).
4. **Commit per item** with a detailed message ending in the two required trailers (see below), then `git push -u origin <branch>`. Each commit is a safe checkpoint.
5. **Revert eval `RESULTS.md` churn** when an item can't affect retrieval (keeps commits focused; the churn is nondeterminism noise).
6. **Setup:** `python3 -m pip install -q pytest` (not preinstalled). `regex`/`tree_sitter`/`tiktoken` are NOT installed — tree-sitter-dependent tests are `importorskip`-gated; the stdlib regex backend is the tested default.

### Session-limit recovery (this HAS happened twice)
Agents can be killed mid-work by account session limits. Recovery:
- Partial work is left in the working tree. **Do not discard it.**
- **Resume the same agent** via `SendMessage` to its `agentId` — it keeps full transcript context and continues from its partial state. (Used for P0-2.)
- If the agent is ~1 step from done, **finish it yourself** (orchestrator tool calls still work when subagent spawns are limited). (Used for P1-5.)
- Only commit after YOUR own green re-run. Never commit a partial/broken tree.

### Commit trailer (required on every commit)
```
Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Pqcey2qb6tUr5a37nzkbTP
```
Do NOT put the model identifier in commits/PRs/code. Do NOT create a PR unless the user asks.

---

## Eval baseline (existing tasks — must not regress)

`python3 evals/run_evals.py`, pagerank mode, precision/recall:
- "Trace password login token issuance and session audit": **0.333 / 1.000**
- "How does rank_nodes score and order graph nodes": **0.250 / 0.333**
- "Where is the token budget applied when packing output": **0.250 / 0.333**
- "fix the stale index detection in the auto refresh path": **0.111 / 0.333**
- qt_app "Where is the deviceConnected signal emitted…": **0.571 / 0.667**

Token/latency columns and tight-budget/skeleton file ordering vary run-to-run (known nondeterminism) — compare precision/recall, not those.

---

## Invariants every item must honor (from plan §3)
- **Core stays stdlib-only.** Optional features (tiktoken, embeddings, tree-sitter) go behind pyproject extras with soft imports; default path unchanged.
- **Schema changes** via `CortexStore._migrate_existing_schema` (idempotent ALTERs) or `CREATE TABLE IF NOT EXISTS` in `initialize_schema`.
- **Update `README.md` / `CHANGELOG.md` / `skills/cortex/SKILL.md` / `hooks/session-start.py`** whenever the MCP/CLI tool surface changes (documented past bug class).
- **QML/C++/Qt parity:** any item touching ranking/packing/reading/reporting must test on the `qt_app` fixture, regex backend (default) at minimum. Never gate a feature on tree-sitter.
- **New/changed behavior needs tests**; retrieval-affecting changes need eval verification.

---

## Key file map
- `src/cortex/mcp/tools.py` — MCP tools, `TOOL_DEFINITIONS`, `call_tool`, `_format_payload`/`_build_meta` (P1-5), `_estimate_baseline`/`_record_tool_usage` (P0-1), cache helpers (P1-3).
- `src/cortex/bundle.py` — `generate_bundle` ranking + fusion (P0-2), `_render_skeleton`.
- `src/cortex/fusion.py` — `rrf_fuse` (N-list; P1-7 adds an embedding list here).
- `src/cortex/store.py` — schema, all tables (`tool_usage`, `query_cache`, `source_fts`), `QtSymbolIndex` fetch.
- `src/cortex/ingest.py` — full + incremental (delta) ingest; `build_file_layer`/`build_cochange_layer` in `graph.py`.
- `src/cortex/structural/{regex_backend,treesitter_backend}.py` — Qt/C++/QML extraction (P0-4 resolution).
- `src/cortex/tokenizer.py` — `count_text_tokens(text, kind)`, CALIBRATION (placeholders), soft tiktoken.
- `evals/run_evals.py` — fixtures (incl. `qt_app`, `body_text_repo`) + GOLD_TASKS.
