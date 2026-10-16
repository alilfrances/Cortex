"""Task 7: JavaScript / TypeScript / TSX call extraction (tree-sitter) and name-only regex fallback.

Fixture: ``tests/fixtures/calls_js`` (call-specific; ``multilang_repo/{app.js,client.ts}`` stay
frozen).  Import resolution here is the extractor's own (``resolve_js_spec``): the generic
``imports`` edges of JS files target ``module:<spec>`` and are frozen, so the call edge carries
``import_spec``/``import_file`` (or ``scope_file`` for namespace/class bindings).
"""

from __future__ import annotations

import pytest

from cortex.callresolve import resolve_call_endpoints
from cortex.models import GraphEdge, GraphNode
from cortex.structural.call_sites import resolve_js_spec
from tests.call_helpers import (
    CONTRACT_KEYS, assert_ambiguous, assert_regex_name_only, assert_resolved, assert_unresolved,
    by_site, calls, fixture_graph,
)
from tests.golden_utils import TREE_SITTER_ONLY

START = "symbol:app.ts:start"
H, U = "symbol:lib/helpers.js:helper", "symbol:lib/utils.ts:helper"

# (file, line, callee, target, tier)
RESOLVED = [
    ("app.ts", 15, "helper", H, "import_scoped"),                      # import { helper } -> helpers.js (not utils.ts)
    ("app.ts", 16, "helper", U, "scoped"),                              # import * as utils; utils.helper()
    ("app.ts", 17, "stop", "symbol:app.ts:stop", "this"),               # this.stop()
    ("app.ts", 18, "save", "symbol:lib/repo.ts:save", "declared_type"),  # private repo: Repo
    ("app.ts", 19, "run", "symbol:lib/service.ts:run", "declared_type"),  # constructor(private svc: Service)
    ("app.ts", 20, "Service", "symbol:lib/service.ts:Service", "import_scoped"),  # new + default import
    ("app.ts", 21, "run", "symbol:lib/service.ts:run", "declared_type"),  # const s = new Service()
    ("app.ts", 22, "run", "symbol:lib/service.ts:run", "declared_type"),  # new Service().run()
    ("app.ts", 22, "Service", "symbol:lib/service.ts:Service", "import_scoped"),
    ("app.ts", 23, "render", "symbol:lib/panel_a.ts:render", "declared_type"),  # p: Panel, imported from panel_a
    ("app.ts", 24, "go", "symbol:lib/legacy.js:go", "scoped"),          # const legacy = require(...)
    ("app.ts", 27, "render", "symbol:lib/panel_a.ts:render", "declared_type"),  # p?.render()
    ("app.ts", 36, "render", "symbol:lib/panel_a.ts:render", "declared_type"),
    ("app.ts", 37, "save", "symbol:lib/repo.ts:save", "declared_type"),
    ("lib/worker.js", 3, "step", "symbol:lib/worker.js:step", "this"),
    ("main.js", 4, "Worker", "symbol:lib/worker.js:Worker", "import_scoped"),
    ("main.js", 5, "run", "symbol:lib/worker.js:run", "declared_type"),  # JS: `new Type()` inference only
    ("view.tsx", 4, "helper", H, "import_scoped"),                      # import { helper as h }; h() -> callee helper
]


@pytest.fixture(scope="module")
def ts_edges():
    return fixture_graph("calls_js")[1]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("file,line,callee,target,tier", RESOLVED, ids=lambda v: str(v))
def test_site_resolves_to_expected_symbol_and_tier(ts_edges, file, line, callee, target, tier) -> None:
    assert_resolved(by_site(ts_edges), file, line, callee, target, tier)


