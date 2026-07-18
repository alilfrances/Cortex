# Fix Plan — Remaining Feedback Gaps (branch: claude/cortex-feedback-fixes)

Scope: items from the head-to-head feedback not covered by commits `31ef948..68a6284`,
plus three correctness caveats found while reviewing that branch. Deferred item listed
at the end with rationale.

Conventions for all items: follow existing code style (no comments on simple code),
tests use in-memory/fake stores and tmp_path fixtures like the existing suites
(`tests/test_qt_connects.py`, `tests/test_impact_cochange.py` are the style reference).
Run `python3 -m pytest tests/ -q` after each item; all 222 existing tests must stay green.

---

## Item 1 — Read-vs-write mutation query (feedback P1 #5)

**Goal:** `cortex_references` answers "where is X mutated?" not just "where does X appear?".

**Changes:**
- `src/cortex/references.py`
  - Tag every grep hit with `access: "read" | "write"`. Write detection on the matched
    line (heuristic, per language family is unnecessary — one shared set is fine):
    - assignment: `X = ...` where next char after `X\s*` is `=` but not `==`, `<=`, `>=`, `!=`, `=>`
    - compound assignment: `X +=`, `-=`, `*=`, `/=`, `|=`, `&=`, `^=`, `<<=`, `>>=`
    - increment/decrement: `X++`, `X--`, `++X`, `--X`
    - mutating method call on the symbol: `X.append(`, `X.insert(`, `X.push_back(`,
      `X.clear(`, `X.remove(`, `X.pop(`, `X.emplace_back(`, `X.erase(`, `X.update(`,
      `X[...] =` (subscript store)
    - `del X` (Python)
    - everything else → `read`
  - Graph hits keep no access tag (they are definitions, not uses) — give them
    `access: "definition"`.
  - `find_references(...)` gains `mode: str = "all"` parameter; `mode="writes"` filters
    to write hits only (definitions always included).
- `src/cortex/mcp/tools.py`
  - `cortex_references` inputSchema gains `"mode": {"type": "string", "enum": ["all", "writes"], "default": "all"}`.
    Pass through to `find_references`. Update tool description: mention
    `mode:"writes"` answers "where is X mutated".
  - Each returned entry already carries `origin`; add `access`.

**Tests:** new `tests/test_references_writes.py`
- fixture file with one def, one read, one `x = ...`, one `x +=`, one `x.append(...)`,
  one `x == y` comparison (must be read).
- `mode="writes"` returns only the writes + definition; `mode="all"` tags all hits.
- Comparison operators never classified as writes.

---

## Item 2 — Emits/handles anchored to enclosing symbol (feedback P1 #5)

**Goal:** `emits` edges point from the emitting function, not the whole file, so
`cortex_path` through a signal shows the real emitter.

**Changes:**
- `src/cortex/structural/regex_backend.py` `_extract_qt_cpp_edges`:
  - Track the most recent function symbol node created in the line scan (the scan
    already creates symbol nodes with line numbers). When an emit line is inside a
    known function span (or after the most recent function definition at brace depth > 0),
    use that symbol's node id as the edge source instead of `file_node_id`.
    Fall back to `file_node_id` when no enclosing function is known.
- QML `handles` verification (`_extract_qml_handlers`): when the handler name
  `onFooChanged`/`onFoo` has a matching signal symbol `foo`/`fooChanged` in the graph
  for the same file's instantiated components, keep confidence as-is; when it matches
  nothing, keep the edge but set `confidence="LOW"` and `metadata["unverified"] = True`.
  (Do not drop edges — just label them; provenance is the contract.)

**Tests:** extend `tests/test_qt_connects.py` or new `tests/test_qt_emits.py`
- emit inside a method → edge source is `symbol:<file>:<method>`.
- emit at file scope (unlikely but possible in macros) → source stays `file:` node.
- QML handler with no matching signal gets `unverified` metadata.

---

## Item 3 — Token stats in cortex_query payload (feedback P2 #8)

