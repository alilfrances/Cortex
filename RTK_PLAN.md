# RTK Plan: Shell-Output Compression & Built-in-Tool Interception (separate plugin)

Date: 2026-07-17
Split out of `IMPROVEMENT_PLAN.md` (2026-07-16). Rationale: rtk-style functionality —
compressing shell output and intercepting/redirecting an agent's tool calls — operates on the
**command layer**, while Cortex is a **codebase-intelligence** tool (index once, serve curated
context over MCP). The two compose but do not belong in one plugin. This plan collects the
rtk-derived items (formerly P1-8, P2-1, P2-4) as the seed of a separate plugin.

Reference: [rtk-ai/rtk](https://github.com/rtk-ai/rtk) (Rust Token Killer).

---

## Status / history on branch `claude/cortex-rtk-repowise-analysis-zh2g8p`

- **P1-8 was implemented inside Cortex** in commit
  `fdaa182db353c491b27e58924783b0265c3ebd5a` (`feat: add P1-8 fail-open PreToolUse redirects
  and logging`) and then **reverted in the working tree on 2026-07-17** when this split was
  decided. The reverted implementation is fully recoverable from that commit and is a working
  reference for the standalone plugin: `hooks/pre-tool-use.py` (1048 lines, fail-open,
  read-only SQLite access, `advise`/`enforce`/`off` modes, metadata-only `usage.jsonl`
  logging), `tests/test_pre_tool_use_hook.py` (409 lines), a hook adoption replay in
  `evals/run_evals.py` (`run_hook_adoption_replay`), and `hooks/hooks.json` registration.
- Measured evidence from that implementation (see the pre-split `HANDOFF.md` history):
  replay precision **1.0** / recall **1.0** on four Qt/QML positives and three negatives;
  warmed decision median **1.21 ms** (well under the 50 ms gate); subprocess median
  **102 ms** including Python startup.
- **P2-1 and P2-4 were never implemented.**

---

## 1. What rtk provides (research summary)

rtk is a CLI proxy that compresses command output *before* it reaches an agent's context
window, claiming 60–90% token savings. Its efficiency machinery:

- **Output compression filters for 100+ commands** — test runners (jest/pytest/cargo/go),
  linters, git, package managers, docker/kubectl/aws — using four techniques: smart filtering
  (strip noise/boilerplate), grouping (errors by rule/file, files by directory), truncation,
  and deduplication (repeated log lines collapsed with counts).
- **Failures-only test output** (`rtk pytest`, `rtk test <cmd>`, `rtk err <cmd>`): ~90%
  reduction.
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

### The interception insight

rtk proves interception is the only *deterministic* adoption mechanism — but its
Bash-rewriting hook explicitly cannot reach an agent's built-in `Read`/`Grep`/`Glob` tools,
which bypass the shell. A plugin that reads an existing Cortex index (`mode=ro`) *can*
intercept those built-in calls — the mechanism no reference tool has. That was the original
P1-8 motivation and remains this plugin's strongest differentiator. Cortex needs no changes
to support it: its central SQLite store is already readable by external tooling.

### Relationship to existing tools

- **Cortex** owns the index and the MCP retrieval tools this plugin redirects *to*
  (`cortex_search_symbols`, `cortex_references`, `cortex_read_file`, `cortex_read_symbol`).
  This plugin is a consumer of Cortex's store, never a writer.
- **tokenslim** already covers shell-output slimming in this user's setup, and by prior
  decision skips MCP JSON (server-side concise formats own that). R2 below overlaps
  tokenslim — resolve the delegate-vs-build decision before implementing it.

---

## 2. Work items

Numbering preserved from `IMPROVEMENT_PLAN.md` for traceability (P1-8 → R1, P2-1 → R2,
P2-4 → R3).

### R1 (formerly P1-8). PreToolUse redirect hook for built-in `Read` / `Grep` / `Glob`

**Motivation**: interception is the only deterministic adoption mechanism, and built-in tool
calls are exactly the gap rtk's Bash rewriter cannot reach. A standalone hook consulting a
Cortex index read-only can advise (or enforce) cheaper indexed alternatives.

**Design stance**: advisory by default, never lossy. The hook must be **fail-open** in every
path (no index, corrupt DB, timeout, non-git cwd → silent pass-through) and must never
suppress a tool call the index cannot actually answer.

**Implementation steps** (a complete, tested reference implementation exists in Cortex commit
`fdaa182` — port it rather than rewriting):
1. `pre-tool-use.py` registered under `PreToolUse` with a matcher for `Grep`, `Glob`, and
   `Read`. Hard latency budget: open the store read-only, answer from indexed metadata only
   (never run ingest, PageRank, or file I/O beyond the DB), target <50 ms warm / 5 s timeout.
2. Redirect logic, per tool:
   - `Grep` with a pattern that case-normalizes to an indexed symbol/identifier (Cortex's
     `_normalized_identifier` / `_search_tokens`, `store.py:21-28`): emit advisory context —
     "`<pattern>` is indexed; `cortex_search_symbols` / `cortex_references` answers this in
     ~N tokens" (N from the stored node count, not a guess).
   - `Read` of an indexed source file above a size threshold (from `sources.size_bytes`):
     suggest `cortex_read_file`/`cortex_read_symbol` skeleton read with the estimated token
     delta.
   - `Glob`: suggest `cortex_search_symbols` only when the pattern embeds an identifier-like
     stem; plain directory globs pass silently.
3. Modes via env/config `CORTEX_HOOK_MODE = off | advise (default) | enforce`. `advise`
   returns non-blocking `additionalContext`; `enforce` returns a deny with the redirect
   message (agent retries with the Cortex tool). Enforce must auto-downgrade to advise when
   the index is stale beyond a threshold, so a blocked grep can never strand an agent against
   an outdated index.
4. Log every interception decision (tool, path/pattern, action, estimated tokens at stake) to
   a local JSONL usage log — this **is** the data source R3 reads; build the writer here.
5. Effectiveness measurement: a scripted adoption scenario (recorded raw-tool session
   replayed against the hook) asserting redirect precision — no advice on unindexed targets,
   correct advice on indexed ones.

**Acceptance criteria**: fail-open verified for missing/corrupt/locked DB and non-git cwd
(hook exits 0, empty output); warm advisory decision <50 ms; `enforce` mode denies with a
message naming the exact replacement call; stale-index auto-downgrade tested; zero advice
emitted for files/symbols the index doesn't contain. **Qt parity**: a `Grep` for a signal
name or `onFoo` handler on a Qt fixture gets redirected to
`cortex_references`/`cortex_search_symbols`; a `Read` of a C++ implementation file suggests
the skeleton read. (All already demonstrated by the `fdaa182` reference implementation.)

**Effort**: S–M as a port of `fdaa182` into a standalone plugin skeleton (hooks.json,
plugin.json, its own test suite); M from scratch. **Dependencies**: an existing Cortex index;
Cortex's `cortex_read_file` skeleton reads (landed, P1-6). Ship `advise` first; `enforce`
only after R3 data shows advice is being followed.

---

### R2 (formerly P2-1). `distill` / `expand`: reversible shell-output compression

**Motivation**: rtk's entire product (Repowise ships it too: `distill`, 61–89% savings,
errors-first, net-positive guard, reversible `expand`).

