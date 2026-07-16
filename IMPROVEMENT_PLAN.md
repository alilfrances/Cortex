# Cortex Improvement Plan: Optimization & Efficiency Gaps vs. rtk, Repowise, and Semble

Date: 2026-07-16 (revised same day: added MinishLab/semble research and QML/C++/Qt parity
requirements)
Scope: Comparison of Cortex (v0.7.5) against [rtk-ai/rtk](https://github.com/rtk-ai/rtk),
[repowise-dev/repowise](https://github.com/repowise-dev/repowise), and
[MinishLab/semble](https://github.com/MinishLab/semble), focused on **optimization and
efficiency features**. Each work item below is written to be delegated independently to an AI
agent: it names the motivating feature in the reference tool, the current state in Cortex with
file references, concrete implementation steps, and acceptance criteria.

A cross-cutting constraint applies to every item: Cortex's QML / C++ / Qt support is a
first-class differentiator and must be preserved and extended, not regressed — see
[§3 ground rules](#3-work-items) and the per-item Qt requirements.

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

### 1.3 Semble (MinishLab)

Semble is "fast and accurate code search for agents" (~5.6k stars, MIT), the closest
competitor to Cortex's retrieval core specifically. It claims **~98% fewer tokens than
grep+read**. Its efficiency machinery:

- **Dual-retriever fusion, fully local**: static embeddings via
  [Model2Vec](https://github.com/MinishLab/model2vec) (`potion-code-16M` — a static lookup
  model, no transformer forward pass at query time, CPU-only, no API keys or network) +
  **BM25** lexical matching on identifiers, fused with **reciprocal rank fusion (RRF)**.
- **Code-aware ranking signals**: adaptive weighting (symbol-shaped queries get more lexical
  emphasis), definition boosts (chunks *defining* the queried symbol rank higher), identifier
  stem matching, file coherence scoring, and noise penalties for test files/boilerplate
  (Cortex independently arrived at the same aux-path demotion in 0.7.1).
- **Tree-sitter code-aware chunking** — splits on syntactic structure, not lines.
- **Performance**: indexes an average repo in ~250 ms, answers queries in ~1.5 ms, NDCG@10
  0.854 (99% of the quality of a 137M-parameter embedding model, 218× faster indexing);
  94% recall at only 2k tokens vs. grep+read needing ~100k context for 85%.
- **Savings tracking**: `semble savings` per-period statistics (same pattern as rtk `gain` /
  Repowise `saved`); conservative token math `(file chars − snippet chars) / 4`.
- **Weak incremental story**: mtime comparison triggers a *full* index rebuild (MCP mode adds
  a file watcher). Cortex's fingerprint + delta ingest is architecturally ahead here — P0-3
  widens that lead.
- MCP server + CLI + Python library; `.sembleignore`; agent auto-detecting installer.

**Key lesson for Cortex**: local-first and semantic retrieval are not mutually exclusive.
Static embeddings run offline on CPU with numpy-sized dependencies, which fits Cortex's
no-network invariant as an *optional extra* even though the default path stays deterministic
and dependency-free. See P0-2 (fusion layer) and P1-7 (optional static-embedding retriever).

### 1.4 What Cortex already does well (no action needed)

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
- **Qt-native structural extraction none of the three competitors has**: the regex backend
  parses Qt signals/slots sections, `Q_OBJECT`, `emit`/`Q_EMIT`, both pointer-to-member and
  `SIGNAL()/SLOT()` macro `connect()` forms, and QML `signal` declarations, `onFoo:` handlers,
  and component instantiation with local resolution
  (`src/cortex/structural/regex_backend.py:46-72,177,308-440`); the tree-sitter backend covers
  C++ (`tree_sitter_cpp`) and QML (via `tree-sitter-language-pack`, `[qml]` extra) including
  C++ inheritance edges and QML component nodes
  (`src/cortex/structural/treesitter_backend.py:21-28,144-299`); bundle ranking understands
  `qml`/`cpp` language hints (`LANGUAGE_HINT_SUFFIXES`, `src/cortex/bundle.py:50`); and
  `cortex_relations` exposes `emits`/`connects`/`handles` edges. Repowise treats C++ as one of
  15 generic languages and QML not at all; semble is language-agnostic chunking only; rtk does
  not parse code.

---

## 2. Gap analysis summary

| Capability | rtk | Repowise | Semble | Cortex today |
|---|---|---|---|---|
| Token-savings ledger & report | `rtk gain` | `repowise saved` | `semble savings` | ❌ only LLM-enrich cost ledger; MCP tool usage untracked |
| Full-text / lexical search | n/a | SQLite FTS | BM25 | ❌ `LIKE`-based symbol-name search only (`store.search_nodes`) |
| Semantic retrieval, local | n/a | vector embeddings (server/Ollama) | static Model2Vec, CPU-only offline | ❌ |
| Rank fusion (RRF) | n/a | FTS + vectors | BM25 + embeddings + code-aware signals | ⚠️ ad-hoc additive bonuses in `bundle.py` |
| Incremental update speed | n/a | <30 s, git-aware | ❌ full rebuild on any change | ⚠️ delta detection exists but re-reads every file, rewrites entire graph table |
| Query result caching | n/a | index-side caching | index cache dir | ❌ PageRank + packing recomputed per call |
| Query latency | <10 ms overhead | n/a | ~1.5 ms | ⚠️ unmeasured for MCP path; evals report ~14 ms bundle-only on small fixtures |
| Batched context tool | n/a | `get_context(targets[])` | n/a | ❌ one target per `cortex_impact`/`cortex_read_symbol` call |
| Hotspots (churn × complexity) | n/a | core feature | n/a | ❌ co-change edges exist, no churn/complexity scoring |
| Dead-code detection | n/a | confidence-tiered | n/a | ❌ graph has the edges, no report |
| Diff/PR risk scoring | n/a | Kamei-style + directives | n/a | ❌ |
| Shell-output distill + savings recovery | core feature | `distill`/`expand` | n/a | ❌ (README delegates to external `tokenslim`) |
| Aggressive read modes | `read -l aggressive` | `get_symbol` | snippet-only results | ⚠️ skeletons exist in bundles only, not in read tools |
| Tokenizer accuracy | n/a (measures real) | model-priced | chars/4 heuristic | ⚠️ heuristic regex estimate, uncalibrated (`tokenizer.py`) |
| Staleness metadata envelope | n/a | `_meta` on every response | mtime auto-invalidate | ⚠️ partial (`auto_refreshed`, stale hints) |
| Missed-savings discovery | `rtk discover` | demand-weighted docs | n/a | ❌ |
| QML / C++ Qt awareness | ❌ | C++ generic tier only, no QML | ❌ language-agnostic | ✅ **Cortex's edge — must be preserved by every item below** |
| Agent adoption enforcement | PreToolUse hook rewrites Bash commands (deterministic); adoption metrics | task-shaped tools + session-start decision injection | installer offers MCP / AGENTS.md / dedicated sub-agent | ⚠️ advisory SessionStart hook + SKILL.md only |

### 2.1 The adoption-consistency problem

Every measured savings number above assumes the agent actually calls the tool instead of its
built-in `Read`/`Grep`/`Glob` or raw shell. The three projects handle this differently, and
the difference is a spectrum of enforcement strength:

1. **Interception (rtk)** — a PreToolUse hook rewrites Bash commands before execution
   (`git status` → `rtk git status`); the agent never has to remember rtk exists. Ceiling:
   built-in agent tools bypass Bash entirely, so rtk cannot touch `Read`/`Grep`/`Glob` and
   falls back to instruction files for those. rtk also *measures* leakage (`rtk session`
   adoption rate, `rtk discover` missed savings).
2. **Seduction (Repowise)** — no interception; task-shaped tools that beat the raw
   alternative on the first try, `_meta` staleness stamps that let agents trust the index
   instead of re-verifying with raw reads, and session-start injection of mined decisions.
3. **Substitution (semble)** — the installer can register a **dedicated search sub-agent**,
   so exploration happens inside a delegation boundary where semble is the natural/only
   search path; the main agent's grep habit never gets a chance to fire.

Cortex today sits entirely in the weakest (instruction-only) tier: advisory SessionStart
context plus SKILL.md guidance. The plan closes this on all three fronts: incentives
(P0-2/P1-1 make the sanctioned path cheapest), measurement (P2-4), **interception (P1-8 —
new)**, and **substitution (P2-5 — new)**. Notably, P1-8 targets exactly the gap rtk cannot
reach: Cortex, as a plugin with an indexed store, *can* intercept the built-in `Read`/`Grep`/
`Glob` calls that bypass rtk's Bash rewriter.

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
- **QML / C++ / Qt parity (applies to every item)**: Qt-aware extraction is Cortex's
  competitive edge (see §1.4) and its history shows Qt support regresses when features are
  built Python-first (0.6.2, 0.7.2 changelog entries were all Qt/C++ fixes). Therefore:
  - Any item that touches ranking, packing, reading, or reporting must include **C++ (`.cpp`/
    `.hpp` with `Q_OBJECT`, signals/slots sections, `emit`, both `connect()` forms) and QML
    (`.qml` with `signal`, `onFoo:` handlers, component instantiation) fixtures** in its tests,
    exercised through **both** the regex backend (always available) and the tree-sitter backend
    (`[languages]`/`[qml]` extras) when the item's code path differs by backend.
  - **Prerequisite for Wave 1 (assign first, it unblocks all acceptance tests)**: add a shared
    Qt fixture repo to the eval harness — a small git fixture with a C++ `QObject` subclass
    (header + implementation, signals/slots, `emit`, `connect()` wiring), a `.qml` scene that
    instantiates a local component and defines handlers, a `CMakeLists.txt` and `.qrc`
    referencing them, and commit history where the `.qml` and its C++ backend change together
    (feeds COCHANGE-dependent items). Add 2–3 gold-answer eval tasks over it (e.g. "where is
    the <signal> emitted and which slot receives it"). Wire it into `evals/run_evals.py`
    alongside the existing fixtures and keep it small enough that the suite still runs in
    seconds.
  - Never gate a feature on tree-sitter availability: the stdlib regex backend must produce a
    usable (if coarser) result for C++/QML, matching the existing fallback philosophy.

---

### P0-1. Token-savings ledger and `cortex saved` command

**Motivation**: rtk's `gain`, Repowise's `saved`, and semble's `savings` all ship this — every
serious tool in the space makes savings *visible*, because it drives adoption and validates the
product claim. Cortex computes token counts for every response already but throws the numbers
away. Semble's baseline convention (tokens of what the agent *would* have read minus tokens
returned) is the model to follow.

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

**Motivation**: Repowise's hybrid retrieval (FTS + vectors via RRF) and semble's BM25 +
embeddings + RRF are what let their search tools replace grep sessions — semble reports 94%
recall at 2k tokens where grep+read needs ~100k. Cortex's `search_nodes` (`store.py:558`) is
`LIKE`-based over node labels/signatures/paths only — body text, docstrings, and Markdown
content are invisible to search, which forces agents back to raw grep (the exact failure mode
Cortex exists to prevent). FTS5 is in the stdlib `sqlite3` build, so this preserves the
zero-dependency invariant. Transformer embeddings stay out of the default path; the fusion
layer built here must accept **N ranked lists** so the optional static-embedding retriever
(P1-7) plugs in without another ranking rewrite.

**Implementation steps**:
1. In `initialize_schema`, create `CREATE VIRTUAL TABLE IF NOT EXISTS source_fts USING fts5(
   repo_path UNINDEXED, path UNINDEXED, kind UNINDEXED, content, tokenize='unicode61')` —
   guard with a runtime check (`PRAGMA compile_options` or a try/except on creation) and fall
   back cleanly to the current behavior when FTS5 is unavailable.
2. Keep it in sync inside `save_sources` / `delete_sources` / `reset_repo` (delete+insert per
   path — same delta granularity as the `sources` table).
3. Add `CortexStore.search_fulltext(repo_path, query, limit) -> list[(path, bm25, snippet)]`
   using FTS5 `bm25()` and `snippet()`.
4. Bundle ranking (`bundle.py::generate_bundle`): extract a small generic fusion helper
   (`src/cortex/fusion.py`: `rrf_fuse(ranked_lists: list[list[node_id]], k=60) -> dict[node_id,
   float]`) and feed it the existing name-match/keyword ranking plus the new FTS list. RRF
   (`score += 1/(60+rank)` per list) avoids scale calibration between BM25 and the existing
   bonuses; keep `NAME_MATCH_BONUS` dominance for exact stem/symbol hits (regression risk: the
   0.7.x ranking fixes — run the eval suite). Design the helper for N lists (P1-7 adds one).
5. Borrow semble's validated code-aware signals where Cortex lacks them: **definition boost**
   (a chunk/symbol *defining* a queried identifier outranks ones merely mentioning it — Cortex
   has this only via `NAME_MATCH_BONUS`; extend to FTS hits) and **adaptive weighting**
   (queries that look like identifiers — camelCase/snake_case/`::`-qualified — weight the
   lexical/name lists over FTS body hits). Cortex's aux-path demotion (0.7.1) already matches
   semble's noise penalty; don't duplicate it.
6. Identifier-aware indexing for Qt/C++ symbols: FTS5 `unicode61` splits `MyClass::mySignal`
   and camelCase identifiers unhelpfully. Index an auxiliary normalized column (reuse
   `_search_tokens` / `_normalized_identifier`, `store.py:21-28`, which already split
   camel/snake) alongside raw content so both `devicelistmodel` and `device list model` hit.
7. New MCP tool `cortex_search_text(query, limit, budget)` returning path + line-anchored
   snippets, so agents get a grep replacement that reads from the index instead of the tree.
8. Add eval tasks where the gold file is findable only via body text (e.g. an error-message
   string), and assert precision does not regress on the existing 13 tasks.

**Acceptance criteria**: eval suite passes with new body-text tasks; `cortex_search_text`
returns snippets under budget; graceful no-FTS5 fallback tested (monkeypatched failure).
**Qt parity**: on the Qt fixture (see §3 ground rules), searching a signal name, a
`SIGNAL()/SLOT()` macro string, an `onFoo` QML handler name, and a `Class::method` qualified
name each surface the right `.cpp`/`.hpp`/`.qml` file; body-text search finds a string literal
inside a `.qml` file.

**Effort**: M. **Dependencies**: Qt fixture; do before P1-2 (hotspot ranking) and P1-7 to
settle fusion code.

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

### P0-4. Cross-file Qt signal/handler symbol resolution

**Motivation** (surfaced by the Wave 0 fixture build, not speculative): Cortex's structural
backends resolve `emit`/`handles` edge endpoints only *within the same file*. Confirmed on the
`qt_app` fixture: `emit deviceConnected(42)` in `src/DeviceManager.cpp` produces an `emits`
edge to a placeholder `module:deviceConnected` node because the signal is *declared* in
`include/DeviceManager.hpp`, not the `.cpp`; and QML `onFoo:` handlers (`onClicked:`,
`onDeviceConnected:`) never become symbol nodes at all — they emit only a `handles` edge to a
placeholder `module:<name>`. Header/implementation split is the norm in C++, so today the
`emits`/`handles`/`connects` graph is systematically broken across the exact file boundary Qt
code lives on. Several later Qt-parity items depend on these edges resolving to real symbols:
P1-1 (signals a class emits, in the context card), P2-2 (a signal must not be flagged dead
because it has an incoming `emits`/`connects` edge), and P2-3 (a renamed signal whose
`connect()`/QML-handler sites didn't change).

**Implementation steps**:
1. `emit`/`Q_EMIT` resolution: when a signal name isn't a symbol in the current file, resolve
   it against declared signals in `#include`d headers of the same translation unit first
   (Cortex already resolves local includes to file nodes — `resolve_local_import`, 0.6.2), then
   fall back to a unique repo-wide signal-declaration match, then the `module:` placeholder.
   Do this in the regex backend (`regex_backend.py::_extract_qt_cpp_edges`) and mirror in the
   tree-sitter backend; the ingest graph-merge must run it after all files are parsed (signal
   declaration may be ingested after the emitting `.cpp`) — a resolution pass over placeholder
   endpoints at the end of `build_graph`, not per-file.
2. QML `onFoo:` handlers: emit a real symbol node for each handler (kind `func`, `qt:handler`
   metadata) anchored on the handler line, in addition to the existing `handles` edge, so the
   handler is addressable by `cortex_read_symbol`/dead-code/context like any other symbol.
   Resolve the `handles` edge target to the declaring `signal`/property where the on-name maps
   to a known signal in the instantiated component (`onDeviceConnected` → `deviceConnected`
   signal on the `DeviceDelegate` the file instantiates), else placeholder.
3. Keep the placeholder path for genuinely external/unresolved names (Qt framework signals) —
   do not invent symbols for them.
4. Tighten the Wave 0 gold-task `expected_symbols` to the now-resolved ids once available, and
   add relation-level assertions (see acceptance).

**Acceptance criteria**: on `qt_app`, `cortex_relations` for `deviceConnected` shows the
`emits` edge from `DeviceManager::scan` resolving to `include/DeviceManager.hpp:deviceConnected`
(not `module:deviceConnected`); the `connects` edge resolves both endpoints to real
signal/slot symbols; each QML `onFoo:` handler is returned as a symbol by
`cortex_search_symbols`; the `handles` edge for `onDeviceConnected` resolves to the
`DeviceDelegate` `deviceConnected` signal. Regex and tree-sitter backends both covered;
existing non-Qt tests and evals unchanged.

**Effort**: M. **Dependencies**: Qt fixture (Wave 0, done). Should land before P1-1/P2-2/P2-3.

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
   `impact.py:25`), span info, and `hotspot` bit once P1-2 lands. For Qt symbols, the card
   must include the Qt-specific relations already in the graph: signals a class emits, slots
   it defines, and `connects` wiring (query via the existing `store.query_edges` relation
   filter, same source as `cortex_relations`), plus QML→C++ instantiation edges for `.qml`
   targets — this is what makes one `cortex_context` call replace a grep session in a Qt
   codebase.
3. Split the budget across targets like `ITEM_BUDGET_SHARE` does in `bundle.py`; report
   `truncated` per card.
4. Single `_ensure_fresh` call for the whole batch (not per target).
5. Skill/hook guidance update: "before editing several files, call `cortex_context` once with
   all of them."

**Acceptance criteria**: one call with 5 targets returns 5 cards under budget with correct
resolution for both paths and symbol names; unit tests for ambiguous-symbol handling (reuse
non-error disambiguation pattern from `_call_read_symbol`). **Qt parity**: a card for the Qt
fixture's `QObject` subclass lists its signals/slots and connect wiring; a card for the `.qml`
file lists its handlers and the C++ type it instantiates.

**Effort**: S–M. **Dependencies**: better with P1-2, not blocked by it; P0-4 for the Qt
relations in cards (signals emitted / connect wiring) to resolve to real symbols.

---

### P1-2. Hotspot analytics: churn × complexity

**Motivation**: Repowise's highest-signal cheap analytic. Cortex already stores commit history
(`commits` table) and co-change coupling but computes no per-file churn or complexity, so
ranking can't prefer "frequently changed AND complex" files, and reports can't warn about them.

**Implementation steps**:
1. New module `src/cortex/hotspots.py`: `compute_churn(commits) -> dict[path, int]` (commit
   touch counts, optionally recency-weighted with exponential decay over `authored_at`);
   `estimate_complexity(source) -> int` — deterministic proxy: count of branch keywords via
   the existing structural backends' line scan, normalized per KLOC. No new parser work. The
   keyword table must be **per-language**, not Python-defaults: Python
   (`if/elif/for/while/except/and/or`), C++ (`if/for/while/switch/case/catch/&&/||/?:` — count
   `case` so big switch dispatch registers), QML/JS (`if/for/while/switch/case/&&/||/?:` plus
   one point per `onFoo:` handler and per property binding expression, since QML complexity
   lives in bindings, not statements). Comment/string stripping before counting must handle
   `//`, `/* */`, and C++ raw strings — reuse the brace-matcher's skip logic from
   `regex_backend.py` (0.7.2) rather than writing a new one.
2. Persist as node metadata (`metadata_json.hotspot = {churn, complexity, score}`) during
   ingest, or a small `file_stats` table if metadata writes complicate the P0-3 delta path.
3. Surface: (a) `cortex_overview` gains a `top_hotspots` list; (b) `report.py` gains a
   "Hotspots" section next to god-nodes; (c) optional ranking multiplier in `bundle.py` behind
   a parameter (`hotspot_boost=False` default — measure on evals before enabling).
4. `cortex_impact` response gains `hotspot` fields on neighbors (cheap join).

**Acceptance criteria**: fixture repo with a deliberately churned file ranks it top hotspot;
eval metrics unchanged with boost off; report renders the section. **Qt parity**: on the Qt
fixture, the C++ file with a fat `switch` and the `.qml` file with many handlers/bindings score
visibly higher complexity than trivial siblings; churn picks up the co-changing `.qml`+`.cpp`
pair from the fixture history.

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
elision markers under budget; modes covered by tests; SKILL.md guidance updated. **Qt parity**:
skeleton packing was generalized beyond Python in 0.7.0 but read-mode output must be verified
on brace languages explicitly — skeleton of the fixture's C++ header keeps `#include` lines,
the class declaration line, `Q_OBJECT`, `signals:`/`slots:` section markers, and member
signatures while eliding bodies; skeleton of the `.qml` file keeps `import` lines, component
ids, `signal` declarations, and `onFoo:` handler names with bound expressions elided. Test
under both structural backends.

**Effort**: S. **Dependencies**: none (P0-1 for savings attribution).

---

### P1-7. Optional local semantic retriever: static embeddings via a `[semantic]` extra

**Motivation**: Semble proves the assumption behind Cortex's "no embeddings" stance is
outdated: Model2Vec static embeddings (`potion-code-16M`) run **fully offline, CPU-only, no
API keys, no transformer at query time**, with numpy-scale dependencies — and semble reaches
NDCG@10 0.854 with them. Lexical search (P0-2) cannot bridge vocabulary gaps ("auth" vs
`login_handler`); a static-embedding list fused via RRF can, at near-zero runtime cost.

**Constraints**: strictly an optional extra (like `[languages]`/`[llm]`). The default install
must remain stdlib-only with byte-identical output; when the extra is absent, the fusion layer
simply receives one fewer list. Model files must be cached locally (respect `CORTEX_DATA_DIR`)
and fetched only at explicit install/setup time — never during a query or auto-refresh.

**Implementation steps**:
1. Add `[semantic]` extra to `pyproject.toml`: `model2vec` (pulls numpy). Document the model
   download step (`cortex semantic setup`, one-time, ~tens of MB) and the fully-offline
   posture after setup.
2. New module `src/cortex/semantic.py`: embed symbol-level chunks (Cortex already chunks by
   symbol spans — reuse them; embed `signature + docstring/leading comment + first N lines`)
   at ingest time into a `chunk_embeddings` table (`node_id`, `vector BLOB` float32). Delta
   updates ride P0-3's per-file delete/insert path.
3. Query time: embed the task string, brute-force cosine over the repo's vectors (numpy dot —
   at potion-16M dimensions and typical repo sizes this is single-digit ms; no vector DB), emit
   a ranked node list into the P0-2 RRF fusion.
4. Gate everything behind soft imports mirroring the `regex`/`tiktoken` pattern; `cortex
   doctor`-style status line in `cortex_overview.detailed` showing whether semantic is active.
5. Evals: run the suite with the extra on and off; semantic-on must improve or match every
   metric, and a new vocabulary-gap eval task (task words absent from the gold file's
   identifiers) must pass only with semantic on — that task is tagged optional in the harness.

**Acceptance criteria**: default install untouched (test matrix without the extra stays
green and byte-identical); with the extra, vocabulary-gap task passes; ingest overhead with
embeddings < 2× baseline on the eval fixtures; no network after setup (test with sockets
blocked). **Qt parity**: embeddings must be computed for regex-backend symbols too (C++/QML
files without tree-sitter), and a Qt-vocabulary task ("where is the button click handled" →
`onClicked` QML handler) passes with semantic on.

**Effort**: M. **Dependencies**: P0-2 (fusion layer), P0-3 (delta path for embedding updates).

---

### P1-8. PreToolUse redirect hook for built-in `Read` / `Grep` / `Glob`

**Motivation**: rtk proves interception is the only *deterministic* adoption mechanism — but
its Bash-rewriting hook explicitly cannot reach the agent's built-in `Read`/`Grep`/`Glob`
tools, which bypass the shell. Cortex can: it already ships a hook bundle
(`hooks/hooks.json` registers SessionStart with the `${CLAUDE_PLUGIN_ROOT}` launch pattern)
and a read-only fast-path against the index (`hooks/session-start.py` opens the SQLite store
`mode=ro` and compares fingerprints in milliseconds). Intercepting built-in tool calls that
the index could serve cheaper is a consistency mechanism none of the three reference tools
has.

**Design stance**: advisory by default, never lossy. The hook must be **fail-open** in every
path (no index, corrupt DB, timeout, non-git cwd → silent pass-through), mirroring the
SessionStart hook's philosophy, and must never suppress a tool call the index cannot
actually answer.

**Implementation steps**:
1. New `hooks/pre-tool-use.py`, registered in `hooks/hooks.json` under `PreToolUse` with a
   matcher for `Grep`, `Glob`, and `Read`. Hard latency budget: open the store read-only,
   answer from indexed metadata only (never run ingest, PageRank, or file I/O beyond the DB),
   target <50 ms warm / 5 s timeout like the existing hook.
2. Redirect logic, per tool:
   - `Grep` with a pattern that case-normalizes to an indexed symbol/identifier (reuse
     `_normalized_identifier` / `_search_tokens`, `store.py:21-28`): emit advisory context —
     "`<pattern>` is indexed; `cortex_search_symbols` / `cortex_references` answers this in
     ~N tokens" (N from the stored node count, not a guess).
   - `Read` of an indexed source file above a size threshold (from `sources.size_bytes`):
     suggest `cortex_read_symbol`/skeleton read (P1-6) with the estimated token delta.
   - `Glob`: suggest `cortex_search_symbols` only when the pattern embeds an identifier-like
     stem; plain directory globs pass silently.
3. Modes via env/config `CORTEX_HOOK_MODE = off | advise (default) | enforce`. `advise`
   returns non-blocking `additionalContext`; `enforce` returns a deny with the redirect
   message (agent retries with the Cortex tool). Enforce must auto-downgrade to advise when
   the index is stale beyond a threshold, so a blocked grep can never strand an agent against
   an outdated index.
4. Log every interception decision (tool, path/pattern, action, estimated tokens at stake) to
   the local usage log — this **is** the data source P2-4 (`cortex discover`) reads, so build
   the JSONL writer here and let P2-4 consume it (updates P2-4 step 1 from "or a new
   PostToolUse hook" to "the P1-8 hook's log").
5. Effectiveness measurement: extend the eval harness with a scripted adoption scenario
   (recorded raw-tool session replayed against the hook) asserting redirect precision — no
   advice on unindexed targets, correct advice on indexed ones.

**Acceptance criteria**: fail-open verified for missing/corrupt/locked DB and non-git cwd
(hook exits 0, empty output); warm advisory decision <50 ms on the eval fixtures; `enforce`
mode denies with a message naming the exact replacement call; stale-index auto-downgrade
tested; zero advice emitted for files/symbols the index doesn't contain. **Qt parity**: a
`Grep` for a signal name or `onFoo` handler on the Qt fixture gets redirected to
`cortex_references`/`cortex_search_symbols`; a `Read` of the fixture's C++ implementation
file suggests the skeleton read.

**Effort**: M. **Dependencies**: P1-6 (skeleton reads must exist before the hook recommends
them); feeds P2-4. Ship `advise` first; `enforce` only after P2-4 data shows advice is
being followed.

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
2. **Qt meta-object exclusions (correctness-critical, not optional)**: the Qt runtime invokes
   code with no static call edge, so the following are *never* `high` confidence: slots (the
   meta-object system calls them via `connect()` — credit `connects` edges as incoming
   references, both pointer-to-member and string-based `SIGNAL()/SLOT()` forms, which the
   regex backend already parses); signals (invoked by `emit` — credit `emits` edges);
   `Q_INVOKABLE` methods and `Q_PROPERTY` accessors (called from QML by name); any C++ type
   registered via `qmlRegisterType`/`QML_ELEMENT` (instantiated from QML — credit the QML
   instantiation edges from `treesitter_backend.py:249`); QML `onFoo` handlers (invoked by the
   engine); and resources referenced only from `.qrc`/`CMakeLists.txt` (covered by the
   `references` grep tier, which already scans configs — see 0.6.0).
3. Surface as `cortex report` section + `cortex_dead_code` MCP tool with budget.
4. Be honest about limits: regex-backend languages get `low` confidence by default; a `.qml`
   file's connection to C++ via context properties (`setContextProperty`) is string-based and
   must fall to the grep tier.

**Acceptance criteria**: fixture with a known-unused function flags it `high`; a
grep-referenced one drops to `medium`; no false `high` on dunder/entry-point symbols.
**Qt parity**: on the Qt fixture, a connected slot, an emitted signal, and a
QML-instantiated C++ type are all *not* flagged (assert explicitly — these are the false
positives that would destroy trust in Qt shops); a genuinely orphaned private helper method
in the same class still flags.

**Effort**: M. **Dependencies**: Qt fixture; P0-4 (the meta-object exclusions credit
`emits`/`connects`/`handles` edges — those must resolve cross-file first, or slots/signals
get false-flagged); better after P0-2 for the grep tier.

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
3. Qt cross-boundary pairs: the highest-value `missing_cochange` signals in a Qt codebase are
   cross-language — a changed `.hpp` whose `.cpp` is untouched (or vice versa), a changed C++
   class whose instantiating `.qml` is untouched, and a renamed signal whose `connect()` sites
   / QML handlers didn't change. The first comes free from co-change history; the latter two
   should also consult the STRUCTURAL `connects`/instantiation edges so they fire even without
   commit history. Also flag `.qml` additions not referenced by any `.qrc`/`CMakeLists.txt`
   (build-system miss, detectable via the references grep tier).
4. CLI `cortex risk <range>` + MCP `cortex_risk(range?)` (default `HEAD~1..HEAD` /
   staged), concise directive list: `missing_cochange: [b.py (0.8)]`, `missing_tests: [...]`.
5. Eval-style fixture: repo where two files always change together; diff touching one flags
   the other; the Qt fixture's `.qml`+`.cpp` pair exercises the cross-language case.

**Acceptance criteria**: fixture assertions above; deterministic scores; runs without network
on a plain git repo; a diff touching only the Qt fixture's C++ backend flags its co-changing
`.qml` as `missing_cochange`.

**Effort**: M. **Dependencies**: P1-2 (hotspots), Qt fixture, P0-4 (renamed-signal detection
needs `connects`/`handles` edges resolved to real signal symbols).

---

### P2-4. Missed-savings discovery (`cortex discover` analogue)

**Motivation**: rtk's `discover` scans recent sessions for commands that *bypassed* rtk and
quantifies the missed savings — a growth loop. Cortex's equivalent: detect raw `Read`/`grep`
of files that the index could have served skeletonized or via `cortex_search_text`.

**Implementation steps**:
1. Consume the interception log written by the P1-8 hook (`(tool, path/pattern, action,
   estimated tokens)` JSONL under `~/.cortex/data/<repo-hash>/usage.jsonl` — strictly local,
   opt-in via hook install, no content captured, paths and sizes only). If P1-8 hasn't
   shipped, fall back to a minimal `PostToolUse` logger with the same schema.
2. `cortex discover [repo]`: join the JSONL against the index; for each raw read of an indexed
   file, report tokens spent vs skeleton/`cortex_read_symbol` cost; total the gap.
3. Keep it out of the MCP surface (CLI-only, human-facing).

**Acceptance criteria**: synthetic usage log produces correct per-file and total missed-savings
numbers; zero output when log absent; documented privacy posture (local-only, paths+sizes).

**Effort**: M. **Dependencies**: P0-1 (shared token math), P1-6 (the cheaper alternative must
exist to be recommended), P1-8 (log source).

---

### P2-5. Dedicated exploration sub-agent shipped with the plugin

**Motivation**: semble's installer can register a **dedicated search sub-agent**, using the
delegation boundary itself as the adoption mechanism: inside the sub-agent's context, the
tool is the natural search path and the main agent's raw-grep habit never fires. This is
complementary to P1-8 — interception corrects individual calls; substitution restructures
the workflow so exploration is Cortex-native end to end, and the exploration transcript
(often tens of thousands of tokens of search noise) stays out of the main agent's context
entirely. That containment is itself a token-efficiency win independent of which search tool
gets used.

**Implementation steps**:
1. Add an agent definition to the plugin bundle (e.g. `agents/cortex-explorer.md` with
   frontmatter, following the Claude Code plugin agents convention) named `cortex-explorer`:
   a read-only repo-exploration agent whose instructions lead with the Cortex loop
   (`cortex_search_symbols` / `cortex_search_text` → `cortex_read_symbol` / `cortex_context`
   → `cortex_impact`/`cortex_relations`), with raw Read/Grep positioned as last-resort
   fallbacks for unindexed content. Tool access limited to Cortex MCP tools plus read-only
   built-ins; no Edit/Write/Bash.
2. Return contract: instruct the agent to answer with (a) findings, (b) the file/symbol IDs
   consulted with spans, (c) suggested next Cortex calls for the parent — so the parent can
   act without re-exploring (this mirrors what makes Repowise's triage cards effective).
3. Update `skills/cortex/SKILL.md` and the SessionStart hook context to recommend delegating
   multi-step exploration ("where is X handled / how does Y flow") to `cortex-explorer`,
   keeping single lookups as direct tool calls (a sub-agent round-trip costs more than one
   tool call — the skill must say when *not* to delegate).
4. Verify the definition loads from the plugin dir in both distribution modes (marketplace
   install and `--plugin-dir` dev mode); document in README. Check whether the Codex plugin
   manifest supports an equivalent; if not, note the limitation rather than forcing it.
5. Record sub-agent usage in the P0-1 ledger via the tools it calls (no extra plumbing —
   its Cortex calls are ledgered like any other).

**Acceptance criteria**: agent definition loads and is invocable in a plugin-installed
session; its instructions keep exploration within Cortex tools on the eval fixtures
(scripted scenario: an indexed-symbol question answered without any raw Grep call);
SKILL.md documents the delegate/don't-delegate boundary. **Qt parity**: the scripted
scenario includes one Qt fixture question ("which slot receives `<signal>`?") answered via
`cortex_relations`/`cortex_references` inside the sub-agent.

**Effort**: S–M. **Dependencies**: P1-1 (`cortex_context` makes the sub-agent's answers
cheap); P1-8's advisory hook still applies inside the sub-agent as a safety net.

---

## 4. Explicit non-goals (evaluated and rejected for now)

- **Transformer/server-side embeddings** (Repowise's vector layer): violates Cortex's
  no-network default; FTS5 + graph fusion (P0-2) captures most of the retrieval win locally.
  *Revised after studying semble*: **static** embeddings (Model2Vec) are offline, CPU-only,
  and dependency-light, so they moved from non-goal to optional extra — see P1-7. Only
  transformer inference and hosted embedding APIs remain rejected.
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

0. **Wave 0 (single small task, unblocks all acceptance tests)**: the shared Qt fixture repo
   + gold-answer eval tasks described in §3 ground rules.
1. **Wave 1 (independent, parallel-safe)**: P0-1 (ledger), P0-2 (FTS + fusion layer), P1-3
   (cache), P1-4 (tokenizer). P0-3 (incremental) in parallel but by a single agent with the
   regression tests written first.
2. **Wave 2**: P0-4 (cross-file Qt signal/handler resolution — surfaced by Wave 0, unblocks the
   Qt-parity criteria of P1-1/P2-2/P2-3), P1-5 (meta envelope, folds in Wave 1 flags), P1-6
   (read modes), P1-1 (batched context), P1-2 (hotspots), P1-7 (static-embedding retriever —
   needs P0-2's fusion and P0-3's delta path).
3. **Wave 3**: P1-8 (redirect hook, `advise` mode — needs P1-6's skeleton reads to
   recommend), P2-3 (risk, needs hotspots), P2-2 (dead code — assign to an agent briefed on
   the Qt meta-object exclusions), P2-1 (distill — after the tokenslim decision), P2-4
   (discover — consumes P1-8's log), P2-5 (exploration sub-agent — needs P1-1).
4. **Post-Wave 3, data-gated**: flip P1-8 to offer `enforce` mode only after `cortex
   discover` shows advisory redirects are being followed; otherwise iterate on the advice
   wording first.

Each wave should end with: `python3 -m pytest tests/ -q`, `python3 evals/run_evals.py` (metrics
must not regress, including the Qt fixture tasks from Wave 0), CHANGELOG entry, and
README/SKILL.md surface updates.
