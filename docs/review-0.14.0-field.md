# Cortex 0.14.0: field review (fifth pass)

**Reviewer environment:** private C++/Qt/QML repository, about 3.7k indexed files, 161k nodes and 622k edges. Linux, Python 3.12, no `tiktoken`/`regex` (stdlib token estimate).

**Method:** 0.13.0 and 0.14.0, fresh `CORTEX_DATA_DIR` each, full refresh, one process per cold call, `CORTEX_TOOL_TIMEOUT_SECONDS=30`, one frozen `git clone --local`. Causes in §1.4 come from running `generate_bundle` in-process on the copied indexes with instrumented `_select_seeds` / `_callee_scores`; the other causes are inferred from index contents.

## 0. Summary

| # | Item | 0.14.0 status |
|---|---|---|
| A | `Ns::f()` regression | **fixed** (`dead_code` 12920 -> 12683; 30 of 32 false positives gone) |
| B | risk `files` at larger budget | **fixed** (monotonic on the four pinned ranges) |
| C | test mapping through mock headers | **correct revert**: the 3 mappings dropped belong to tests whose target puts the mock directory first on the include path |
| D | `inherits` gate | **met** (in-repo share 99.6%). External classification verified: 988 edges, no class defined in the repository is classified external. Resolution itself grew only 888 -> 923 symbol |
| E | `calls` gate | **not met**: 38.1% in-repo against 60% |
| E.2 | fake `class QString` nodes | **partial**: dozens -> 3 (anonymous `struct {` / `typedef struct {` still take the first member type as label) |
| F | query recall | **improved** 3 of 4 tasks; no gold file lost |
| new | `dead_code` | 62 added findings vs 0.13 (all `low`) |
| new | risk | `uncovered_changed_files_total` depends on budget |
| new | query | one C++ task lost all call/override expansion |

## 1. New defects

### 1.1 `dead_code` reports namespace nodes (37 of the 62 added)
Nested `namespace A::B` nodes (now emitted correctly by the §E.2 fix) appear as unreferenced. Namespaces are never "called". Fix: exclude `namespace` kind nodes from `dead_code` (or treat any reference to a member as a reference). The other 25 are class constructors and methods; 3 spot-checked ones are all called in the source (in-class calls, and one call from another file). Likely `new X` now targeting constructors, or in-class calls not counted. Needs a fixture: a method called only from the same class.

### 1.2 Anonymous struct label (§E.2 follow-up)
`struct { bool operator()(...) }` and `typedef struct { QString name; }` still get a body identifier as label. Apply the `namespace_definition` rule (label `(anonymous)`, not a class) to `struct_specifier` / `class_specifier` with no name field and no typedef declarator name.

### 1.3 `uncovered_changed_files_total` is budget dependent
300-commit range: 511 at the full budget, 604 at budget 2000 (where `tests_by_changed_file` is skipped and `tests_to_run` is empty). The total should be computed before truncation; files whose tests are merely omitted must not count as uncovered.

### 1.4 Call expansion absent on one task (possible ranking regression)
Task: a one-sentence bug report about a UI item changing type and then being removed; the fix site is one large C++ file (found by both versions).
- 0.13.0 at budget 8000: 53 items, `keyword` 26, `calls` 16, `override` 11; packs a base-class `.cpp` and several subclass `.cpp` files reached through call/override edges.
- 0.14.0 at budget 8000: 31 items, **all `keyword`**; 5 of 9 files are UI QML screens that match the task words. Other tasks in the same run keep `calls`/`override` items.
Traced: all 6 call-expansion seeds are UI-markup spans, which have no outgoing `calls` edges, so the walk returns 2 callees (0.13: 269). `_select_seeds` ranks files by the length-normalised `source_scores`; the large C++ files the task is about drop from 379 / 411 / 340 to 151 / 92 / 150 (rank 2 -> 28) and the small UI files fill the `SEED_FILES` = 5 slots. Counterfactuals on the same index: `LENGTH_NORM = 0` gives 119 callees and 102 items with C++ seeds back; `SEED_FILES = 3` alone gives one C++ seed. Fix proposal: choose seed files by the raw score (the one spans inherit), and skip seeds with no outgoing `calls`.

## 2. Limitations and open gaps

- **Plugin boundary.** Some implementations are reached only through an interface class loaded with `QPluginLoader`; the index has 0 edges between the calling file and the plugin implementation files. No static analysis can add them. A heuristic could link a base class's virtual call to its overriders in `override` edges (it exists), so the gap is the unresolved virtual call below.
- **Unresolved virtual call.** A call `obj.isFoo()` stays `name:isFoo` because the label exists in the base declaration and in an inline override in another header. Proposal: resolve to the base declaration when the receiver type's class (or its ancestors) declares the label, then reach overriders through `override` edges.
- **`calls` 38%.** About 13.6k unresolved are untyped `m_*` members, 14.9k chained calls, plus JS/Java/Python (deferred). The member type is already in the class node metadata (`member_types`), so `m_*` inference is the cheapest next gain.
- **Task gold files.** The T1 gold set used in the field review was approximate. The real fix touches one file, and that file is found by every version, so T1 should be considered met. Please do not weigh T1 in the next acceptance run.

## 3. Claude Code integration: adoption gaps

