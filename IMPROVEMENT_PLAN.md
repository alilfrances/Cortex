# Cortex Improvement Plan: Optimization & Efficiency Gaps vs. rtk and Repowise

Date: 2026-07-16
Scope: Comparison of Cortex (v0.7.5) against [rtk-ai/rtk](https://github.com/rtk-ai/rtk) and
[repowise-dev/repowise](https://github.com/repowise-dev/repowise), focused on **optimization and
efficiency features**. Each work item below is written to be delegated independently to an AI
agent: it names the motivating feature in the reference tool, the current state in Cortex with
file references, concrete implementation steps, and acceptance criteria.

---

## 1. What the reference tools provide

### 1.1 rtk (Rust Token Killer)

rtk is a CLI proxy that compresses command output *before* it reaches an agent's context window,
claiming 60–90% token savings. Its efficiency machinery:

- **Output compression filters for 100+ commands** — test runners (jest/pytest/cargo/go),
  linters, git, package managers, docker/kubectl/aws — using four techniques: smart filtering
  (strip noise/boilerplate), grouping (errors by rule/file, files by directory), truncation, and
  deduplication (repeated log lines collapsed with counts).
- **Failures-only test output** (`rtk pytest`, `rtk test <cmd>`, `rtk err <cmd>`): ~90% reduction.
- **Signature-only file reading** (`rtk read <file> -l aggressive`) and 2-line heuristic
  summaries (`rtk smart <file>`).
- **Token-savings analytics**: `rtk gain` (summary, `--graph`, `--history`, `--daily`,
  `--format json`), `rtk discover` (finds *missed* savings opportunities in recent sessions),
  `rtk session` (adoption rate across sessions).
- **Tee / failure recovery**: when a filtered command fails, the full raw output is saved to
  `~/.local/share/rtk/tee/…` so the agent can read it without re-running the command.
  Configurable (`failures` / `always` / `never`).
- **Hook-based transparent adoption**: PreToolUse hooks for 15+ agents rewrite `git status` →
  `rtk git status` automatically; `--ultra-compact` global flag for extra squeeze.
- **<10 ms overhead per command**; single static binary.

### 1.2 Repowise

Repowise is a codebase-intelligence platform (closest in spirit to Cortex: index once, serve
curated context over MCP). Its efficiency machinery:

- **Task-shaped MCP tools that replace raw file reads**: `get_overview`, `get_context(targets)`
  (batched triage cards), `get_symbol` (raw source for one indexed symbol), `get_answer`
  (hybrid retrieval + graph bias → cited answer), `search_codebase`, `get_risk`, `get_why`,
  `get_dead_code`, `get_health`. Claimed effect: up to −96% context tokens, −89% file reads,
  −70% tool calls.
- **Hybrid retrieval**: SQLite FTS fulltext + vector embeddings fused with reciprocal rank
  fusion (RRF), plus PageRank bias and 1-hop graph expansion.
- **`repowise distill <cmd>`**: reversible shell-output compression (61–89% on pytest/git
  log/git diff), errors-first, inline `[repowise#<ref>]` markers with `repowise expand <ref>`,
  net-positive guard (small outputs pass through untouched).
- **`repowise saved`**: tallies tokens and dollars saved across distill + MCP usage; dashboard
  "Costs" view priced at the agent's model rates.
- **Fast incremental indexing**: `repowise update` <30 s, git-aware skip of unchanged files;
  worktrees auto-seed from base checkout.
- **Deterministic analytics with zero LLM in the indexing path**: hotspots (churn ×
  complexity), co-change coupling, bus factor, 25 code-health markers, dead-code detection with
  confidence tiers, diff risk scoring (Kamei-style, 0–10) with `will_break` /
  `missing_cochanges` / `missing_tests` directives, `impacted-tests <range>`.
- **`_meta` envelope on every MCP response**: `index_age_days`, `indexed_commit`,
  `stale_warning`.
- **Session-aware learning**: mines durable decisions from Claude Code transcripts; wiki
  generation budget tilts toward frequently-queried modules.

### 1.3 What Cortex already does well (no action needed)

- Token-budgeted, graph-ranked bundles with skeleton degradation (`src/cortex/bundle.py`) —
  comparable to rtk's aggressive read, but proactive and rank-aware.
- 8 task-shaped MCP tools with `concise` default response format and per-tool token budgets
  (`src/cortex/mcp/tools.py`) — same philosophy as Repowise's tool surface.
- Incremental auto-refresh on read tools via repo fingerprint (`_ensure_fresh`,
  `src/cortex/ingest.py`), reported under `auto_refreshed`.
- Deterministic, stdlib-only, local-first core; LLM strictly optional (`enrich` extra) with an
  LLM response cache and cost ledger already in the schema (`llm_cache`, `cost` tables in
  `src/cortex/store.py`).
- `cortex benchmark` (corpus-vs-bundle token comparison) and a 13-task eval harness with
  precision/recall/token/latency reporting.

---

## 2. Gap analysis summary

| Capability | rtk | Repowise | Cortex today |
|---|---|---|---|
| Token-savings ledger & report | `rtk gain` | `repowise saved` | ❌ only LLM-enrich cost ledger; MCP tool usage untracked |
| Full-text / hybrid search | n/a | FTS + vectors + RRF | ❌ `LIKE`-based symbol-name search only (`store.search_nodes`) |
| Incremental update speed | n/a | <30 s, git-aware | ⚠️ re-reads every file, rewrites entire graph table on each incremental pass |
| Query result caching | n/a | index-side caching | ❌ PageRank + packing recomputed per call |
| Batched context tool | n/a | `get_context(targets[])` | ❌ one target per `cortex_impact`/`cortex_read_symbol` call |
| Hotspots (churn × complexity) | n/a | core feature | ❌ co-change edges exist, no churn/complexity scoring |
| Dead-code detection | n/a | confidence-tiered | ❌ graph has the edges, no report |
| Diff/PR risk scoring | n/a | Kamei-style + directives | ❌ |
| Shell-output distill + savings recovery | core feature | `distill`/`expand` | ❌ (README delegates to external `tokenslim`) |
| Aggressive read modes | `read -l aggressive` | `get_symbol` | ⚠️ skeletons exist in bundles only, not in read tools |
| Tokenizer accuracy | n/a (measures real) | model-priced | ⚠️ heuristic regex estimate, uncalibrated (`tokenizer.py`) |
| Staleness metadata envelope | n/a | `_meta` on every response | ⚠️ partial (`auto_refreshed`, stale hints) |
| Missed-savings discovery | `rtk discover` | demand-weighted docs | ❌ |

---

## 3. Work items

Priorities: **P0** = highest leverage on Cortex's core promise (token-efficient repo context),
**P1** = strong differentiators with moderate effort, **P2** = valuable but optional/larger.

Ground rules for all items (Cortex invariants — do not violate):

- Core stays **stdlib-only** (SQLite FTS5 ships with CPython's `sqlite3`; `tiktoken`/embeddings
  must live behind optional extras). No network, no vector DB in the default path.
- Everything deterministic and testable; add pytest coverage under `tests/` and, where retrieval
  quality is affected, eval tasks under `evals/`.
- Follow the existing schema-migration pattern (`CortexStore._migrate_existing_schema`,
  `src/cortex/store.py:188`) for any new tables/columns.
- Update `README.md`, `CHANGELOG.md`, `skills/cortex/SKILL.md`, and `hooks/session-start.py`
  tool listings whenever the MCP/CLI surface changes (a past bug class — see 0.6.0 changelog).

---

### P0-1. Token-savings ledger and `cortex saved` command

**Motivation**: rtk's `gain` and Repowise's `saved` make savings *visible*, which drives
adoption and validates the product claim. Cortex computes token counts for every response
already but throws the numbers away.

**Current state**: `bundles.total_tokens` is persisted per bundle (`store.py`), and a `cost`
table exists but only records LLM enrichment tokens (`record_cost`, `store.py:715`). MCP tool
calls (`call_tool`, `src/cortex/mcp/tools.py:556`) record nothing.

**Implementation steps**:
1. Add a `tool_usage` table via `_migrate_existing_schema`: `(id, repo_path, called_at, tool,
   response_tokens, baseline_tokens, meta_json)`.
2. In `call_tool` (mcp/tools.py), after each successful tool dispatch, compute
   `response_tokens = count_text_tokens(json.dumps(payload))` and a **deterministic baseline**:
   - `cortex_query` / `cortex_read_symbol` / `cortex_impact` / `cortex_references`: sum of
     `count_text_tokens(source.content)` for every distinct file referenced in the response
     (the tokens an agent would have spent reading those files raw). Reuse
     `store.fetch_source_content`.
   - `cortex_search_symbols` / `cortex_relations` / `cortex_overview`: baseline = tokens of the
     `detailed` rendering of the same payload (savings of the concise format), plus referenced
     files for relations.
   Keep this in one helper (`_estimate_baseline(payload) -> int`) so the policy is auditable.
3. Write one row per call; make failures to write non-fatal (savings tracking must never break
   a tool response).
4. Add CLI `cortex saved [repo] [--daily] [--format text|json]` in `cli.py`: totals (calls,
   response tokens, baseline tokens, saved tokens, save %), optional day-by-day rollup, and an
   optional `--price-per-mtok <in>,<out>` flag to render dollar figures without hardcoding
   model prices.
5. Docs: README section mirroring the eval-numbers table style.

**Acceptance criteria**: after a `cortex_query` + `cortex_read_symbol` session against a fixture
repo, `cortex saved` reports non-zero savings; unit tests cover baseline computation for each
tool family; MCP responses unchanged except optional `_meta.saved_tokens` (see P1-5).

**Effort**: S–M. **Dependencies**: none.

---

### P0-2. SQLite FTS5 full-text layer with rank fusion

**Motivation**: Repowise's hybrid retrieval (FTS + vectors via RRF) is what lets `get_answer` /
`search_codebase` replace grep sessions. Cortex's `search_nodes` (`store.py:558`) is
`LIKE`-based over node labels/signatures/paths only — body text, docstrings, and Markdown
content are invisible to search, which forces agents back to raw grep (the exact failure mode
Cortex exists to prevent). FTS5 is in the stdlib `sqlite3` build, so this preserves the
zero-dependency invariant. Embeddings stay out of scope (local-first invariant).

**Implementation steps**:
1. In `initialize_schema`, create `CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
   repo_path UNINDEXED, path UNINDEXED, kind UNINDEXED, content, tokenize='unicode61')` —
   guard with a runtime check (`PRAGMA compile_options` or a try/except on creation) and fall
   back cleanly to the current behavior when FTS5 is unavailable.
2. Keep it in sync inside `save_sources` / `delete_sources` / `reset_repo` (delete+insert per
   path — same delta granularity as the `sources` table).
3. Add `CortexStore.search_fulltext(repo_path, query, limit) -> list[(path, bm25, snippet)]`
   using FTS5 `bm25()` and `snippet()`.
4. Bundle ranking (`bundle.py::generate_bundle`): add FTS results as a third seed source next
   to the existing name-match/keyword scoring. Fuse with **RRF** (`score += 1/(60+rank)` per
   list) rather than raw-score mixing so BM25 and the existing bonuses don't need scale
   calibration; keep `NAME_MATCH_BONUS` dominance for exact stem/symbol hits (regression risk:
   the 0.7.x ranking fixes — run the eval suite).
5. New MCP tool `cortex_search_text(query, limit, budget)` returning path + line-anchored
   snippets, so agents get a grep replacement that reads from the index instead of the tree.
6. Add eval tasks where the gold file is findable only via body text (e.g. an error-message
   string), and assert precision does not regress on the existing 13 tasks.

**Acceptance criteria**: eval suite passes with new body-text tasks; `cortex_search_text`
returns snippets under budget; graceful no-FTS5 fallback tested (monkeypatched failure).

**Effort**: M. **Dependencies**: none; do before P1-2 (hotspot ranking) to settle fusion code.

---

### P0-3. Fast-path incremental ingest (delta writes, no full-graph rewrite)

**Motivation**: Repowise's "<30 s incremental update, git-aware skip" is table stakes for the
auto-refresh-on-read pattern Cortex uses — every read tool may trigger `_ensure_fresh`, so
incremental latency is *query* latency.

**Current state** (`src/cortex/ingest.py`): even in incremental mode, `_scan_sources` **reads
and hashes every file in the repo** (line 134–155) before diffing; the graph update loads the
**entire graph into memory** (`fetch_graph`), filters in Python, and `replace_graph`
(`store.py:281`) **deletes and rewrites every node/edge row** for the repo. Cost is O(repo)
per changed file.

**Implementation steps**:
1. Stat-first scan: reuse the fingerprint walk's `(path, size, mtime_ns)` triples; only read +
   hash files whose stat triple changed since the stored record (store `mtime_ns`/`size` are
   already in `sources`). Fall back to hashing when stats are equal but a force flag is set.
2. Delta graph writes: add `CortexStore.delete_graph_for_sources(repo_path, paths)` (DELETE
   nodes `WHERE source_ref IN (…)` and edges `WHERE json_extract(metadata_json,'$.source_file')
   IN (…)` — add a real `source_file` column if the JSON extract is too slow; migration
   pattern exists) and `append_graph(...)`. Replace the fetch-filter-replace sequence in
   `ingest_repository`'s incremental branch.
3. COCHANGE correctness: co-change edges derive from commits, not file contents
   (`cochange.py`); rebuild only the COCHANGE layer when `collect_recent_commits` reports new
   SHAs, and keep it untouched otherwise. Verify `build_graph(sources_to_process, commits)`
   doesn't currently produce duplicate co-change edges on incremental runs (suspected bug:
   merged with `filtered_edges` which retains prior COCHANGE edges whose `source_file` is
   unset — write a regression test first).
4. Add indexes: `CREATE INDEX IF NOT EXISTS idx_nodes_source_ref ON graph_nodes(repo_path,
   source_ref)` and the edge equivalent.
5. Add a perf harness (`evals/perf_ingest.py` or pytest marker): synthetic 2,000-file repo;
   assert warm single-file-change refresh does O(changed) file reads (instrument via a counter,
   not wall time) and completes < 1 s on CI hardware.

**Acceptance criteria**: single-file change triggers content read of exactly that file;
node/edge rows for unchanged files have unchanged rowids (proves no rewrite); all existing
ingest/store tests pass; regression test for duplicated COCHANGE edges.

**Effort**: M. **Dependencies**: none. Highest engineering-risk item — require the regression
test suite before refactoring.

---

### P1-1. Batched triage tool: `cortex_context(targets[])`

**Motivation**: Repowise attributes its "−70% tool calls" largely to `get_context` accepting a
**batch** of files/symbols and returning compact triage cards. Cortex forces one round-trip per
target (`cortex_impact`, `cortex_read_symbol`), and each round-trip costs agent tokens and
latency.

**Implementation steps**:
1. New MCP tool `cortex_context` in `mcp/tools.py`: `targets: list[str]` (paths or symbol
   names, resolved via the existing `_resolve_symbol` / file-node lookup), `budget` (default
   2000), `include: list[str]` optional expansions (`"impact"`, `"cochange"`, `"symbols"`).
2. Per-target card (concise by default): resolved id, kind, signature or heading list, top-3
   structural neighbors, top-3 co-change partners with weights (reuse `rank_file_impact`,
   `impact.py:25`), span info, and `hotspot` bit once P1-2 lands.
3. Split the budget across targets like `ITEM_BUDGET_SHARE` does in `bundle.py`; report
   `truncated` per card.
4. Single `_ensure_fresh` call for the whole batch (not per target).
5. Skill/hook guidance update: "before editing several files, call `cortex_context` once with
   all of them."

**Acceptance criteria**: one call with 5 targets returns 5 cards under budget with correct
resolution for both paths and symbol names; unit tests for ambiguous-symbol handling (reuse
non-error disambiguation pattern from `_call_read_symbol`).

**Effort**: S–M. **Dependencies**: better with P1-2, not blocked by it.

---

### P1-2. Hotspot analytics: churn × complexity

**Motivation**: Repowise's highest-signal cheap analytic. Cortex already stores commit history
(`commits` table) and co-change coupling but computes no per-file churn or complexity, so
ranking can't prefer "frequently changed AND complex" files, and reports can't warn about them.

**Implementation steps**:
1. New module `src/cortex/hotspots.py`: `compute_churn(commits) -> dict[path, int]` (commit
   touch counts, optionally recency-weighted with exponential decay over `authored_at`);
   `estimate_complexity(source) -> int` — deterministic proxy: count of branch keywords
   (`if/for/while/case/catch/&&/||/elif/except`) via the existing structural backends' line
   scan, normalized per KLOC. No new parser work.
2. Persist as node metadata (`metadata_json.hotspot = {churn, complexity, score}`) during
   ingest, or a small `file_stats` table if metadata writes complicate the P0-3 delta path.
3. Surface: (a) `cortex_overview` gains a `top_hotspots` list; (b) `report.py` gains a
   "Hotspots" section next to god-nodes; (c) optional ranking multiplier in `bundle.py` behind
   a parameter (`hotspot_boost=False` default — measure on evals before enabling).
4. `cortex_impact` response gains `hotspot` fields on neighbors (cheap join).

**Acceptance criteria**: fixture repo with a deliberately churned file ranks it top hotspot;
eval metrics unchanged with boost off; report renders the section.

**Effort**: S–M. **Dependencies**: coordinate metadata storage with P0-3.

---

### P1-3. Query/bundle result cache keyed on fingerprint

**Motivation**: rtk's whole pitch is sub-10 ms overhead; Repowise caches index-side. Cortex
recomputes PageRank over the full graph and re-packs on **every** `cortex_query`, even when
nothing changed. Repeated queries (common in agent loops) should be near-free.

**Implementation steps**:
1. Table `query_cache (repo_path, cache_key TEXT, created_at, payload_json, PRIMARY KEY
   (repo_path, cache_key))`; `cache_key = sha256(fingerprint + tool + canonical_json(args))`.
2. Wrap `_call_query` (and `_call_impact`, `_call_overview`) in a read-through cache; the
   fingerprint is already computed by `_ensure_fresh`, so hits cost one SELECT.
3. Invalidation is automatic (fingerprint in key); add LRU-ish pruning (`DELETE` rows older
   than N days or beyond M rows per repo) inside `cortex gc`.
4. Expose `"cached": true` in `_meta` (P1-5) and honor `CORTEX_QUERY_CACHE=0` env kill-switch.

**Acceptance criteria**: second identical `cortex_query` performs no PageRank (assert via call
counter/monkeypatch) and returns byte-identical payload; a file edit changes the fingerprint
and misses the cache; `gc` prunes.

**Effort**: S. **Dependencies**: none.

---

### P1-4. Tokenizer calibration + optional exact tokenizer extra

**Motivation**: Budgets drive packing decisions (skeleton vs full file, truncation), and
savings reports (P0-1) are only credible if counts are near-real. `count_text_tokens`
(`src/cortex/tokenizer.py`) is a regex segment count — for code it typically **overestimates**
vs cl100k/o200k BPE, meaning bundles under-fill their budgets and leave value on the table.

**Implementation steps**:
1. Dev-time calibration script (`evals/calibrate_tokenizer.py`): compare `count_text_tokens`
   vs `tiktoken` (o200k_base) across the repo's own sources per kind (code/markdown/text);
   emit per-kind ratio table.
2. Bake calibration factors into `tokenizer.py` as constants (e.g.
   `CALIBRATION = {"code": 0.72, "markdown": 0.85, ...}` — measured values, not guesses) and
   apply in `count_text_tokens(text, kind="text")`; thread `kind` through call sites that know
   it (`bundle.py`, `benchmark.py` have `SourceRecord.kind`).
3. Optional `[tokens]` extra adding `tiktoken`: when importable, use it directly (cached
   encoder), else calibrated heuristic. Mirror the `regex` soft-import pattern already in the
   file.
4. Re-run evals and `cortex benchmark` and update README numbers.

**Acceptance criteria**: calibration script reproducible; heuristic-vs-tiktoken error within
±15% per kind on the Cortex repo corpus; no behavior change when `tiktoken` absent beyond the
calibration factors; evals updated.

**Effort**: S. **Dependencies**: do before or with P0-1 so savings numbers are calibrated.

---

### P1-5. Standard `_meta` envelope on all MCP responses

**Motivation**: Repowise stamps every response with `index_age_days`, `indexed_commit`,
`stale_warning`, which lets agents decide when to trust vs refresh without extra calls. Cortex
has pieces (`auto_refreshed`, stale hints in `_staleness`, `mcp/tools.py:187`) but no uniform
envelope, and each tool formats status differently.

**Implementation steps**:
1. Define `_meta = {index_age_seconds, indexed_at, fingerprint_fresh: bool, auto_refreshed?,
   cached?, saved_tokens?}` assembled in one place (`_format_payload`, `mcp/tools.py:169`).
2. Keep `concise` mode lean: include `_meta` only when something is noteworthy (stale, just
   refreshed, cached); always include in `detailed`.
3. Fold P0-1's `saved_tokens` and P1-3's `cached` flags here.

**Acceptance criteria**: all 8+ tools emit the envelope through the shared path; tests assert
schema stability; concise responses gain ≤ ~20 tokens in the noteworthy case, 0 otherwise.

**Effort**: XS–S. **Dependencies**: lands naturally with P0-1/P1-3.

---

### P1-6. Aggressive read modes for read tools

**Motivation**: rtk's `read -l aggressive` (signatures only) and `smart` (2-line summary).
Cortex builds skeletons (`_render_skeleton`, `bundle.py:109`) but only inside bundle packing —
`cortex_read_symbol` always returns full span text, and there is no skeleton-read for a whole
file.

**Implementation steps**:
1. `cortex_read_symbol`: add `mode: "full" | "skeleton" | "signature"` (default `full`).
   `skeleton` reuses `_render_skeleton` scoped to the symbol's children; `signature` returns
   signature + span metadata only.
2. New thin tool or extension `cortex_read_file(path, mode="skeleton", budget=...)` returning
   the skeleton rendering of an indexed file — this is the direct raw-Read replacement and
   should be advertised as such in `skills/cortex/SKILL.md`.
3. Record savings for these reads in the P0-1 ledger (baseline = full file tokens).

**Acceptance criteria**: skeleton read of a large Python file returns imports + signatures +
elision markers under budget; modes covered by tests; SKILL.md guidance updated.

**Effort**: S. **Dependencies**: none (P0-1 for savings attribution).

---

### P2-1. `cortex distill` / `cortex expand`: reversible shell-output compression

**Motivation**: This is rtk's entire product and Repowise ships it too (`distill`, 61–89%
savings, errors-first, net-positive guard, reversible `expand`). Cortex's README currently
delegates this to external `tokenslim` ("stacking" section) — that stance is worth revisiting
because (a) the savings ledger (P0-1) can then cover the whole session, and (b) one tool with
one install story beats two.