@TREE_SITTER_ONLY
def test_contract_and_metadata(ts_edges) -> None:
    sites = by_site(ts_edges)
    for edge in calls(ts_edges):
        assert CONTRACT_KEYS <= set(edge.metadata)
    assert len({e.edge_id for e in calls(ts_edges)}) == len(calls(ts_edges))
    assert sites[("app.ts", 15, "helper")].source == START
    md = sites[("app.ts", 17, "stop")].metadata
    assert (md["receiver_kind"], md["enclosing_class"]) == ("this", "Controller")
    md = sites[("app.ts", 16, "helper")].metadata
    assert (md["receiver_kind"], md["scope"], md["scope_namespace"]) == ("scoped", "utils", True)
    assert sites[("app.ts", 20, "Service")].metadata["receiver_kind"] == "new"
    assert sites[("app.ts", 22, "run")].metadata["receiver"] == "new Service()"
    assert sites[("view.tsx", 4, "helper")].metadata["receiver_kind"] == "none"
    assert sites[("app.ts", 29, "baz")].metadata["receiver_kind"] == "chain"
    assert sites[("app.ts", 29, "baz")].metadata["receiver"] == "foo.bar"
    assert sites[("app.ts", 28, "get")].metadata["receiver"] == "this.cache"


@TREE_SITTER_ONLY
def test_external_untyped_chain_and_member_calls_stay_name_based_with_unchanged_confidence(ts_edges) -> None:
    sites = by_site(ts_edges)
    for key in (("app.ts", 26, "useState"), ("app.ts", 28, "get"), ("app.ts", 29, "baz"), ("main.js", 6, "missing")):
        edge = assert_unresolved(sites, *key)
        assert edge.confidence == "EXTRACTED"


@TREE_SITTER_ONLY
def test_ambiguity_is_kept_with_sorted_candidates(ts_edges) -> None:
    sites = by_site(ts_edges)
    assert_ambiguous(sites, "app.ts", 25, "render", ["symbol:lib/other.js:render", "symbol:lib/shared.js:render"])
    # `Panel` is defined in two files and `loose.ts` imports neither
    assert_ambiguous(sites, "loose.ts", 2, "render", ["symbol:lib/panel_a.ts:render", "symbol:lib/panel_b.ts:render"])


def test_resolve_js_spec_probes_extensions_index_and_js_to_ts() -> None:
    known = {"a/x.ts", "a/y/index.tsx", "a/z.js", "b/c.ts"}
    assert resolve_js_spec("a/main.ts", "./x", known) == "a/x.ts"
    assert resolve_js_spec("a/main.ts", "./y", known) == "a/y/index.tsx"
    assert resolve_js_spec("a/main.ts", "./z.js", known) == "a/z.js"
    assert resolve_js_spec("a/main.ts", "../b/c.js", known) == "b/c.ts"  # TS `.js` specifier
    assert resolve_js_spec("a/main.ts", "react", known) is None
    assert resolve_js_spec("a/main.ts", "./missing", known) is None


def _node(path, name, kind="func", span=(1, 3)):
    return GraphNode(f"symbol:{path}:{name}", kind, name, path, "symbol", "", span[0], span[1], {})


def _call(callee, **md):
    base = {
        "lineno": 1, "source_file": "main.ts", "callee": callee, "receiver": None, "receiver_kind": "none",
        "scope": None, "receiver_type": None, "enclosing_class": None,
    }
    base.update(md)
    return GraphEdge(f"e:{callee}", "symbol:main.ts:main", f"name:{callee}", "calls", "STRUCTURAL", "EXTRACTED", 1.0, base)


def test_explicit_import_beats_same_file_and_unresolved_import_never_guesses_repo_wide() -> None:
    nodes = [
        _node("main.ts", "main"), _node("main.ts", "f"), _node("lib.ts", "f"), _node("other.ts", "g"),
    ]
    imported = _call("f", import_spec="./lib", import_file="lib.ts", implicit_receiver=False)
    external = _call("g", import_spec="pkg", import_file=None, implicit_receiver=False)
    plain = _call("g", implicit_receiver=False)
    resolve_call_endpoints(nodes, [imported, external, plain])
    assert imported.target == "symbol:lib.ts:f" and imported.metadata["resolution"] == "import_scoped"
    assert external.target == "name:g"            # `import { g } from "pkg"`: not other.ts's g
    assert plain.target == "symbol:other.ts:g"     # genuinely free + repo-unique


