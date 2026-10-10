# Fix plan: MCP server wedges under concurrent `cortex_*` batches

Status: proposed
Affects: cortex 0.9.1 (`src/cortex/mcp/server.py`, `src/cortex/deadcode.py`, `src/cortex/references.py`, `src/cortex/mcp/tools.py`)

## Summary of the diagnosis

The report describes the symptom accurately but attributes it to the wrong layer.
There is no deadlock, no lock contention, and no infinite loop. There are two
independent defects that compose into the observed wedge:

1. **A genuine multi-minute CPU burn inside `cortex_overview`.** `analyze_dead_code`
   runs a full-corpus regex scan *per class candidate*. Measured at ~600 ms per
   candidate on a synthetic 3600-file / 43.8 MB C++ corpus, scaling linearly.
   A monorepo with ~1000 class symbols lacking inbound graph edges therefore
   spends ~10 minutes in one call — matching the reported "10+ minutes, state `R`,
   CPU advancing 1:1 with wall clock".
2. **A strictly serial request loop with no timeout and no progress signal.**
   `server.py` reads and handles one JSON-RPC frame at a time, so every other
   request in the batch is head-of-line blocked behind that burn.

Defect 1 is why one call takes minutes. Defect 2 is why *the whole batch* dies
rather than just that call. Both need fixing; fixing only the concurrency model
would leave `cortex_overview` taking 10 minutes, and fixing only the hot loop
would leave the server one pathological repo away from the same wedge.

The "separate idle `cortex-mcp.py` (different PID, `S` state)" observation is
consistent with this: that worker simply never received an expensive call. It
confirms per-process scope, as the reporter concluded.

## Root cause 1 — quadratic full-corpus scans in dead-code analysis

`cortex_overview` → `build_report_data` (`src/cortex/report.py:124`) →
`analyze_dead_code` (`src/cortex/deadcode.py:179`).

For every symbol node with no inbound graph edges, `analyze_dead_code` calls
`_candidate_reason`, which has three separate unbounded costs:

### 1a. Per-class regex scan over every source file — the dominant cost

`_qt_macro_flags` (`src/cortex/deadcode.py:126-131`):

```python
if not qml_registration and node.kind == "class":
    qml_registration = any(
        re.search(rf"\bqmlRegisterType\s*<\s*{label}\s*>", source.content)
        or re.search(rf"\bqmlRegisterType\s*\([^)]*\b{label}\b", source.content)
        for source in all_sources
    )
```

Per class candidate this is `2 × len(all_sources)` regex searches over full file
contents. Worse, the patterns are interpolated with `node.label`, so each candidate
produces two *new* pattern strings; with thousands of distinct labels the 512-entry
`re` cache thrashes and the patterns are recompiled on nearly every search. The
`[^)]*` in the second pattern also backtracks on long lines.

Measured (`3600` files, `43.8 MB`, distinct labels):

| class candidates | wall time | per candidate |
|---|---|---|
| 5  | 2.94 s  | 588 ms |
| 20 | 12.09 s | 605 ms |
| 50 | 29.78 s | 596 ms |

Linear in candidates, with a very large constant. 1000 candidates ≈ 10 minutes.
This alone reproduces the report.

### 1b. Per-candidate filesystem walk

`_reference_tier` → `find_references` → `_grep_hits` (`src/cortex/references.py:93`)
walks the repo, `read_text()`s every candidate file, and regex-matches every line —
once per dead-code candidate. With a warm page cache this presents as CPU-bound,
not I/O-bound, which is why `strace` would have shown little even had ptrace been
permitted.

### 1c. Redundant per-symbol content fetches

`analyze_dead_code:224` calls `fetch_source_content` per symbol, and
`_candidate_reason:167` immediately fetches the *same* file again. With ~30 symbols
per file that is ~60 full-content materializations per file, uncached.

### 1d. Quadratic token accounting

`find_references:133-134` rebuilds the entire `items` dict and re-tokenizes it for
every hit. Same pattern in `truncate_dead_code_result` (`deadcode.py:250`),
`_call_relations` (`tools.py:1711`), `_call_search_text` (`tools.py:1849`), and
`_context_fit_card` (`tools.py:1242-1249`). Secondary, but it multiplies 1a-1c.

### 1e. Repeated graph loads per overview call