**Decision required before delegation**: keep delegating to tokenslim (do nothing) vs. build a
minimal distiller. If built, keep it *generic and deterministic* — do not attempt rtk's 100+
per-command filters.

**Implementation steps (if built)**:
1. New module `src/cortex/distill.py` with composable passes: (a) exact-duplicate line collapse
   with `×N` counts; (b) errors-first reordering using a small pattern set
   (`error|fail|exception|traceback|warning`), original order preserved within groups;
   (c) run-length truncation of homogeneous middles (keep head/tail); (d) net-positive guard —
   if compressed ≥ 90% of original tokens, pass through untouched.
2. CLI `cortex distill -- <command…>`: run the command, print distilled output, save raw output
   to `~/.cortex/tee/<timestamp>_<slug>.log`, print a final line
   `[cortex#<ref>] full output: cortex expand <ref>`; `cortex expand <ref>` cats the raw log.
   Exit code passthrough.
3. Record raw-vs-distilled tokens in the P0-1 ledger (`tool = "distill"`).
4. Tee retention handled by `cortex gc`.

**Acceptance criteria**: `cortex distill -- python -m pytest tests/` on a failing fixture shows
failures first and ≥40% token reduction on noisy output; passthrough on tiny output; `expand`
restores exact bytes; exit codes preserved.

