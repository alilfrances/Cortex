"""Task 5: call-site metadata contract and the repo-wide call resolver.

Unit tiers use hand-built ``GraphNode``/``GraphEdge`` lists (no parser needed);
the Python end-to-end class at the bottom ingests real tmp repos, full and
incremental, and asserts the two converge.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.callresolve import MAX_CANDIDATES, MAX_INHERIT_DEPTH, resolve_call_endpoints
from cortex.ingest import ingest_repository
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore, default_db_path


def sym(path: str, name: str, kind: str = "func", span: tuple[int, int] | None = None, **meta) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind=kind,
        label=name.rsplit(".", 1)[-1].rsplit("::", 1)[-1],
        source_ref=path,
        granularity="symbol",
        span_start=span[0] if span else None,
        span_end=span[1] if span else None,
        metadata=dict(meta),
    )


def file_node(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path)


def call(
    source: str,
    callee: str,
    path: str,
    *,
    kind: str = "none",
    receiver=None,
    scope=None,
    receiver_type=None,
    enclosing_class=None,
    lineno: int = 1,
    confidence: str = "EXTRACTED",
    **extra,
) -> GraphEdge:
    return GraphEdge(
        edge_id=f"e:{source}:{callee}:{receiver}:{lineno}",
        source=source,
        target=f"name:{callee}",
        relation="calls",
        layer="STRUCTURAL",
        confidence=confidence,
        metadata={
            "lineno": lineno,
            "source_file": path,
            "callee": callee,
            "receiver": receiver,
            "receiver_kind": kind,
            "scope": scope,
            "receiver_type": receiver_type,
            "enclosing_class": enclosing_class,
            **extra,
        },
    )


def imports(src: str, dst: str) -> GraphEdge:
    return GraphEdge(f"imp:{src}:{dst}", f"file:{src}", f"file:{dst}", "imports", "STRUCTURAL")


def inherits(src_id: str, base: str) -> GraphEdge:
    return GraphEdge(f"inh:{src_id}:{base}", src_id, f"name:{base}", "inherits", "STRUCTURAL")


def resolve(nodes, edges, **kwargs):
    resolve_call_endpoints(nodes, edges, **kwargs)
    return {e.edge_id: e for e in edges if e.relation == "calls"}


def only(edges: list[GraphEdge]) -> GraphEdge:
    (edge,) = [e for e in edges if e.relation == "calls"]
    return edge


# -- tier: scoped ------------------------------------------------------------


def test_scoped_class_member_prefers_declaring_file_over_qualified_definition():
    nodes = [
        sym("a.hpp", "Cls", "class", (1, 5)),
        sym("a.hpp", "make", span=(2, 2)),
        sym("a.cpp", "make", qualifier="Cls"),
        sym("b.cpp", "run"),
    ]
    edge = call("symbol:b.cpp:run", "make", "b.cpp", kind="scoped", scope="Cls", receiver="Cls")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.hpp:make"
    assert edge.metadata["resolution"] == "scoped"


def test_scoped_class_member_falls_back_to_qualified_definition():
    nodes = [sym("a.hpp", "Cls", "class", (1, 5)), sym("a.cpp", "make", qualifier="Cls"), sym("b.cpp", "run")]
    edge = call("symbol:b.cpp:run", "make", "b.cpp", kind="scoped", scope="Cls")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.cpp:make"
    assert edge.metadata["resolution"] == "scoped"


def test_scoped_namespace_function_uses_qualifier():
    nodes = [sym("u.cpp", "helper", qualifier="util"), sym("u.cpp", "helper2"), sym("m.cpp", "main")]
    edge = call("symbol:m.cpp:main", "helper", "m.cpp", kind="scoped", scope="util")
    resolve(nodes, [edge])
    assert edge.target == "symbol:u.cpp:helper"
    assert edge.metadata["resolution"] == "scoped"


def test_scoped_unknown_scope_stays_unresolved_even_if_callee_is_unique():
    nodes = [sym("u.py", "join"), sym("m.py", "main")]
    edge = call("symbol:m.py:main", "join", "m.py", kind="scoped", scope="os.path", receiver="os.path", scope_module="os.path")
    resolve(nodes, [edge])
    assert edge.target == "name:join"
    assert "ambiguous" not in edge.metadata


def test_python_module_alias_resolves_through_dotted_module_suffix():
    nodes = [
        file_node("src/pkg/mod.py"),
        sym("src/pkg/mod.py", "run"),
        file_node("app.py"),
        sym("app.py", "main"),
        sym("other.py", "run"),
    ]
    edge = call("symbol:app.py:main", "run", "app.py", kind="scoped", scope="m", receiver="m", scope_module="pkg.mod")
    resolve(nodes, [edge])
    assert edge.target == "symbol:src/pkg/mod.py:run"
    assert edge.metadata["resolution"] == "scoped"


def test_python_imported_class_static_call_resolves_to_member():
    nodes = [
        file_node("models.py"),
        sym("models.py", "User", "class", (1, 10)),
        sym("models.py", "User.create", span=(2, 3)),
        sym("app.py", "main"),
    ]
    edge = call("symbol:app.py:main", "create", "app.py", kind="scoped", scope="User", receiver="User", scope_module="models.User")
    resolve(nodes, [edge])
    assert edge.target == "symbol:models.py:User.create"
    assert edge.metadata["resolution"] == "scoped"


# -- tier: this / self / implicit this ---------------------------------------


def test_self_call_resolves_to_enclosing_class_member_by_qualname_prefix_without_spans():
    nodes = [sym("a.py", "A", "class"), sym("a.py", "A.f"), sym("a.py", "A.g"), sym("a.py", "B", "class"), sym("a.py", "B.g")]
    edge = call("symbol:a.py:A.f", "g", "a.py", kind="self", receiver="self", enclosing_class="A")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.py:A.g"
    assert edge.metadata["resolution"] == "this"


def test_this_call_by_span_containment_with_tree_sitter_style_ids():
    nodes = [
        sym("w.cpp", "W", "class", (1, 20)),
        sym("w.cpp", "go", span=(2, 6)),
        sym("w.cpp", "stop", span=(8, 10)),
        sym("w.cpp", "Z", "class", (22, 30)),
        sym("w.cpp", "stop2", span=(23, 24)),
    ]
    edge = call("symbol:w.cpp:go", "stop", "w.cpp", kind="this", receiver="this", enclosing_class="W")
    resolve(nodes, [edge])
    assert edge.target == "symbol:w.cpp:stop"
    assert edge.metadata["resolution"] == "this"


def test_implicit_this_in_non_python_file_prefers_member_over_repo_unique_free_function():
    nodes = [
        sym("w.cpp", "W", "class", (1, 20)),
        sym("w.cpp", "go", span=(2, 6)),
        sym("w.cpp", "stop", span=(8, 10)),
        sym("z.cpp", "stop"),
    ]
    edge = call("symbol:w.cpp:go", "stop", "w.cpp", kind="none", enclosing_class="W")
    resolve(nodes, [edge])
    assert edge.target == "symbol:w.cpp:stop"
    assert edge.metadata["resolution"] == "this"


def test_implicit_this_for_out_of_class_cpp_definition_uses_qualifier_and_header_class():
    nodes = [
        sym("w.hpp", "W", "class", (1, 8)),
        sym("w.hpp", "stop", span=(2, 2)),
        sym("w.cpp", "go", qualifier="W"),
    ]
    edge = call("symbol:w.cpp:go", "stop", "w.cpp", kind="none", enclosing_class="W")
    resolve(nodes, [edge])
    assert edge.target == "symbol:w.hpp:stop"
    assert edge.metadata["resolution"] == "this"


def test_python_bare_call_is_not_implicit_this():
    nodes = [sym("a.py", "A", "class"), sym("a.py", "A.f"), sym("a.py", "A.g")]
    edge = call("symbol:a.py:A.f", "g", "a.py", kind="none", enclosing_class="A")
    resolve(nodes, [edge])
    assert edge.target == "name:g"


def test_explicit_self_that_is_missing_does_not_fall_through_to_repo_unique():
    nodes = [sym("a.py", "A", "class"), sym("a.py", "A.f"), sym("z.py", "g")]
    edge = call("symbol:a.py:A.f", "g", "a.py", kind="self", receiver="self", enclosing_class="A")
    resolve(nodes, [edge])
    assert edge.target == "name:g"


def test_super_call_skips_own_members_and_uses_bases():
    nodes = [
        sym("a.py", "Base", "class"),
        sym("a.py", "Base.f"),
        sym("a.py", "A", "class"),
        sym("a.py", "A.f"),
    ]
    edges = [inherits("symbol:a.py:A", "Base"), call("symbol:a.py:A.f", "f", "a.py", kind="self", receiver="super()", enclosing_class="A", super=True)]
    calls = resolve(nodes, edges)
    (edge,) = calls.values()
    assert edge.target == "symbol:a.py:Base.f"
    assert edge.metadata["resolution"] == "inherited"


# -- tier: declared type ------------------------------------------------------


def test_declared_receiver_type_resolves_member_and_stays_inside_that_class():
    nodes = [
        sym("a.hpp", "Engine", "class", (1, 5)),
        sym("a.hpp", "start", span=(2, 2)),
        sym("b.hpp", "Other", "class", (1, 5)),
        sym("b.hpp", "start", span=(2, 2)),
        sym("m.cpp", "main"),
    ]
    edge = call("symbol:m.cpp:main", "start", "m.cpp", kind="var", receiver="e", receiver_type="Engine")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.hpp:start"
    assert edge.metadata["resolution"] == "declared_type"


def test_declared_type_outside_repo_is_unresolved():
    nodes = [sym("a.hpp", "start"), sym("m.cpp", "main")]
    edge = call("symbol:m.cpp:main", "start", "m.cpp", kind="var", receiver="s", receiver_type="std::string")
    resolve(nodes, [edge])
    assert edge.target == "name:start"


def test_untyped_receiver_is_never_guessed_from_a_unique_label():
    nodes = [sym("a.hpp", "start"), sym("m.cpp", "main")]
    for kind in ("var", "chain", "new"):
        edge = call("symbol:m.cpp:main", "start", "m.cpp", kind=kind, receiver="x")
        resolve(nodes, [edge])
        assert edge.target == "name:start"


def test_declared_type_prefers_same_file_then_imported_class_of_that_name():
    nodes = [
        sym("a.py", "T", "class"),
        sym("a.py", "T.m"),
        sym("b.py", "T", "class"),
        sym("b.py", "T.m"),
        sym("c.py", "use"),
        sym("d.py", "use"),
    ]
    edges = [imports("c.py", "b.py")]
    near = call("symbol:c.py:use", "m", "c.py", kind="var", receiver="t", receiver_type="T")
    far = call("symbol:d.py:use", "m", "d.py", kind="var", receiver="t", receiver_type="T")
    resolve(nodes, [*edges, near, far])
    assert near.target == "symbol:b.py:T.m"
    assert far.target == "name:m" and far.metadata["ambiguous"] is True
    assert far.metadata["candidates"] == ["symbol:a.py:T.m", "symbol:b.py:T.m"]


# -- tier: inherited ------------------------------------------------------------


def _chain(depth: int):
    nodes = [sym("c.py", "C0", "class"), sym("c.py", "C0.f")]
    edges: list[GraphEdge] = []
    for level in range(1, depth + 1):
        nodes.append(sym("c.py", f"C{level}", "class"))
        edges.append(inherits(f"symbol:c.py:C{level}", f"C{level - 1}"))
    nodes.append(sym("m.py", "main"))
    return nodes, edges


def test_inherited_member_found_within_depth_bound():
    nodes, edges = _chain(MAX_INHERIT_DEPTH)
    edge = call("symbol:m.py:main", "f", "m.py", kind="var", receiver="x", receiver_type=f"C{MAX_INHERIT_DEPTH}")
    resolve(nodes, [*edges, edge])
    assert edge.target == "symbol:c.py:C0.f"
    assert edge.metadata["resolution"] == "inherited"


def test_inheritance_search_is_bounded():
    nodes, edges = _chain(MAX_INHERIT_DEPTH + 1)
    edge = call("symbol:m.py:main", "f", "m.py", kind="var", receiver="x", receiver_type=f"C{MAX_INHERIT_DEPTH + 1}")
    resolve(nodes, [*edges, edge])
    assert edge.target == "name:f"


def test_inheritance_cycle_terminates_and_nearest_base_wins():
    nodes = [
        sym("c.py", "A", "class"),
        sym("c.py", "B", "class"),
        sym("c.py", "B.f"),
        sym("c.py", "Root", "class"),
        sym("c.py", "Root.f"),
        sym("m.py", "main"),
    ]
    edges = [
        inherits("symbol:c.py:A", "B"),
        inherits("symbol:c.py:B", "A"),
        inherits("symbol:c.py:B", "Root"),
    ]
    edge = call("symbol:m.py:main", "f", "m.py", kind="var", receiver="a", receiver_type="A")
    resolve(nodes, [*edges, edge])
    assert edge.target == "symbol:c.py:B.f"
    assert edge.metadata["resolution"] == "inherited"


def test_two_bases_defining_the_member_is_ambiguous():
    nodes = [
        sym("c.py", "A", "class"),
        sym("c.py", "L", "class"),
        sym("c.py", "L.f"),
        sym("c.py", "R", "class"),
        sym("c.py", "R.f"),
        sym("m.py", "main"),
    ]
    edges = [inherits("symbol:c.py:A", "L"), inherits("symbol:c.py:A", "R")]
    edge = call("symbol:m.py:main", "f", "m.py", kind="var", receiver="a", receiver_type="A")
    resolve(nodes, [*edges, edge])
    assert edge.target == "name:f"
    assert edge.metadata["candidates"] == ["symbol:c.py:L.f", "symbol:c.py:R.f"]


# -- tiers: same file, import scoped, repo unique -----------------------------------


def test_free_call_tier_order_same_file_then_imports_then_repo_unique():
    nodes = [
        sym("a.py", "helper"),
        sym("a.py", "main"),
        sym("b.py", "helper"),
        sym("b.py", "only_b"),
        sym("c.py", "only_c"),
        sym("c.py", "only_b2"),
    ]
    e_same = call("symbol:a.py:main", "helper", "a.py")
    e_imp = call("symbol:a.py:main", "only_b", "a.py", lineno=2)
    e_uniq = call("symbol:a.py:main", "only_c", "a.py", lineno=3)
    resolve(nodes, [imports("a.py", "b.py"), e_same, e_imp, e_uniq])
    assert (e_same.target, e_same.metadata["resolution"]) == ("symbol:a.py:helper", "same_file")
    assert (e_imp.target, e_imp.metadata["resolution"]) == ("symbol:b.py:only_b", "import_scoped")
    assert (e_uniq.target, e_uniq.metadata["resolution"]) == ("symbol:c.py:only_c", "repo_unique")


def test_import_scope_disambiguates_a_label_that_is_not_repo_unique():
    nodes = [sym("a.py", "main"), sym("b.py", "dup"), sym("c.py", "dup")]
    edge = call("symbol:a.py:main", "dup", "a.py")
    resolve(nodes, [imports("a.py", "c.py"), edge])
    assert edge.target == "symbol:c.py:dup"
    assert edge.metadata["resolution"] == "import_scoped"


def test_python_from_import_hint_beats_repo_wide_homonyms_and_follows_init_reexport():
    nodes = [
        file_node("pkg/__init__.py"),
        file_node("pkg/impl.py"),
        sym("pkg/impl.py", "util"),
        sym("zzz.py", "util"),
        sym("app.py", "main"),
    ]
    edge = call("symbol:app.py:main", "util", "app.py", import_module="pkg")
    resolve(nodes, [imports("pkg/__init__.py", "pkg/impl.py"), edge])
    assert edge.target == "symbol:pkg/impl.py:util"
    assert edge.metadata["resolution"] == "import_scoped"


def test_free_calls_never_resolve_to_methods_or_python_builtins():
    nodes = [
        sym("a.py", "A", "class"),
        sym("a.py", "A.go"),
        sym("a.py", "A.open"),
        sym("m.py", "main"),
        sym("t.py", "open"),
    ]
    method = call("symbol:m.py:main", "go", "m.py")
    builtin = call("symbol:m.py:main", "open", "m.py", lineno=2)
    resolve(nodes, [method, builtin])
    assert method.target == "name:go"
    assert builtin.target == "name:open"


def test_python_nested_function_is_only_visible_in_its_lexical_scope():
    nodes = [sym("a.py", "f"), sym("a.py", "f.inner"), sym("a.py", "g")]
    inside = call("symbol:a.py:f", "inner", "a.py")
    outside = call("symbol:a.py:g", "inner", "a.py", lineno=2)
    resolve(nodes, [inside, outside])
    assert inside.target == "symbol:a.py:f.inner"
    assert outside.target == "name:inner"


def test_header_declaration_and_same_stem_definition_collapse_to_the_declaration():
    nodes = [sym("src/e.hpp", "tick"), sym("src/e.cpp", "tick"), sym("m.cpp", "main")]
    edge = call("symbol:m.cpp:main", "tick", "m.cpp")
    resolve(nodes, [edge])
    assert edge.target == "symbol:src/e.hpp:tick"
    assert edge.metadata["resolution"] == "repo_unique"


def test_self_recursion_resolves_to_the_source_symbol():
    nodes = [sym("a.py", "fact")]
    edge = call("symbol:a.py:fact", "fact", "a.py")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.py:fact"


def test_file_scope_call_source_is_supported():
    nodes = [sym("a.py", "main")]
    edge = call("file:a.py", "main", "a.py")
    resolve(nodes, [edge])
    assert edge.target == "symbol:a.py:main"


# -- ambiguity ---------------------------------------------------------------


def test_ambiguity_keeps_name_target_low_confidence_and_sorted_capped_candidates():
    names = [f"m{n:02d}.py" for n in range(MAX_CANDIDATES + 4)]
    nodes = [sym(path, "dup") for path in reversed(names)] + [sym("main.py", "main")]
    edge = call("symbol:main.py:main", "dup", "main.py")
    resolve(nodes, [edge])
    assert edge.target == "name:dup"
    assert edge.confidence == "LOW"
    assert edge.metadata["ambiguous"] is True
    assert edge.metadata["candidates"] == [f"symbol:{path}:dup" for path in names[:MAX_CANDIDATES]]
    assert "resolution" not in edge.metadata


def test_already_low_confidence_edge_stays_low_and_records_no_restore_marker():
    nodes = [sym("a.py", "dup"), sym("b.py", "dup"), sym("m.py", "main")]
    edge = call("symbol:m.py:main", "dup", "m.py", confidence="LOW")
    resolve(nodes, [edge])
    assert edge.confidence == "LOW" and "extracted_confidence" not in edge.metadata


def test_re_resolution_resets_stale_resolution_ambiguity_and_confidence():
    nodes = [sym("a.py", "dup"), sym("b.py", "dup"), sym("m.py", "main")]
    edge = call("symbol:m.py:main", "dup", "m.py")
    resolve(nodes, [edge])
    assert edge.metadata["ambiguous"] is True and edge.confidence == "LOW"

    # one definition disappears -> resolved, ambiguity fully cleared
    resolve([n for n in nodes if n.node_id != "symbol:b.py:dup"], [edge])
    assert edge.target == "symbol:a.py:dup" and edge.confidence == "EXTRACTED"
    assert set(edge.metadata) & {"ambiguous", "candidates", "extracted_confidence"} == set()

    # last definition disappears -> back to the extractor's name target
    resolve([n for n in nodes if n.label != "dup"], [edge])
    assert edge.target == "name:dup" and "resolution" not in edge.metadata


def test_resolution_is_deterministic_regardless_of_input_order():
    nodes = [sym("a.py", "dup"), sym("b.py", "dup"), sym("m.py", "main")]
    first = call("symbol:m.py:main", "dup", "m.py")
    second = call("symbol:m.py:main", "dup", "m.py")
    resolve(nodes, [first])
    resolve(list(reversed(nodes)), [second])
    assert first.metadata == second.metadata and first.target == second.target


def test_qml_and_legacy_call_edges_without_the_contract_are_untouched():
    nodes = [sym("a.py", "helper"), sym("m.py", "main")]
    legacy = GraphEdge("old", "symbol:m.py:main", "name:helper", "calls", "STRUCTURAL", metadata={"lineno": 3})
    qml = GraphEdge("qml", "symbol:m.py:main", "name:helper", "calls", "STRUCTURAL", metadata={"callee": "helper"})
    resolve(nodes, [legacy, qml])
    assert legacy.target == qml.target == "name:helper"


# -- incremental scoping ------------------------------------------------------------


def test_scoped_re_resolution_only_touches_affected_edges():
    nodes = [sym("a.py", "helper"), sym("m.py", "main"), sym("n.py", "other")]
    stale = call("symbol:m.py:main", "helper", "m.py")
    untouched = call("symbol:n.py:other", "helper", "n.py", lineno=2)
    resolve(nodes, [stale, untouched])
    assert stale.target == untouched.target == "symbol:a.py:helper"

    # a second `helper` appears in an unrelated file: only edges with that label re-resolve
    nodes.append(sym("z.py", "helper"))
    third = call("symbol:m.py:main", "unrelated", "m.py", lineno=9)
    third.target = "symbol:a.py:helper"  # would be reset if (wrongly) touched
    resolve(nodes, [stale, untouched, third], changed_labels={"helper"})
    assert stale.metadata["ambiguous"] and untouched.metadata["ambiguous"]
    assert third.target == "symbol:a.py:helper"  # not in scope -> left alone


def test_scoped_re_resolution_picks_up_batch_files_and_dangling_targets():
    nodes = [sym("m.py", "main"), sym("b.py", "gone_label")]
    in_batch = call("symbol:m.py:main", "x", "m.py")
    dangling = call("symbol:b.py:gone_label", "y", "b.py", lineno=2)
    dangling.target = "symbol:deleted.py:y"
    bystander = call("symbol:b.py:gone_label", "z", "b.py", lineno=3)
    bystander.target = "symbol:kept.py:z"
    nodes += [sym("kept.py", "z"), sym("q.py", "x")]
    resolve(nodes, [in_batch, dangling, bystander], affected_files={"m.py"}, changed_labels=set())
    assert in_batch.target == "symbol:q.py:x"
    assert dangling.target == "name:y"  # target vanished -> reset
    assert bystander.target == "symbol:kept.py:z"


def test_widen_typed_re_resolves_member_lookups_without_label_change():
    nodes = [sym("c.py", "A", "class"), sym("c.py", "Root", "class"), sym("c.py", "Root.f"), sym("m.py", "main")]
    edge = call("symbol:m.py:main", "f", "m.py", kind="var", receiver="a", receiver_type="A")
    resolve(nodes, [edge])
    assert edge.target == "name:f"
    inh = inherits("symbol:c.py:A", "Root")
    resolve(nodes, [inh, edge], affected_files=set(), changed_labels=set(), widen_typed=False)
    assert edge.target == "name:f"  # nothing in scope: untouched
    resolve(nodes, [inh, edge], affected_files=set(), changed_labels=set(), widen_typed=True)
    assert edge.target == "symbol:c.py:Root.f"


# -- Python extractor contract -----------------------------------------------


def test_python_extractor_emits_the_full_call_metadata_contract():
    from cortex.ast_extract import extract_python_edges

    source = (
        "import pkg.mod as m\n"
        "from util import helper as h, Thing\n"
        "class A(Base):\n"
        "    def f(self, x: Foo):\n"
        "        self.g(); super().f(); m.run(); h(); Thing.make(); x.go(); Foo().bar(); self.a.b()\n"
        "        z = Bar(); z.q()\n"
        "    def g(self): pass\n"
        "main()\n"
    )
    _, edges = extract_python_edges("src/a.py", source, set())
    by_site = {(e.metadata["receiver"], e.metadata["callee"]): e for e in edges if e.relation == "calls"}
    keys = {"lineno", "source_file", "callee", "receiver", "receiver_kind", "scope", "receiver_type", "enclosing_class"}
    assert all(keys <= set(e.metadata) for e in by_site.values())
    assert all(e.target == f"name:{e.metadata['callee']}" for e in by_site.values())
    assert len({e.edge_id for e in by_site.values()}) == len(by_site)

    def kind(receiver, callee):
        return by_site[(receiver, callee)].metadata

    assert kind("self", "g")["receiver_kind"] == "self" and kind("self", "g")["enclosing_class"] == "A"
    assert kind("super()", "f")["super"] is True
    assert kind("m", "run")["receiver_kind"] == "scoped" and kind("m", "run")["scope_module"] == "pkg.mod"
    assert kind("Thing", "make")["scope_module"] == "util.Thing"
    assert kind("x", "go")["receiver_kind"] == "var" and kind("x", "go")["receiver_type"] == "Foo"
    assert kind("Foo()", "bar")["receiver_kind"] == "new" and kind("Foo()", "bar")["receiver_type"] == "Foo"
    assert kind("self.a", "b")["receiver_kind"] == "chain"
    assert kind("z", "q")["receiver_type"] == "Bar"
    helper = kind(None, "helper")
    assert helper["import_module"] == "util" and helper["alias"] == "h" and helper["receiver_kind"] == "none"
    top = by_site[(None, "main")]
    assert top.source == "file:src/a.py" and top.metadata["enclosing_class"] is None


# -- Python end to end: full vs incremental convergence ----------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    return repo


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _call_state(store: CortexStore, repo: Path) -> dict[str, tuple]:
    _, edges = store.fetch_graph(repo)
    return {
        e.edge_id: (e.source, e.target, e.confidence, json.dumps(e.metadata, sort_keys=True))
        for e in edges
        if e.relation == "calls"
    }


def _full_state(repo: Path, tmp_path: Path, name: str) -> dict[str, tuple]:
    db = tmp_path / f"{name}.db"
    ingest_repository(repo, commit_limit=20, db_path=db)
    return _call_state(CortexStore(db), repo)


def _assert_converged(repo: Path, tmp_path: Path, db: Path, label: str) -> dict[str, tuple]:
    incremental = _call_state(CortexStore(db), repo)
    assert incremental == _full_state(repo, tmp_path, f"full-{label}"), label
    return incremental


def test_incremental_ingest_converges_with_full_ingest_across_add_remove_and_ambiguity(tmp_path):
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("from lib import work\n\n\ndef main():\n    work()\n    later()\n")
    (repo / "lib.py").write_text("def work():\n    return 1\n")
    _commit(repo, "init")
    db = tmp_path / "inc.db"
    ingest_repository(repo, commit_limit=20, db_path=db)
    state = _assert_converged(repo, tmp_path, db, "initial")
    by_callee = {json.loads(v[3])["callee"]: v for v in state.values()}
    assert by_callee["work"][1] == "symbol:lib.py:work"
    assert by_callee["later"][1] == "name:later"  # unresolved: nothing defines it yet

    # 1. a new file defines `later` -> a previously *unresolved* edge in an unchanged file resolves
    (repo / "extra.py").write_text("def later():\n    return 2\n")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "unresolved-resolves")
    assert any(v[1] == "symbol:extra.py:later" for v in state.values())

    # 2. a second definition makes it ambiguous (LOW) and the stale resolved target is dropped
    (repo / "extra2.py").write_text("def later():\n    return 3\n")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "becomes-ambiguous")
    ambiguous = [v for v in state.values() if json.loads(v[3]).get("ambiguous")]
    assert len(ambiguous) == 1
    assert ambiguous[0][1] == "name:later" and ambiguous[0][2] == "LOW"
    assert json.loads(ambiguous[0][3])["candidates"] == ["symbol:extra.py:later", "symbol:extra2.py:later"]

    # 3. one definition is deleted -> ambiguity clears, confidence restored, resolves to the survivor
    (repo / "extra2.py").unlink()
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "ambiguity-clears")
    assert any(v[1] == "symbol:extra.py:later" and v[2] == "EXTRACTED" for v in state.values())

    # 4. the last definition is deleted -> a previously *resolved* edge falls back to name:
    (repo / "extra.py").unlink()
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "resolved-dangles")
    assert not any(v[1] == "symbol:extra.py:later" for v in state.values())

    # 5. a callee is renamed inside a changed file: old edge target must not survive
    (repo / "lib.py").write_text("def work_renamed():\n    return 11\n")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "callee-renamed")
    assert not any(v[1] == "symbol:lib.py:work" for v in state.values())


def test_incremental_ingest_re_resolves_member_calls_when_a_base_class_changes(tmp_path):
    repo = _repo(tmp_path)
    (repo / "base.py").write_text("class Root:\n    def ping(self):\n        return 1\n\n\nclass Mid(Root):\n    pass\n")
    (repo / "leaf.py").write_text("from base import Mid\n\n\nclass Leaf(Mid):\n    def go(self):\n        return self.ping()\n")
    _commit(repo, "init")
    db = tmp_path / "inc.db"
    ingest_repository(repo, commit_limit=20, db_path=db)
    state = _assert_converged(repo, tmp_path, db, "inherit-initial")
    assert any(v[1] == "symbol:base.py:Root.ping" for v in state.values())

    # Mid stops inheriting from Root (label `ping` is unchanged by this edit)
    (repo / "base.py").write_text("class Root:\n    def ping(self):\n        return 1\n\n\nclass Mid:\n    x = 1\n\n")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    state = _assert_converged(repo, tmp_path, db, "inherit-removed")
    assert not any(v[1] == "symbol:base.py:Root.ping" for v in state.values())


def test_python_cross_module_calls_resolve_end_to_end(tmp_path):
    repo = _repo(tmp_path)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "store.py").write_text(
        "class Store:\n    def save(self, row):\n        return row\n\n\ndef open_store():\n    return Store()\n"
    )
    (repo / "app.py").write_text(
        "from pkg import store\nfrom pkg.store import Store, open_store as opener\n\n\n"
        "def run(s: Store):\n    s.save(1)\n    opener()\n    store.open_store()\n    Store().save(2)\n"
    )
    _commit(repo, "init")
    db = tmp_path / "x.db"
    ingest_repository(repo, commit_limit=20, db_path=db)
    state = _call_state(CortexStore(db), repo)
    resolved = {
        (json.loads(v[3])["lineno"], json.loads(v[3])["callee"]): (v[1], json.loads(v[3]).get("resolution"))
        for v in state.values()
        if v[0] == "symbol:app.py:run"
    }
    assert resolved[(6, "save")] == ("symbol:pkg/store.py:Store.save", "declared_type")
    assert resolved[(7, "open_store")] == ("symbol:pkg/store.py:open_store", "import_scoped")
    assert resolved[(8, "open_store")] == ("symbol:pkg/store.py:open_store", "scoped")
    assert resolved[(9, "save")] == ("symbol:pkg/store.py:Store.save", "declared_type")
    assert resolved[(9, "Store")] == ("symbol:pkg/store.py:Store", "import_scoped")


@pytest.mark.parametrize("count", [2000])
def test_resolving_2000_symbols_stays_under_half_a_second(count):
    import time

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for i in range(count):
        path = f"m{i // 4:04d}.py"
        cls = f"C{i}"
        nodes.append(file_node(path)) if i % 4 == 0 else None
        nodes.append(sym(path, cls, "class", (i * 10 + 1, i * 10 + 9)))
        nodes.append(sym(path, f"{cls}.run{i}", span=(i * 10 + 2, i * 10 + 3)))
        nodes.append(sym(path, f"free{i}"))
        edges.append(imports(path, f"m{((i // 4) + 1) % (count // 4):04d}.py"))
        src = f"symbol:{path}:{cls}.run{i}"
        edges.append(call(src, f"run{i}", path, kind="self", receiver="self", enclosing_class=cls, lineno=i + 1))
        edges.append(call(src, f"free{(i + 7) % count}", path, lineno=i + 2))  # repo_unique
        edges.append(call(src, f"free{i}", path, lineno=i + 3))  # same_file
        edges.append(call(src, f"run{(i + 1) % count}", path, kind="var", receiver="o", receiver_type=f"C{(i + 1) % count}", lineno=i + 4))
        edges.append(call(src, "shared", path, lineno=i + 5))  # unresolved
        edges.append(inherits(f"symbol:{path}:{cls}", f"C{(i + 1) % count}"))
    assert len([n for n in nodes if n.granularity == "symbol"]) >= 3 * count

    started = time.perf_counter()
    resolve_call_endpoints(nodes, edges)
    elapsed = time.perf_counter() - started
    calls = [e for e in edges if e.relation == "calls"]
    assert sum(1 for e in calls if e.target.startswith("symbol:")) >= 4 * count
    assert elapsed < 0.5, f"resolving {count} symbols took {elapsed:.3f}s"


# -- namespace candidates in prefer()/locality() (plan 0.14, Task 1.1) ------------------------------


def _ns(path: str, name: str = "Ns", span: tuple[int, int] = (1, 5)) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="class", label=name, source_ref=path, granularity="symbol",
        signature=f"namespace {name}", span_start=span[0], span_end=span[1], metadata={},
    )


def test_locality_keeps_every_namespace_node_of_a_split_namespace():
    """The header's `namespace Ns` and the .cpp's are one entity: neither the header step nor the
    directory step may drop the one that holds the out-of-line members."""
    from cortex.callresolve import _Index

    nodes = [_ns("lib/ns.h"), _ns("lib/ns.cpp"), sym("app/c.cpp", "main")]
    index = _Index(nodes, [])
    ids = ["symbol:lib/ns.cpp:Ns", "symbol:lib/ns.h:Ns"]
    assert index.locality(ids, "app/c.cpp") == ids
    assert index.locality(ids, "lib/other.cpp") == ids


def test_locality_still_narrows_classes_to_the_header():
    from cortex.callresolve import _Index

    nodes = [
        sym("lib/a.h", "Foo", "class", (1, 5)),
        sym("lib/a.cpp", "Foo", "class", (1, 5)),
    ]
    index = _Index(nodes, [])
    assert index.locality(["symbol:lib/a.cpp:Foo", "symbol:lib/a.h:Foo"], "app/c.cpp") == ["symbol:lib/a.h:Foo"]


def test_locality_prefers_the_callers_own_directory_over_a_subdirectory():
    """Nearest directory: from `src/a.cpp`, `src/b.h` (same directory) beats `src/x/b.h`, which shares
    the same prefix but lies one level below it."""
    from cortex.callresolve import _Index

    nodes = [sym("src/b.h", "Base", "class", (1, 3)), sym("src/x/b.h", "Base", "class", (1, 3))]
    index = _Index(nodes, [])
    assert index.locality(["symbol:src/b.h:Base", "symbol:src/x/b.h:Base"], "src/a.cpp") == ["symbol:src/b.h:Base"]
    # two subdirectories one level below the caller stay a tie
    nodes = [sym("src/x/b.h", "Base", "class", (1, 3)), sym("src/y/b.h", "Base", "class", (1, 3))]
    index = _Index(nodes, [])
    ids = ["symbol:src/x/b.h:Base", "symbol:src/y/b.h:Base"]
    assert index.locality(ids, "src/a.cpp") == ids