**Method.** Two sources:
- Tool-use counts from the last 60 main-session transcripts and 64 subagent transcripts on the same repository, with my own retest sessions excluded.
- A read of the 0.14.0 `hooks/session-start.py`, `agents/cortex-explorer.md`, the three skills, and the `tools/list` output (15 tools, about 11k chars of schema).

The host is Claude Code 2.1.310, a native Linux build (no separate Grep/Glob tools; `grep` runs through Bash).

**Finding: organic use is near zero in the main thread.**
- Normal sessions: 0 Cortex calls in the main thread, against 1037 Bash calls and 320 Read calls.
- Subagents: 58 Cortex calls, almost all from agents whose tool list contains only Cortex tools.
- Conclusion: the guidance (MCP instructions, SessionStart context, skills) does not change tool choice while the native tools are available.

**Finding: the cost Cortex can remove is whole-file source reads.**
- Source-file `Read` results: 616 calls averaging about 4k chars, about 2.5M chars in total.
- `cortex_read_symbol` and the `cortex_read_file` skeleton target this directly.
- Logs and docs (about 2M chars) are out of scope.

### 3.1 Outdated-format index: SessionStart message contradicts the deliberate skip
- **Observed:** the index on disk was graph format 5, so it stayed outdated across two upgrades (0.13 and 0.14 each only read it). With 0.14 installed, `session-start.py` says: "the next read/query tool call or cortex_refresh rebuilds it in full". Two consecutive read calls (`cortex_references`, then `cortex_relations`) both answered from the old index with `fingerprint_fresh: false` and `refresh_hint: "The index needs a full rebuild (older graph format); call cortex_refresh"`. Only an explicit `cortex_refresh` rebuilt it (65 s) and it worked.
- **Cause (read from `mcp/tools.py` `_ensure_fresh`):** this is deliberate. An ordinary stale index is refreshed incrementally before every read call, but a format-outdated index is answered from as-is, because a full rebuild takes minutes and exceeds the call budget. The defect is the SessionStart message, which says the opposite.
- **Fix:**
  - Make the message match: "built by an older Cortex; run `cortex_refresh` (full rebuild, about a minute)".
  - Better: start the full rebuild in the background from SessionStart when the state is `outdated`; it must not block the 5 s hook timeout. Read calls keep answering with `stale: true` meanwhile.
- **Related measurement:** an incremental auto-refresh after touching one file took about 13 s end to end on this repository (3.7k files; the whole tree is re-fingerprinted), against a 30 s call deadline. Clients that route to Cortex should not assume a refresh costs "a few seconds".
- **Possibly a local install artifact:** `~/.local/bin/cortex` fails with `ModuleNotFoundError: No module named 'cortex'`, so the `cortex risk --tests-only` command in the `cortex-change-review` skill cannot be run from a shell here.

### 3.2 All tools are deferred behind ToolSearch
- **Observed:** all 15 tools are deferred, while Bash and Read are always loaded. Each session's first Cortex call needs a ToolSearch round trip first, so the model takes the already-loaded native tool.
- **Fix:** Claude Code supports a per-tool `_meta['anthropic/alwaysLoad']` and server-level `alwaysLoad`.
  - Set it per tool on the reliable tier only: `cortex_search_symbols`, `cortex_references`, `cortex_relations`, `cortex_read_symbol`, `cortex_impact`. That is about 3.4k chars of schema, against about 11k for all 15.
  - Keep `query`, `overview`, `dead_code`, `risk`, `path`, `context`, `read_file`, `search_text`, `refresh`, `status` deferred. These are slower, noisier, or rarely the first call.

### 3.3 `cortex-explorer` agent has no model pin and lists unavailable tools
- **No model pin:** the agent has no `model:` field, so it inherits the parent model (often the most expensive one). It is a read-only lookup agent and fits a small model. Add `model: haiku`, or let users override it.
- **Unavailable tools:** it lists `Grep` and `Glob`, which native Claude Code builds no longer have (search runs through Bash). Replace them with `Bash` for literal search and `git`, or drop them.

### 3.4 Duplicated routing text and size of `references` output
- **SessionStart text duplicates the MCP instructions.**
  - The SessionStart `additionalContext` repeats the "prefer Cortex over Grep/Glob/Read, delegate to cortex-explorer" routing, and the MCP server instructions already say this.
  - It costs tokens every session and adds no information.
  - Suggest emitting only the state, e.g. "Cortex index fresh (N files)", plus the refresh hint when needed.
- **`cortex_references` returns locations without text.**
  - Concise `items.code` gives only `path:line`. A plain grep for the same identifier returned 9 lines in 935 chars and 73 ms, with the line text. Cortex returned 11 locations in 1.3k chars with no text, so a follow-up Read is needed.
  - Suggest including a trimmed line snippet in concise mode, so one call replaces grep + Read.
  - Separately, `_meta.saved_tokens` (e.g. 151442 for that call) does not match any observable saving; consider removing it or documenting its basis.

## 4. Verified unchanged / fine
`inherits` targets resolve as `Class @ path:line`; `cortex_impact` on a base header lists derived classes with `inherits` edges; `cortex_path` derived -> base is 1 hop; truncated risk shape (`omitted_fields`, `uncovered_changed_files_total`) kept; graph-format upgrade 6 -> 7 rebuilds in full (65 s) and works; timings flat (refresh 60 s, full `dead_code` 169 s, 300-commit risk 84 s).
