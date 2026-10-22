"""Task 6: C/C++ call extraction (tree-sitter) with receiver typing; regex fallback name-only.

Fixtures: ``tests/fixtures/calls_cpp`` (plain C++) -- call-specific, so the frozen
``multilang_repo`` definitions stay untouched.  Qt-specific assertions live in
``tests/test_qt_relations.py``.
"""

from __future__ import annotations

import pytest

from cortex.callresolve import is_contract_call, resolve_call_endpoints
from cortex.models import GraphEdge, GraphNode
from tests.call_helpers import (
    CONTRACT_KEYS,
    assert_ambiguous,
    assert_regex_name_only,
    assert_resolved,
    assert_unresolved,
    by_site,
    calls,
    fixture_graph,
)
from tests.golden_utils import TREE_SITTER_ONLY

RUN = "symbol:app.cpp:run"

# (file, line, callee, resolved target, tier)
RESOLVED = [
    ("app.cpp", 32, "free_helper", "symbol:app.cpp:free_helper", "same_file"),
    ("app.cpp", 33, "warm_up", "symbol:app.cpp:warm_up", "this"),          # this->warm_up()
    ("app.cpp", 34, "warm_up", "symbol:app.cpp:warm_up", "this"),          # implicit this
    ("app.cpp", 35, "make", "symbol:app.cpp:make", "scoped"),              # Engine::make()
    ("app.cpp", 36, "connect_count", "symbol:app.cpp:connect_count", "scoped"),  # Net::connect_count()
    ("app.cpp", 38, "work", "symbol:worker.hpp:work", "declared_type"),    # Worker local; local.work()
    ("app.cpp", 39, "work", "symbol:worker.hpp:work", "declared_type"),    # Worker *w; w->work()
    ("app.cpp", 40, "warm", "symbol:app.cpp:warm", "declared_type"),       # Engine &e; e.warm()
    ("app.cpp", 41, "warm", "symbol:app.cpp:warm", "declared_type"),       # std::shared_ptr<Engine> member
    ("app.cpp", 42, "work", "symbol:worker.hpp:work", "declared_type"),    # Worker *worker_ member
    ("app.cpp", 44, "warm", "symbol:app.cpp:warm", "declared_type"),       # auto x = Engine();
    ("app.cpp", 45, "Worker", "symbol:worker.hpp:Worker", "import_scoped"),  # new Worker()
    ("app.cpp", 46, "work", "symbol:worker.hpp:work", "declared_type"),    # auto *y = new Worker();
    ("app.cpp", 47, "ping", "symbol:base.hpp:ping", "inherited"),          # implicit-this -> base
    ("app.cpp", 48, "tick", "symbol:base.hpp:tick", "inherited"),          # this->tick() -> base
    ("app.cpp", 49, "clamp", "symbol:util.hpp:clamp", "import_scoped"),    # inline in included header
    ("app.cpp", 50, "compute_total", "symbol:util.cpp:compute_total", "import_scoped"),  # header decl, .cpp def
    ("util.cpp", 4, "clamp", "symbol:util.hpp:clamp", "import_scoped"),
    ("worker.hpp", 7, "ping", "symbol:base.hpp:ping", "inherited"),
    # header-declared members typed through the class node's `member_types`
    ("gateway.cpp", 4, "warm", "symbol:app.cpp:warm", "declared_type"),
    ("gateway.cpp", 5, "work", "symbol:worker.hpp:work", "declared_type"),
    ("gateway.cpp", 6, "warm", "symbol:app.cpp:warm", "declared_type"),
]


@pytest.fixture(scope="module")
def ts_edges():
    import os

    assert os.environ.get("CORTEX_FORCE_REGEX") != "1"
    return fixture_graph("calls_cpp")[1]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("file,line,callee,target,tier", RESOLVED, ids=lambda v: str(v))
def test_site_resolves_to_expected_symbol_and_tier(ts_edges, file, line, callee, target, tier) -> None:
    assert_resolved(by_site(ts_edges), file, line, callee, target, tier)