**Decision required first**: keep delegating to `tokenslim` (do nothing) vs. build a minimal
distiller in this plugin. If built, keep it *generic and deterministic* — do not attempt
rtk's 100+ per-command filters (explicit non-goal: unbounded maintenance surface).

**Implementation steps (if built)**:
1. A distill module with composable passes: (a) exact-duplicate line collapse with `×N`
   counts; (b) errors-first reordering using a small pattern set
   (`error|fail|exception|traceback|warning`), original order preserved within groups;
   (c) run-length truncation of homogeneous middles (keep head/tail); (d) net-positive
   guard — if compressed ≥ 90% of original tokens, pass through untouched.
2. CLI `distill -- <command…>`: run the command, print distilled output, save raw output to a
   tee dir (`~/.rtkplugin/tee/<timestamp>_<slug>.log` or similar), print a final line
   `[ref] full output: expand <ref>`; `expand <ref>` cats the raw log. Exit code passthrough.
3. Record raw-vs-distilled tokens in a savings ledger.
4. Tee retention handled by a `gc` command.

**Acceptance criteria**: `distill -- python -m pytest tests/` on a failing fixture shows
failures first and ≥40% token reduction on noisy output; passthrough on tiny output;
`expand` restores exact bytes; exit codes preserved.

**Effort**: M. **Dependencies**: the tokenslim decision.

---

### R3 (formerly P2-4). Missed-savings discovery (`discover` analogue)

**Motivation**: rtk's `discover` scans recent sessions for commands that *bypassed* rtk and
quantifies the missed savings — a growth loop. Equivalent here: detect raw `Read`/`grep` of
files a Cortex index could have served skeletonized or via its search tools.

**Implementation steps**:
1. Consume the interception log written by the R1 hook (`(tool, path/pattern, action,
   estimated tokens)` JSONL — strictly local, no content captured, paths and sizes only).
   The `fdaa182` reference wrote it to `<CORTEX_DATA_DIR>/<repo-hash>/usage.jsonl`; a
   standalone plugin should use its own data dir.
2. `discover [repo]`: join the JSONL against the Cortex index; for each raw read of an
   indexed file, report tokens spent vs skeleton read cost; total the gap.
3. CLI-only, human-facing (no MCP surface).

**Acceptance criteria**: synthetic usage log produces correct per-file and total
missed-savings numbers; zero output when log absent; documented privacy posture (local-only,
paths+sizes).

**Effort**: M. **Dependencies**: R1 (log source); Cortex index for the join.

---

## 3. Non-goals

- **Per-command output filters for 100+ CLIs**: unbounded maintenance surface; R2's generic
  distiller + tokenslim stacking covers the bulk of the value.
- **Writing to or refreshing the Cortex index**: this plugin is read-only over Cortex's
  store; index lifecycle stays with Cortex.

## 4. Suggested order

1. Decide packaging (standalone Claude Code plugin repo vs. tokenslim extension).
2. R1 as a port of `fdaa182` (`advise` mode only).
3. R3 (consumes R1's log).
4. R2 only after the tokenslim delegate-vs-build decision.
5. Data-gated: enable R1 `enforce` mode only after R3 shows advisory redirects are being
   followed; otherwise iterate on the advice wording first.