**Effort**: M. **Dependencies**: P0-1 ledger.

---

### P2-2. Dead-code report

**Motivation**: Repowise's `get_dead_code` (confidence-tiered). Cortex's graph already contains
`imports`/`calls`/`contains`/reference edges — unreferenced symbols are a query away, and dead
code is pure token waste in every future bundle.

**Implementation steps**:
1. `src/cortex/deadcode.py`: symbols with zero incoming `calls`/`imports`/`inherits`/
   `references` edges, excluding entry points (`main`, `__main__`, exported/`__init__`
   re-exports, test files, decorated route handlers where detectable). Confidence tiers:
   `high` (no incoming edges anywhere incl. grep via `references.find_references`), `medium`
   (no graph edges, grep hits only in comments/docs), `low` (dynamic-language caveats).
2. Surface as `cortex report` section + `cortex_dead_code` MCP tool with budget.
3. Be honest about limits: regex-backend languages get `low` confidence by default.

**Acceptance criteria**: fixture with a known-unused function flags it `high`; a
grep-referenced one drops to `medium`; no false `high` on dunder/entry-point symbols.

**Effort**: M. **Dependencies**: none (better after P0-2 for the grep tier).

---

### P2-3. Diff-aware risk and impacted-context: `cortex risk <range>`

**Motivation**: Repowise's `risk main..HEAD` + `missing_cochanges` / `missing_tests` directives
turn the index into a pre-commit guard. Cortex has the co-change layer — the inversion
("you changed A; its co-change partner B is not in the diff") is cheap and uniquely aligned
with Cortex's COCHANGE differentiator.