@TREE_SITTER_ONLY
def test_enclosing_symbols_and_contract(ts_edges) -> None:
    for edge in calls(ts_edges):
        assert CONTRACT_KEYS <= set(edge.metadata)
        assert edge.layer == "STRUCTURAL"
    assert len({e.edge_id for e in calls(ts_edges)}) == len(calls(ts_edges))
    sites = by_site(ts_edges)
    assert sites[("app.cpp", 32, "free_helper")].source == RUN
    assert sites[("gateway.cpp", 4, "warm")].source == "symbol:gateway.cpp:send"
    # receiver metadata
    md = sites[("app.cpp", 33, "warm_up")].metadata
    assert (md["receiver"], md["receiver_kind"], md["enclosing_class"]) == ("this", "this", "Controller")
    md = sites[("app.cpp", 35, "make")].metadata
    assert (md["scope"], md["receiver_kind"]) == ("Engine", "scoped")
    md = sites[("app.cpp", 41, "warm")].metadata
    assert (md["receiver"], md["receiver_kind"], md["receiver_type"]) == ("engine_", "var", "Engine")
    md = sites[("app.cpp", 45, "Worker")].metadata
    assert md["receiver_kind"] == "new"
    # out-of-line member: type unknown at the call site, joined later via the class node
    md = sites[("gateway.cpp", 4, "warm")].metadata
    assert md["receiver_type"] is None and md["receiver_member"] == "engine_"
    assert md["enclosing_class"] == "Gateway"


@TREE_SITTER_ONLY
def test_header_member_types_recorded_on_the_class_node_only_when_present() -> None:
    nodes, _ = fixture_graph("calls_cpp")
    by_id = {n.node_id: n for n in nodes}
    assert by_id["symbol:gateway.hpp:Gateway"].metadata["member_types"] == {"engine_": "Engine", "worker_": "Worker"}
    assert "member_types" not in by_id["symbol:base.hpp:Base"].metadata


@TREE_SITTER_ONLY
def test_untyped_and_external_calls_stay_name_based(ts_edges) -> None:
    sites = by_site(ts_edges)
    assert_unresolved(sites, "app.cpp", 54, "printf")
    assert_unresolved(sites, "app.cpp", 53, "push_back")  # std::vector<int>: not a repo class


@TREE_SITTER_ONLY
def test_ambiguous_free_call_keeps_name_with_sorted_candidates(ts_edges) -> None:
    edge = assert_ambiguous(
        by_site(ts_edges), "app.cpp", 51, "render", ["symbol:draw_a.cpp:render", "symbol:draw_b.cpp:render"]
    )
    assert edge.metadata["receiver_kind"] == "none"


@TREE_SITTER_ONLY
def test_ambiguity_at_an_earlier_tier_stops_before_a_later_unique_match(ts_edges) -> None:
    """`Duo` inherits `name()` from two bases; a repo-unique free `name()` exists in misc.cpp but
    must not win (the inherited tier decides, and it is ambiguous)."""
    sites = by_site(ts_edges)
    assert_ambiguous(sites, "diamond.cpp", 7, "name", ["symbol:left.hpp:name", "symbol:right.hpp:name"])


@TREE_SITTER_ONLY
def test_qt_macros_emit_and_connect_are_not_call_edges() -> None:
    import re
    from pathlib import Path

    from tests.baseline_helpers import FIXTURES, fixture_files, read_sources
    from cortex.graph import build_graph

    root = FIXTURES / "qt_calls_app"
    nodes, edges = build_graph(read_sources(root, fixture_files(root)), [])
    callees = {e.metadata["callee"] for e in calls(edges)}
    assert not callees & {"connect", "emit", "SIGNAL", "SLOT"}
    assert not [c for c in callees if re.fullmatch(r"Q_[A-Z_]+", c)]
    assert [e for e in edges if e.relation == "emits"], "emit still produces an `emits` edge"
    assert [e for e in edges if e.relation == "connects"], "connect still produces a `connects` edge"