def test_js_calls_never_resolve_as_implicit_this() -> None:
    nodes = [
        _node("a.ts", "A", "class", (1, 9)), _node("a.ts", "go", "func", (2, 3)), _node("a.ts", "m", "func", (4, 8)),
    ]
    bare = _call("go", source_file="a.ts", enclosing_class="A", implicit_receiver=False)
    bare.source = "symbol:a.ts:m"
    cpp_like = _call("go", source_file="a.ts", enclosing_class="A")
    cpp_like.source = "symbol:a.ts:m"
    resolve_call_endpoints(nodes, [bare, cpp_like])
    assert bare.target == "name:go"  # JS `go()` is a free call, not `this.go()`
    assert cpp_like.target == "symbol:a.ts:go" and cpp_like.metadata["resolution"] == "this"


# --------------------------------------------------------------------------- #
# Regex fallback
# --------------------------------------------------------------------------- #

def test_regex_fallback_is_name_only_low(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    _, edges = fixture_graph("calls_js")
    expected = {(f, line, callee) for f, line, callee, _, _ in RESOLVED if (f, line) != ("app.ts", 22) and f != "view.tsx"} | {
        ("view.tsx", 4, "h"),  # the fallback has no import model: it sees the local alias
        ("app.ts", 25, "render"), ("app.ts", 26, "useState"), ("loose.ts", 2, "render"),
    }
    assert_regex_name_only(edges, expected)
    sites = by_site(edges)
    # `helper` is imported from a unique file; the fallback must not resolve it
    assert sites[("app.ts", 15, "helper")].target == "name:helper"
    assert sites[("main.js", 5, "run")].target == "name:run"


def test_regex_class_headers_and_interface_signatures_are_not_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural.regex_backend import extract_regex_edges

    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    src = (
        "export interface Shape {\n  area(): number;\n}\n"
        "export class Box {\n  size: number = compute();\n  constructor(private n: number) {}\n"
        "  grow(): void {\n    this.n = bump(this.n);\n  }\n}\n"
    )
    _, edges = extract_regex_edges("box.ts", src, {"box.ts"})
    callees = sorted(e.metadata["callee"] for e in edges if e.relation == "calls")
    assert callees == ["bump", "compute"]


@TREE_SITTER_ONLY
def test_cortex_references_shows_graph_tier_read_hits_from_other_files(tmp_path, monkeypatch) -> None:
    import json

    from cortex.ingest import ingest_repository
    from cortex.mcp.tools import call_tool
    from tests.baseline_helpers import FIXTURES, copy_fixture_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = copy_fixture_repo(FIXTURES / "calls_js", tmp_path / "repo")
    ingest_repository(repo, commit_limit=5)
    result = call_tool("cortex_references", {"repo_path": str(repo), "symbol": "save", "format": "detailed"})
    payload = json.loads(result["content"][0]["text"])
    assert not result["isError"], payload
    hits = [h for bucket in payload["items"].values() for h in bucket]
    graph_reads = sorted(h["text"] for h in hits if h["origin"] == "graph" and h["access"] == "read")
    # `save()` is called at app.ts:18 (this.repo.save()) and app.ts:37 (r.save()): the graph tier
    # reports each resolved call as a `read` at the caller's file and line, and the method's own
    # location as a `definition` (never a `read` of the definition).
    assert graph_reads == ["app.ts:18", "app.ts:37"], hits
    assert [h["text"] for h in hits if h["origin"] == "graph" and h["access"] == "definition"] == ["lib/repo.ts:2"]
    assert not any(h["origin"] == "grep" and h["text"] in graph_reads for h in hits)  # no duplicate grep hit