`_call_overview` already holds a `CortexStore`, but `build_report_data` constructs
its own and re-runs `fetch_graph`; `_overview_detailed_capabilities` runs a third
`fetch_graph`. On a `detailed` response, `_record_tool_usage` → `_estimate_baseline`
→ `_detailed_rendering_tokens` (`tools.py:1940`) re-enters `_call_overview`,
re-running report fitting and capability collection a second time.

### 1f. Cache does not protect against the common case

The analysis cache key includes the repo fingerprint
(`_overview_analysis_cache_key`, `tools.py:639`). Any edit changes the fingerprint,
so the next `cortex_overview` recomputes from scratch. During active development —
exactly when an agent batches these calls — the cache essentially never hits.

## Root cause 2 — serial dispatch, no timeout, no observability

`src/cortex/mcp/server.py:71`:

```python
for line in sys.stdin:
    ...
    response = _handle(frame)
    ...
    sys.stdout.write(...)
```

- One frame is read, fully handled, and answered before the next is read.
- `grep -rn "threading\|asyncio\|ThreadPool\|signal\.\|timeout" src/cortex/mcp/`
  returns nothing: there is no concurrency and no time bound anywhere in the server.
- A long call therefore blocks reading, so queued frames sit in the pipe buffer
  until the client abandons them at 120 s. Nothing is "dropped" server-side; the
  client gave up.
- There is no request-queue or current-operation indicator, so the state is
  unobservable from outside, as the report notes.

## Fix plan

Ordered by payoff. Phase 1 alone should resolve the reported wedge; phases 2-3
make it structurally impossible to recur.

### Phase 1 — remove the quadratic scans (highest payoff, lowest risk)

1. **Precompute the QML registration index once per analysis run.**
   Replace the per-candidate `all_sources` scan in `_qt_macro_flags` with a single
   pass over the corpus that extracts every `qmlRegisterType<...>` / `qmlRegisterType(...)`
   type name into a `set[str]`, built once in `analyze_dead_code` and passed down.
   The membership test then becomes `label in qml_registered`. This turns
   `O(candidates × corpus)` into `O(corpus)` and is the single change that fixes
   the report. Use one precompiled module-level pattern; do not interpolate labels
   into patterns.

2. **Build one inverted index for reference lookups.**
   Replace the per-candidate `_grep_hits` walk with a token → `(path, line)` index
   built once per `analyze_dead_code` run from `all_sources` (already in memory),
   and have `_reference_tier` query it. Keep `find_references`' existing behaviour
   for the standalone `cortex_references` tool, but give it an optional injected
   index so the dead-code path does not re-walk the tree per symbol.

3. **Memoize source content within a run.** Fetch each file's content once
   (`all_sources` already holds it) and drop the per-symbol `fetch_source_content`
   calls at `deadcode.py:224` and `deadcode.py:167`.

4. **Replace the grow-one-and-re-tokenize loops with incremental accounting.**
   In `find_references`, `truncate_dead_code_result`, `_fit_overview_report`,
   `_call_relations`, `_call_search_text`, and `_context_fit_card`: track a running
   token total and add each item's own cost, or binary-search the cut point
   (`tokenizer._max_prefix_within_budget` already does this and can be generalized).
   Re-serializing the whole accumulator per element is never necessary.

5. **Thread the existing store/graph through `cortex_overview`.** Pass the caller's
   `CortexStore` and the already-fetched `(nodes, edges)` into `build_report_data`
   and `_overview_detailed_capabilities` so one call does one `fetch_graph`.

6. **Cap the analysis cache payload.** The cached overview analysis embeds every
   community's full `node_ids` plus every dead-code finding; on a large monorepo
   that is a multi-MB SQLite blob written on each fingerprint change. Store only
   what `render_report` consumes (top-10 communities with member counts and a
   5-member preview), or compress it.

### Phase 2 — bound every call

7. **Per-request soft deadline.** Add a wall-clock budget (default ~30 s, override
   via `CORTEX_TOOL_TIMEOUT_SECONDS`, `0` to disable) enforced inside `call_tool`.
   Use a cooperative deadline object checked at the natural loop boundaries in
   `analyze_dead_code`, `detect_communities`, and the reference scans — not
   `signal.alarm`, which is not safe once phase 3 introduces worker threads.
   On expiry, return a structured, non-fatal error payload
   (`{"error": "deadline_exceeded", "tool": ..., "elapsed_seconds": ..., "hint": ...}`)
   plus whatever partial result is meaningful. This satisfies the report's
   "fails fast instead of hanging" expectation.

