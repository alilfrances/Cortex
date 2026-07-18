# Follow-up Spec — Tree-sitter C++ qualifier parity for connect resolution

Branch base: `claude/cortex-rtk-repowise-analysis-zh2g8p` (post-merge `e6e31ef`).
Single-item, delegate-ready. Style + verification conventions identical to `plan.md`
(in-memory/tmp_path fixtures; `tests/test_qt_connects.py` is the reference suite).
Run `python3 -m pytest tests/ -q` after the change — all 396 tests must stay green.

---

## Problem

`plan.md` Item 5 taught the **regex** backend to capture the class qualifier of an
out-of-line C++ definition (`void Cls::slot() {}`) into `metadata["qualifier"]`, so
`graph.py::resolve_connect_endpoints` can resolve a `name:Cls::slot` connect endpoint to
the real `symbol:Cls.cpp:slot` id (the file that defines `Cls::slot` becomes a candidate
file for class `Cls`).

The **tree-sitter** backend — the *default* active backend whenever the C++ grammar is
installed — never got the same treatment. It builds every symbol node with
`metadata={"lineno": line}` only (`src/cortex/structural/treesitter_backend.py`, in
`extract_treesitter_edges`, the `nodes.append(GraphNode(... metadata={"lineno": line}))`
block, ~line 348). So on the live path:

- `resolve_connect_endpoints` (`src/cortex/graph.py`) builds `class_files` from
  class-kind nodes **and** `node.metadata.get('qualifier')`. Tree-sitter-parsed
  out-of-line member defs contribute neither → the defining file is never registered as
  a candidate file for its class.
- Result: `connect(a, &Cls::sig, b, &Cls::slot)` where `Cls` is declared in `Cls.h` and
  the member is defined in `Cls.cpp` stays `name:Cls::slot` (unresolved) under
  tree-sitter, even though the regex fallback resolves it.

The existing regression test
`tests/test_qt_connects.py::test_build_graph_resolves_qualified_members_across_header_and_cpp_files`
**monkeypatches tree-sitter off** (`fail_tree_sitter`) — which is exactly why the gap
shipped unnoticed. The tree-sitter path has no coverage for qualifier-based resolution.

Also suspected (confirm during repro, step 0): for a qualified declarator
`void Cls::slot()`, `_name_for_node` (`treesitter_backend.py` ~line 103) does a
first-identifier DFS via `_identifier_text` and may return the **scope** (`Cls`) rather
than the member (`slot`). If so, the symbol node's `node_id`/label is wrong on the
tree-sitter path independent of the qualifier issue, and must be fixed too.

## Step 0 — Reproduce first (establish the baseline)

Run before touching code, to confirm the exact current behavior (label + qualifier +
resolution). Tree-sitter cpp must be installed (`python3 -c "import tree_sitter_cpp"`).

```python
PYTHONPATH=src python3 - <<'PY'
from cortex.structural import treesitter_backend as tb
from cortex.graph import build_graph
from cortex.models import SourceRecord

impl = (
    "void Cls::slot() {}\n"
    "class Cls {\n"
    "signals:\n"
    "    void thing();\n"
    "};\n"
    "void wire(Cls *s, Cls *r) { connect(s, &Cls::thing, r, &Cls::slot); }\n"
)
nodes, _ = tb.extract_treesitter_edges("Cls.cpp", impl, set())
for n in nodes:
    if n.granularity == "symbol":
        print("SYM", repr(n.label), "q=", n.metadata.get("qualifier"), n.node_id)

def src(p, c):
    return SourceRecord(path=p, content=c, kind="code", size_bytes=len(c),
                        modified_at=0.0, content_hash="")

_gn, ge = build_graph([src("Cls.cpp", impl)], [])
print("CONNECTS:", [(e.source, e.target) for e in ge if e.relation == "connects"])
PY
```

Expected **before** fix: the `Cls::slot` symbol has `q=None`; the connect target renders
`name:Cls::slot` (unresolved). Note whether its label is `slot` (correct) or `Cls`
(the label bug above). Record both.

## Changes

### `src/cortex/structural/treesitter_backend.py`

1. Add a helper that extracts the class qualifier and the bare member name from a
   definition node's declarator, using the grammar's structured scope nodes rather than
   string parsing:
   - For C++ the declarator chain is `function_definition → declarator (function_declarator)
     → declarator (qualified_identifier)`; the `qualified_identifier` has a `scope` field
     (`namespace_identifier` / `type_identifier`, possibly nested `A::B::`) and a `name`.
     Walk to the innermost `qualified_identifier`/`scoped_identifier`; the **last scope
     segment** is the class, the trailing identifier is the member.
   - Return `(qualifier: str | None, member_name: str)`. `qualifier` is `None` when the
     declarator is unqualified (ordinary free function / in-class method).
   - Match the regex backend's normalization: for `A::B::member`, qualifier is `B` (last
     segment before the member), i.e. `qualifier.rstrip(":").split("::")[-1]` equivalent.

2. In the symbol-emitting block of `extract_treesitter_edges`:
   - Use the helper's `member_name` for `name`/`node_id`/label when the node is a
     qualified C++ definition, so the label is the member (`slot`), not the scope. Keep
     the current `name.split(".")[-1].split("::")[-1]` label derivation as a safety net.
   - When `qualifier` is non-empty, set `metadata["qualifier"] = qualifier` alongside
     `"lineno"`. Leave all other node types untouched (only C++ suffixes / qualified
     declarators get a qualifier — do not invent qualifiers for Python/JS/etc.).

Keep the change minimal and local to the C++ symbol path. Do not alter
`resolve_connect_endpoints` — it already consumes `metadata["qualifier"]`; this change
just makes the tree-sitter backend feed it the same data the regex backend does.

## Tests — `tests/test_qt_connects.py`

Add a tree-sitter-**on** twin of the existing monkeypatched-off test (do NOT patch
tree-sitter out). Guard it so it skips cleanly when the grammar is absent, matching how
other tree-sitter-dependent tests in the suite gate themselves:

```python
import pytest
try:
    import tree_sitter_cpp  # noqa: F401
    _HAS_TS_CPP = True
except Exception:
    _HAS_TS_CPP = False
```

- `@pytest.mark.skipif(not _HAS_TS_CPP, ...)` — `Cls.h` declares the class + signal;
  `Cls.cpp` defines `void Cls::slot() {}` and holds the connect; assert **both** endpoints
  resolve to real `symbol:` ids on the default (tree-sitter) path — the same assertions
  as `test_build_graph_resolves_qualified_members_across_header_and_cpp_files`, minus the
  `fail_tree_sitter` monkeypatch.
- Assert the `Cls::slot` symbol node's label is `slot` and its
  `metadata["qualifier"] == "Cls"` (locks in the label + qualifier fix).
- Keep the existing monkeypatched-off test as-is (regex parity must not regress).

## Acceptance

- Step-0 repro, re-run after the fix, prints `q= Cls`, label `slot`, and
  `CONNECTS: [... 'symbol:Cls.cpp:slot']` (target resolved).
- New test passes with the grammar installed, skips without it.
- Full suite green: `python3 -m pytest tests/ -q` → 396 passed (+1 new), 4 skipped
  (+1 when grammar absent).

## Out of scope

- QML / Swift / Rust qualifier extraction — C++ member defs are the only case
  `resolve_connect_endpoints` consumes today. Do not broaden.
- Any change to `graph.py`, `ingest.py`, or the regex backend — this is tree-sitter
  parity only.

## Suggested commit

`fix: attach C++ class qualifier to tree-sitter symbol nodes for connect resolution`