**Implementation steps**:
1. `src/cortex/risk.py`: parse `git diff --numstat <range>`; per changed file compute churn,
   hotspot score (P1-2), fan-in from structural edges, and **missing co-change partners**
   (partners above weight threshold not present in the diff). Compose a 0–10 score with fixed,
   documented weights.
2. Missing-tests heuristic: reuse `_looks_like_src_test_pair` (`report.py:22`) to detect
   changed sources whose paired test file is untouched.
3. CLI `cortex risk <range>` + MCP `cortex_risk(range?)` (default `HEAD~1..HEAD` /
   staged), concise directive list: `missing_cochange: [b.py (0.8)]`, `missing_tests: [...]`.
4. Eval-style fixture: repo where two files always change together; diff touching one flags
   the other.

**Acceptance criteria**: fixture assertions above; deterministic scores; runs without network
on a plain git repo.

**Effort**: M. **Dependencies**: P1-2 (hotspots).

---

### P2-4. Missed-savings discovery (`cortex discover` analogue)

**Motivation**: rtk's `discover` scans recent sessions for commands that *bypassed* rtk and
quantifies the missed savings — a growth loop. Cortex's equivalent: detect raw `Read`/`grep`
of files that the index could have served skeletonized or via `cortex_search_text`.

**Implementation steps**:
1. Extend the SessionStart hook's companion (or a new `PostToolUse` hook under `hooks/`) to
   append `(tool, file/path, bytes)` lines for raw Read/Grep/Glob calls to a local JSONL under
   `~/.cortex/data/<repo-hash>/usage.jsonl` (strictly local, opt-in via hook install, no
   content captured — paths and sizes only).