# --------------------------------------------------------------------------- #
# Regex fallback (always runs): name-only, LOW, never resolved
# --------------------------------------------------------------------------- #

EXPECTED_SITES = {(f, line, callee) for f, line, callee, _, _ in RESOLVED} | {
    ("app.cpp", 51, "render"), ("diamond.cpp", 7, "name"), ("app.cpp", 54, "printf"),
}


def test_regex_fallback_emits_name_only_low_calls_for_the_same_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    _, edges = fixture_graph("calls_cpp")
    assert_regex_name_only(edges, EXPECTED_SITES)
    # `ping()` has exactly one definition repo-wide and `clamp()` an include-scoped one: the
    # resolver would have resolved both had they been treated like parser edges.
    sites = by_site(edges)
    assert sites[("app.cpp", 47, "ping")].target == "name:ping"
    assert sites[("app.cpp", 49, "clamp")].target == "name:clamp"


def test_regex_calls_skip_control_flow_macros_and_emit(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.baseline_helpers import FIXTURES, fixture_files, read_sources
    from cortex.graph import build_graph

    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    root = FIXTURES / "qt_calls_app"
    _, edges = build_graph(read_sources(root, fixture_files(root)), [])
    callees = {e.metadata["callee"] for e in calls(edges)}
    assert callees and not callees & {"connect", "emit", "if", "for", "while", "switch", "SIGNAL", "SLOT"}
    assert "found" not in callees  # `emit found(1)` is an `emits` edge, not a call


def _call(name_only: bool) -> GraphEdge:
    md = {
        "lineno": 3, "source_file": "a.cpp", "callee": "helper", "receiver": None, "receiver_kind": "none",
        "scope": None, "receiver_type": None, "enclosing_class": None,
    }
    if name_only:
        md["name_only"] = True
    return GraphEdge("e", "symbol:a.cpp:main", "name:helper", "calls", "STRUCTURAL", "LOW", 1.0, md)


def test_resolver_never_promotes_name_only_calls() -> None:
    nodes = [
        GraphNode("symbol:a.cpp:main", "func", "main", "a.cpp", "symbol", "", 1, 5, {}),
        GraphNode("symbol:b.cpp:helper", "func", "helper", "b.cpp", "symbol", "", 1, 2, {}),
    ]
    parser_edge, regex_edge = _call(False), _call(True)
    assert is_contract_call(parser_edge) and not is_contract_call(regex_edge)
    resolve_call_endpoints(nodes, [parser_edge, regex_edge])
    assert parser_edge.target == "symbol:b.cpp:helper"
    assert regex_edge.target == "name:helper" and regex_edge.confidence == "LOW"


def test_member_type_join_is_resolver_side_and_unique_only() -> None:
    """`receiver_member` + the enclosing class's `member_types` -> declared_type; conflicting
    declarations along the inheritance chain leave the call untyped."""
    def cls(path, name, span, member_types=None):
        md = {"member_types": member_types} if member_types else {}
        return GraphNode(f"symbol:{path}:{name}", "class", name, path, "symbol", f"class {name} {{", span[0], span[1], md)

    def fn(path, name, span, qualifier=None):
        md = {"qualifier": qualifier} if qualifier else {}
        return GraphNode(f"symbol:{path}:{name}", "func", name, path, "symbol", f"void {name}() {{", span[0], span[1], md)

    nodes = [
        cls("a.hpp", "Svc", (1, 9), {"repo_": "Repo"}),
        cls("repo.hpp", "Repo", (1, 5)),
        fn("repo.hpp", "save", (2, 3)),
        fn("a.cpp", "run", (3, 8), qualifier="Svc"),
    ]
    md = {
        "lineno": 4, "source_file": "a.cpp", "callee": "save", "receiver": "repo_", "receiver_kind": "var",
        "scope": None, "receiver_type": None, "enclosing_class": "Svc", "receiver_member": "repo_",
    }
    edge = GraphEdge("c", "symbol:a.cpp:run", "name:save", "calls", "STRUCTURAL", "EXTRACTED", 1.0, dict(md))
    resolve_call_endpoints(nodes, [edge])
    assert edge.target == "symbol:repo.hpp:save" and edge.metadata["resolution"] == "declared_type"

    # no member_types on the class: stays untyped and unresolved
    nodes[0] = cls("a.hpp", "Svc", (1, 9))
    edge2 = GraphEdge("c", "symbol:a.cpp:run", "name:save", "calls", "STRUCTURAL", "EXTRACTED", 1.0, dict(md))
    resolve_call_endpoints(nodes, [edge2])
    assert edge2.target == "name:save" and "resolution" not in edge2.metadata


@TREE_SITTER_ONLY
def test_incremental_reresolution_matches_a_full_ingest(tmp_path, monkeypatch) -> None:
    """Editing one file re-resolves only affected call edges and lands on the same graph a full
    ingest of the same tree would (including the header-member join)."""
    from cortex.ingest import ingest_repository
    from cortex.store import CortexStore, default_db_path
    from tests.baseline_helpers import FIXTURES, commit_all, copy_fixture_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = copy_fixture_repo(FIXTURES / "calls_cpp", tmp_path / "repo")
    ingest_repository(repo, commit_limit=5)
    (repo / "util.cpp").write_text((repo / "util.cpp").read_text() + "\nint extra() { return clamp(1); }\n")
    (repo / "gateway.hpp").write_text((repo / "gateway.hpp").read_text() + "\n// touched\n")
    commit_all(repo, "edit")
    ingest_repository(repo, commit_limit=5, incremental=True)
    inc = {(e.source, e.target) for e in CortexStore(default_db_path(repo)).fetch_graph(repo)[1] if e.relation == "calls"}

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data2"))
    ingest_repository(repo, commit_limit=5)
    full = {(e.source, e.target) for e in CortexStore(default_db_path(repo)).fetch_graph(repo)[1] if e.relation == "calls"}
    assert inc == full
    assert ("symbol:util.cpp:extra", "symbol:util.hpp:clamp") in full
    assert ("symbol:gateway.cpp:send", "symbol:app.cpp:warm") in full


# -- namespace-scoped calls (plan 0.14, Task 1.1) ------------------------------------------------


def _namespace_sites(tmp_path, files: dict[str, str]):
    """Write `files`, extract and resolve them like a repo-wide ingest, return the `by_site` map."""
    from cortex.structural import extract_structural_edges

    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    known = set(files)
    nodes = [GraphNode(f"file:{p}", "file", p, p, "file", "", None, None, {}) for p in sorted(files)]
    edges: list[GraphEdge] = []
    for path in sorted(files):
        result = extract_structural_edges(path, files[path], known)
        nodes.extend(result.nodes)
        edges.extend(result.edges)
    resolve_call_endpoints(nodes, edges)
    return by_site(edges)


_NS_H = "namespace Ns {\nint f();\nint g();\n}\n"
_NS_CPP = '#include "ns.h"\nnamespace Ns {\nint f() { return 1; }\nint g() { return f(); }\n}\n'
_CALLER = '#include "ns.h"\nint main() { return Ns::f() + Ns::g(); }\n'


@TREE_SITTER_ONLY
def test_split_namespace_call_resolves_to_the_out_of_line_definition(tmp_path) -> None:
    sites = _namespace_sites(tmp_path, {"lib/ns.h": _NS_H, "lib/ns.cpp": _NS_CPP, "app/caller.cpp": _CALLER})
    assert_resolved(sites, "app/caller.cpp", 2, "f", "symbol:lib/ns.cpp:f", "scoped")
    assert_resolved(sites, "app/caller.cpp", 2, "g", "symbol:lib/ns.cpp:g", "scoped")


@TREE_SITTER_ONLY
def test_split_namespace_call_through_an_indirect_include(tmp_path) -> None:
    sites = _namespace_sites(
        tmp_path,
        {
            "lib/ns.h": _NS_H,
            "lib/ns.cpp": _NS_CPP,
            "app/facade.h": '#include "ns.h"\n',
            "app/caller.cpp": '#include "facade.h"\nint main() { return Ns::f(); }\n',
        },
    )
    assert_resolved(sites, "app/caller.cpp", 2, "f", "symbol:lib/ns.cpp:f", "scoped")


@TREE_SITTER_ONLY
def test_split_namespace_call_with_a_duplicated_header_basename(tmp_path) -> None:
    sites = _namespace_sites(
        tmp_path,
        {"lib/ns.h": _NS_H, "lib/ns.cpp": _NS_CPP, "other/ns.h": "namespace Other {}\n", "app/caller.cpp": _CALLER},
    )
    assert_resolved(sites, "app/caller.cpp", 2, "f", "symbol:lib/ns.cpp:f", "scoped")


@TREE_SITTER_ONLY
def test_namespace_call_prefers_production_over_a_mock_copy(tmp_path) -> None:
    mock = _NS_CPP.replace('"ns.h"', '"../../lib/ns.h"')
    sites = _namespace_sites(
        tmp_path,
        {"lib/ns.h": _NS_H, "lib/ns.cpp": _NS_CPP, "tests/mocks/ns.cpp": mock, "app/caller.cpp": _CALLER},
    )
    assert_resolved(sites, "app/caller.cpp", 2, "f", "symbol:lib/ns.cpp:f", "scoped")


@TREE_SITTER_ONLY
def test_same_label_namespaces_resolve_to_the_included_one_else_stay_ambiguous(tmp_path) -> None:
    files = {
        "one/ns_one.h": "namespace Ns {\nint f();\n}\n",
        "one/ns_one.cpp": '#include "ns_one.h"\nnamespace Ns {\nint f() { return 1; }\n}\n',
        "two/ns_two.h": "namespace Ns {\nint f();\n}\n",
        "two/ns_two.cpp": '#include "ns_two.h"\nnamespace Ns {\nint f() { return 2; }\n}\n',
        "app/uses_one.cpp": '#include "ns_one.h"\nint a() { return Ns::f(); }\n',
        "app/uses_none.cpp": "int b() { return Ns::f(); }\n",
    }
    sites = _namespace_sites(tmp_path, files)
    assert_resolved(sites, "app/uses_one.cpp", 2, "f", "symbol:one/ns_one.cpp:f", "scoped")
    assert_ambiguous(sites, "app/uses_none.cpp", 1, "f", ["symbol:one/ns_one.cpp:f", "symbol:two/ns_two.cpp:f"])


@TREE_SITTER_ONLY
def test_class_sharing_a_label_with_a_namespace_keeps_class_member_resolution(tmp_path) -> None:
    sites = _namespace_sites(
        tmp_path,
        {
            "lib/foo.h": "class Foo {\npublic:\nstatic int m();\n};\n",
            "lib/foo.cpp": '#include "foo.h"\nint Foo::m() { return 1; }\n',
            "lib/ns.h": "namespace Foo {\nint free_fn();\nint m();\n}\n",
            "lib/ns.cpp": '#include "ns.h"\nnamespace Foo {\nint free_fn() { return 2; }\nint m() { return 3; }\n}\n',
            "app/caller.cpp": '#include "../lib/foo.h"\nint main() { return Foo::m(); }\n',
        },
    )
    edge = sites[("app/caller.cpp", 2, "m")]
    assert edge.target in {"symbol:lib/foo.h:m", "symbol:lib/foo.cpp:m"}, edge.target
    assert edge.metadata["resolution"] == "scoped"


@TREE_SITTER_ONLY
def test_block_nested_namespaces_split_over_header_and_source(tmp_path) -> None:
    sites = _namespace_sites(
        tmp_path,
        {
            "lib/ab.h": "namespace A { namespace B { int f(); } }\n",
            "lib/ab.cpp": '#include "ab.h"\nnamespace A {\nnamespace B {\nint f() { return 1; }\n}\n}\n',
            "app/c.cpp": '#include "../lib/ab.h"\nint main() { return A::B::f(); }\n',
        },
    )
    assert_resolved(sites, "app/c.cpp", 2, "f", "symbol:lib/ab.cpp:f", "scoped")


# -- anonymous and nested namespaces (plan 0.14, Task 1.2) ----------------------------------------


def _extract_cpp(path: str, text: str):
    from cortex.structural import extract_structural_edges

    return extract_structural_edges(path, text, {path})


def _labels(result, kind: str) -> list[str]:
    return sorted(n.label for n in result.nodes if n.kind == kind)


@TREE_SITTER_ONLY
@pytest.mark.parametrize(
    "text",
    [
        "namespace {\nQString f();\n}\n",
        "namespace {\nQString f() { return {}; }\n}\n",
        "namespace A {\nnamespace {\nQStringList g() { return {}; }\n}\n}\n",
    ],
)
def test_anonymous_namespace_is_not_labelled_after_its_first_type(text: str) -> None:
    result = _extract_cpp("a.cpp", text)
    assert not [n for n in result.nodes if n.label in {"QString", "QStringList", "QMap"}], [n.node_id for n in result.nodes]


@TREE_SITTER_ONLY
def test_function_inside_an_anonymous_namespace_is_a_file_level_func() -> None:
    result = _extract_cpp("a.cpp", "namespace {\nQString f() { return {}; }\n}\n")
    assert [(n.kind, n.label) for n in result.nodes] == [("func", "f")]
    assert [e.target for e in result.edges if e.relation == "contains"] == ["symbol:a.cpp:f"]


@TREE_SITTER_ONLY
def test_call_to_a_function_in_an_anonymous_namespace_resolves_to_the_func() -> None:
    text = "namespace {\nint helper() { return 1; }\n}\nint top() { return helper(); }\n"
    result = _extract_cpp("a.cpp", text)
    assert [(n.node_id, n.kind) for n in result.nodes] == [("symbol:a.cpp:helper", "func"), ("symbol:a.cpp:top", "func")]
    nodes = [GraphNode("file:a.cpp", "file", "a.cpp", "a.cpp", "file", "", None, None, {}), *result.nodes]
    edges = list(result.edges)
    resolve_call_endpoints(nodes, edges)
    edge = by_site(edges)[("a.cpp", 4, "helper")]
    assert edge.target == "symbol:a.cpp:helper"
    assert {n.node_id: n.kind for n in nodes}[edge.target] == "func"


@TREE_SITTER_ONLY
def test_nested_namespace_is_labelled_by_its_last_segment() -> None:
    result = _extract_cpp("a.cpp", "namespace A::B {\nint f() { return 1; }\n}\n")
    assert [(n.node_id, n.kind, n.label) for n in result.nodes] == [
        ("symbol:a.cpp:B", "class", "B"),
        ("symbol:a.cpp:f", "func", "f"),
    ]
    deeper = _extract_cpp("a.cpp", "namespace A::B::C {\nint f() { return 1; }\n}\n")
    assert _labels(deeper, "class") == ["C"]


_PARITY_SOURCE = """\
namespace {
QString helper() { return QString(); }
}
namespace Named {
int one() { return 1; }
}
namespace A::B {
int two() { return 2; }
}
namespace Outer {
namespace {
int three() { return 3; }
}
}
"""


@TREE_SITTER_ONLY
def test_namespace_class_nodes_agree_between_tree_sitter_and_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    def class_nodes() -> list[tuple[str, str]]:
        return sorted((n.kind, n.label) for n in _extract_cpp("a.cpp", _PARITY_SOURCE).nodes if n.kind == "class")

    tree_sitter = class_nodes()
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    assert class_nodes() == tree_sitter == [("class", "B"), ("class", "Named"), ("class", "Outer")]