**Goal:** caller sees what the bundle cost and how much of it matched the task.

**Changes:**
- `src/cortex/mcp/tools.py` `_call_query`: add a `token_stats` object to the payload
  (both concise and detailed formats):
  ```json
  {
    "budget": 4000,
    "returned_tokens": <bundle total_tokens>,
    "matched_tokens": <sum of token_count over items whose why contains a keyword entry>,
    "matched_ratio": <matched_tokens / returned_tokens, 2 decimals, 0.0 when empty>
  }
  ```
  No new computation elsewhere — derive from the bundle dict already in hand.

**Tests:** extend existing MCP query test (`tests/test_p2_mcp.py` has the pattern)
- stats present, ratio in [0,1], matched_tokens ≤ returned_tokens.

---

## Item 4 — Build-system wiring: CMake targets + Qt resources (feedback P2 #9, scoped)

**Goal:** graph answers "which build target compiles this file" and "which .qrc
registers this resource" — the cross-language edges grep+git miss together.

**Changes:**
- `src/cortex/ingest.py`: add `".cmake"`, `".qrc"`, `".ui"`, `".pro"` to `_TEXT_SUFFIXES`
  (CMakeLists.txt already ingested via `.txt`).
- `src/cortex/structural/regex_backend.py` (new functions, dispatched from
  `extract_regex_edges`):
  - **CMake** (files named `CMakeLists.txt` or suffix `.cmake` — `extract_regex_edges`
    currently dispatches on suffix only, so add a filename check):
    - `add_executable(<target> [flags] <sources...>)`, `add_library(<target> ...)`,
      `qt_add_executable`, `qt_add_library` → node `target:<name>` (kind `"target"`,
      granularity `"symbol"`) + edge `target --builds--> file:<source>` for every listed
      source that resolves into `known_paths` (resolve relative to the CMake file's dir).
    - `target_sources(<target> ... <sources...>)` → same `builds` edges.
    - `qt_add_resources(<target> <name> PREFIX ... FILES <files...>)` and
      `qt_add_qml_module(... QML_FILES <files...>)` → `target --registers--> file:<f>`.
    - Multi-line calls: scan with one regex over whole content (same technique as the
      connect fix). Skip `${...}` variable sources — unresolvable, don't emit `name:` junk.
    - Edge layer `STRUCTURAL`, confidence `EXTRACTED`, edge_id prefix `regex:` so
      `_edge_origin` reports `regex-parser` unchanged.
  - **.qrc** (XML): `<file>path</file>` entries, resolved relative to the qrc file dir →
    `file:<qrc> --registers--> file:<resolved>` for entries in `known_paths`. A simple
    regex `<file[^>]*>([^<]+)</file>` is fine — qrc is machine-generated XML.
- `src/cortex/references.py` `_bucket`: `.pro` and `.ui` → `"config"` bucket (`.qrc`,
  `.cmake`, `cmakelists.txt` already there).

**Tests:** new `tests/test_build_wiring.py`
- CMakeLists.txt with add_executable + target_sources across lines → `builds` edges to
  the resolved files, none for `${VAR}` entries or files outside known_paths.
- `.qrc` with two entries, one resolvable → exactly one `registers` edge.
- Edges visible through `cortex_relations` for a source file (origin `regex-parser`).

---

## Item 5 — Cross-file `Cls::member` connect resolution (review caveat 1)

**Goal:** `connect(a, &A::sig, b, &B::slot)` resolves when the class is declared in
`Cls.h` and the member is defined in `Cls.cpp` — the standard Qt layout.

**Changes:**
- `src/cortex/structural/regex_backend.py`: the C++ func def pattern
  (`_CPP_DEF_PATTERNS`, the `(?:[A-Za-z_]\w*::)*` group) currently drops the qualifier.
  Capture it (`(?P<qualifier>(?:[A-Za-z_]\w*::)*)`) and when non-empty store the owning
  class name in the symbol node's metadata: `metadata["qualifier"] = "Cls"` (outermost
  segment stripped of trailing `::`; for nested `A::B::` use the last segment before the
  member — that is the class).