8. **Explicit size guard on `cortex_overview`.** When the candidate set exceeds a
   threshold, return the report with a `dead_code_analysis: "skipped_oversized"`
   marker and a pointer to `cortex_dead_code` with an explicit scope, rather than
   silently spending minutes.

### Phase 3 — make one slow call stop blocking the others

9. **Decouple reading from handling.** Keep a reader loop on stdin that only
   parses frames and enqueues them, and dispatch to a small bounded worker pool
   (default 4, `CORTEX_MCP_WORKERS`). Writes to stdout must go through a single
   mutex so JSON-RPC frames are never interleaved — this is the one correctness
   requirement of the change.
   - JSON-RPC ids already make out-of-order responses legal, so no protocol change
     is needed.
   - `CortexStore` wraps a `sqlite3` connection, which is **not** thread-safe across
     threads by default. Give each worker its own connection (thread-local store
     factory) and open the DB in WAL mode for concurrent readers. This must land
     with the pool, not after it.
   - Keep reads concurrent but serialize `cortex_refresh` and the auto-refresh path
     in `_ensure_fresh` behind a single writer lock — concurrent `ingest_repository`
     runs on one DB are a correctness hazard independent of this bug.

### Phase 4 — observability (the report's explicit ask)

10. **Add a `cortex_status` tool** returning in-flight request count, queue depth,
    per-tool elapsed time for anything currently running, worker count, index
    fingerprint and age. This is what "still working vs. wedged" needs, and it is
    cheap.
11. **Emit structured progress to stderr** (never stdout — that is reserved for
    JSON-RPC frames, per the launcher's existing contract). Log tool start/end with
    elapsed time and candidate counts at a level gated by `CORTEX_MCP_LOG`.
12. **Consider MCP progress notifications** for long calls, so the client can
    distinguish slow from hung without a side-channel.

## Verification

- **Regression test for 1a**: synthesize a corpus of N files with M class
  candidates, assert `analyze_dead_code` wall time grows sub-linearly in
  `M × N` and stays under a fixed bound. The bench used for the table above is a
  good starting point.
- **Correctness**: `tests/test_deadcode.py`, `tests/test_overview_budget.py`,
  `tests/test_qml_full_support.py`, `tests/test_qt_relations.py` must pass
  unchanged — the QML-registration index has to classify exactly what the
  per-candidate scan classified. Add a fixture with `qmlRegisterType<Foo>` in a
  file other than `Foo`'s own, which is the case the cross-corpus scan exists for.
- **Concurrency**: a test that drives the server with 10 interleaved `tools/call`
  frames on one stdin pipe and asserts every id gets a response, that responses may
  arrive out of order, and that no frame is truncated or interleaved.
- **Deadline**: a test that a tool exceeding its budget returns
  `deadline_exceeded` rather than blocking.
- **Manual**: reproduce the original batch (`cortex_overview` plus 8 others in one
  parallel batch) against a large repo and confirm all calls return.

## Interim workaround

The reporter's workaround stands until phase 1 ships: do not batch
`cortex_overview` with other `cortex_*` calls. `CORTEX_AUTO_REFRESH=0` also
avoids re-triggering a full recompute on every fingerprint change, at the cost of
staleness.

## Notes on what this is not

- Not a deadlock or lock contention — there are no locks in the MCP path.
- Not an infinite loop — `detect_communities` is bounded at 30 iterations and
  measured at ~2 s on a 111k-node / 524k-edge synthetic graph.
- Not a shared-resource outage — confirmed per-process, as the reporter observed.

## Outcome

- Phase 1 shipped, plus a cost the plan missed: the per-label SQLite `LIKE` scan
  over graph edges in `find_references` was also O(candidates x edges) and
  dominated once 1a-1d were fixed (23.4s on an 800-file probe). It is replaced by
  an in-memory, trigram-prefiltered index that reproduces the `LIKE` scan exactly
  (2.1s on the same probe), alongside a repo corpus read once per run.
- Phase 2 shipped as one budget, `CORTEX_TOOL_TIMEOUT_SECONDS`. Dead-code analysis
  stops softly at it and returns a partial result (`partial` / `dead_code_partial`,
  not cached) instead of `deadline_exceeded`; other overruns still return the error.
- Phase 3's worker pool was not built. A stdin reader thread handles
  `notifications/cancelled`, `ping`, and `cortex_status` while tool execution stays
  serial, which removes the wedge without concurrent tool state.
- Phase 4 shipped: `cortex_status` plus a per-pid `mcp-activity/<pid>.json` file.