2. `cortex discover [repo]`: join the JSONL against the index; for each raw read of an indexed
   file, report tokens spent vs skeleton/`cortex_read_symbol` cost; total the gap.
3. Keep it out of the MCP surface (CLI-only, human-facing).

**Acceptance criteria**: synthetic usage log produces correct per-file and total missed-savings
numbers; zero output when log absent; documented privacy posture (local-only, paths+sizes).

**Effort**: M. **Dependencies**: P0-1 (shared token math), P1-6 (the cheaper alternative must
exist to be recommended).

---

## 4. Explicit non-goals (evaluated and rejected for now)

- **Vector embeddings / semantic search** (Repowise): violates Cortex's no-embedding,
  no-network invariant; FTS5 + graph fusion (P0-2) captures most of the retrieval win locally.
- **LLM-generated wiki/docs layer** (Repowise): outside the optimization/efficiency scope and
  contradicts the deterministic default path; the existing optional `enrich` covers the niche.
- **Per-command output filters for 100+ CLIs** (rtk): unbounded maintenance surface in Python;
  P2-1's generic distiller + tokenslim stacking covers the bulk of the value.
- **Code-health marker suite / refactoring plans** (Repowise's 25 markers, Extract-Class
  plans): large scope, not efficiency-focused; hotspots (P1-2) + dead code (P2-2) deliver the
  highest-signal subset first.
- **Web dashboard / VS Code extension**: transport/UI, not optimization; the HTML graph viewer
  already exists.

## 5. Suggested delegation order

1. **Wave 1 (independent, parallel-safe)**: P0-1 (ledger), P0-2 (FTS), P1-3 (cache), P1-4
   (tokenizer). P0-3 (incremental) in parallel but by a single agent with the regression tests
   written first.
2. **Wave 2**: P1-5 (meta envelope, folds in Wave 1 flags), P1-6 (read modes), P1-1 (batched
   context), P1-2 (hotspots).
3. **Wave 3**: P2-3 (risk, needs hotspots), P2-2 (dead code), P2-1 (distill — after the
   tokenslim decision), P2-4 (discover).

Each wave should end with: `python3 -m pytest tests/ -q`, `python3 evals/run_evals.py` (metrics
must not regress), CHANGELOG entry, and README/SKILL.md surface updates.