- `src/cortex/graph.py` `_resolve_connect_endpoints`:
  - build the `class_files` map from BOTH class-kind nodes AND symbol nodes whose
    `metadata["qualifier"]` names the class (i.e. `Cls.cpp` defining `Cls::slot` makes
    that file a candidate file for `Cls`).
  - `symbol_by_file_label` already maps `(file, member)` — a `Cls::member` def node's
    label must stay `member` (do not change labels; qualifier lives in metadata only)
    so the existing lookup works unchanged.
  - keep the uniqueness rule: resolve only when exactly one candidate node id.

**Tests:** extend `tests/test_qt_connects.py`
- `Cls.h` declares class + signal; `Cls.cpp` defines `Cls::onThing(...) {}` and holds the
  connect call → both endpoints resolve to real `symbol:` ids.
- Two unrelated classes each defining same-named member in different files → endpoint
  stays `name:Cls::member` only when genuinely ambiguous (same class name in two files).

---

## Item 6 — Incremental refresh: re-resolve endpoints + recompute degree (review caveat 2)

**Goal:** incremental refresh produces the same graph a full refresh would.

**Problem:** `_resolve_connect_endpoints` and the degree computation run inside
`build_graph`, which on incremental runs only sees changed sources. Retained edges never
re-resolve against new nodes; retained nodes keep stale `degree`.

**Changes:**
- `src/cortex/graph.py`: extract two public helpers from `build_graph`'s tail:
  `resolve_connect_endpoints(nodes, edges) -> list[GraphEdge]` (already exists as
  `_resolve_connect_endpoints` — make it public) and
  `annotate_degree(nodes, edges) -> None`.
- `src/cortex/ingest.py` incremental branch: after `merged_nodes`/`merged_edges` are
  assembled, run `resolve_connect_endpoints(merged_nodes, merged_edges)` and
  `annotate_degree(merged_nodes, merged_edges)` over the MERGED graph before saving.
  (Running resolution twice is idempotent: already-resolved `symbol:` endpoints pass
  through untouched.)
- `build_graph` keeps calling both for the full path — no behavior change there.

**Tests:** extend `tests/test_refresh_mode.py`
- full ingest, then touch one file, incremental refresh → node degrees equal a fresh
  full ingest's degrees; a `name:Cls::member` endpoint that becomes resolvable due to
  the changed file resolves after incremental refresh.

---

## Item 7 — `module:` prefix in unresolved endpoints (review caveat 3)

**Changes:** `src/cortex/mcp/tools.py` `_unresolved_endpoint`: add
```python
if node_id.startswith("module:"):
    return node_id.removeprefix("module:") or node_id
```

**Tests:** one assertion in the existing relations test: `module:numpy` endpoint renders
as `numpy`.

---

## Deferred (do NOT implement)

- **QML model-role reads** ("which UI binding reads this model role", feedback P2 #9
  deep case): requires correlating C++ `roleNames()` return values with QML delegate
  property references. Regex-level extraction produces mostly false positives; needs
  the tree-sitter backend to grow QML support first. Revisit as its own branch.
- **True "useful tokens" measurement** (P2 #8 full version): usefulness is only
  observable at the caller after task completion. Item 3's matched-ratio proxy is the
  honest server-side stat.

## Suggested commit slicing

1. `feat: read/write access tagging and writes mode on cortex_references` (Item 1)
2. `fix: anchor emits edges to enclosing symbol, mark unverified QML handlers` (Item 2)
3. `feat: token_stats on cortex_query` (Item 3)
4. `feat: CMake target and Qt resource wiring edges` (Item 4)
5. `fix: resolve cross-file Cls::member connect endpoints` (Item 5)
6. `fix: re-resolve endpoints and recompute degree on incremental refresh` (Item 6 + 7)
