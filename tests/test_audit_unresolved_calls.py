"""Buckets of scripts/perf/audit_unresolved_calls.py (Task 1.4): the remaining misses, classified."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from cortex.callresolve import _Index, language_family
from cortex.ingest import ingest_repository
from cortex.models import GraphEdge, GraphNode
from tests.bundle_shapes import make_repo

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "audit_unresolved_calls.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cortex_audit_unresolved_calls", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_script = _load()


def _symbol(path: str, label: str, kind: str, signature: str = "") -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{label}", kind=kind, label=label, source_ref=path,
        granularity="symbol", signature=signature, span_start=1, span_end=50,
    )


def _call(path: str, callee: str, **md: object) -> GraphEdge:
    metadata = {"callee": callee, "receiver_kind": "none", "source_file": path, **md}
    return GraphEdge(edge_id=f"{path}:{callee}:{len(md)}", source=f"file:{path}", target=f"name:{callee}",
                     relation="calls", metadata=metadata)


NODES = [
    _symbol("src/ns.h", "Util", "class", "namespace Util {"),
    _symbol("src/widget.h", "Widget", "class", "class Widget {"),
    _symbol("src/same.h", "Dual", "class", "namespace Dual {"),
    _symbol("src/same2.h", "Dual", "class", "class Dual {"),
    _symbol("src/lib.cpp", "defined_here", "func"),
]


def _classify(edge: GraphEdge) -> str:
    # one index per language family, as `audit` builds them
    family = language_family(str(edge.metadata["source_file"]))
    return audit_script.classify(edge, _Index([n for n in NODES if language_family(n.source_ref) == family], []))


def test_scope_naming_only_namespace_nodes_is_scoped_namespace() -> None:
    assert _classify(_call("src/a.cpp", "missing", receiver_kind="scoped", scope="Util")) == "scoped_namespace"
    assert _classify(_call("src/a.cpp", "missing", receiver_kind="scoped", scope="app::Util")) == "scoped_namespace"


def test_scope_naming_a_class_or_a_mixed_label_is_not_scoped_namespace() -> None:
    assert _classify(_call("src/a.cpp", "missing", receiver_kind="scoped", scope="Widget")) == "member_not_found"
    assert _classify(_call("src/a.cpp", "missing", receiver_kind="scoped", scope="Dual")) == "member_not_found"
    assert _classify(_call("src/a.cpp", "missing", receiver_kind="scoped", scope="QString")) == "type_not_in_repo"


def test_external_is_checked_before_every_other_bucket() -> None:
    edge = _call("src/a.cpp", "missing", receiver_kind="scoped", scope="Util", ambiguous=True, external="std")
    assert _classify(edge) == "external"
    assert _classify(_call("src/a.js", "missing", external=True)) == "external"
    assert _classify(_call("src/a.cpp", "missing", external=None)) == "free_not_in_repo"


def test_callers_outside_the_c_family_get_one_bucket_per_family() -> None:
    assert _classify(_call("web/app.js", "missing")) == "not_cpp_js"
    assert _classify(_call("web/app.tsx", "missing", receiver_kind="var")) == "not_cpp_js"
    assert _classify(_call("app/Main.java", "missing", receiver_kind="chain")) == "not_cpp_java"
    assert _classify(_call("tools/run.py", "missing", receiver_kind="scoped", scope="Util")) == "not_cpp_py"
    assert _classify(_call("src/a.cpp", "missing")) == "free_not_in_repo"
    assert _classify(_call("src/a.h", "defined_here")) == "free_other"  # the C family keeps the old buckets


def test_existing_buckets_keep_their_order() -> None:
    assert _classify(_call("src/a.cpp", "x", ambiguous=True)) == "ambiguous"
    assert _classify(_call("src/a.cpp", "x", receiver_type="qobject_cast", receiver_kind="var")) == "cast_local"
    assert _classify(_call("src/a.cpp", "x", receiver_kind="chain")) == "chain"
    assert _classify(_call("src/a.cpp", "x", receiver_kind="var")) == "untyped_receiver"
    assert _classify(_call("src/a.cpp", "x", receiver_kind="var", receiver_type="Widget")) == "member_not_found"


def test_audit_all_buckets_every_unresolved_edge_and_the_default_samples(tmp_path: Path) -> None:
    body = "\n".join(f"void f{i}() {{ missing_{i}(); }}" for i in range(12))
    repo = make_repo(
        tmp_path / "repo", {"src/a.cpp": body + "\n", "app.js": "function g() { nothere(); }\n"}, ingest=False
    )
    ingest_repository(repo, incremental=False)

    sampled = audit_script.audit(repo, 3, None)
    everything = audit_script.audit(repo, 3, None, all_edges=True)
    assert sampled["unresolved_calls"] == everything["unresolved_calls"] >= 13
    assert sampled["sampled"] <= 3 + 1
    assert everything["sampled"] == everything["unresolved_calls"]
    assert sum(everything["buckets"].values()) == everything["unresolved_calls"]
    assert everything["buckets"]["free_not_in_repo"] == 12
    assert everything["buckets"]["not_cpp_js"] == 1
    assert everything["buckets"].get("scoped_namespace", 0) == 0


# --- Task 6.1: why the `receiver_member` join fails ---------------------------------------------------------

_MEMBER_REPO = {
    "src/target.h": "#pragma once\nclass Target { public: void go(); };\nclass Other { public: void go(); };\n",
    "src/target.cpp": '#include "target.h"\nvoid Target::go() {}\nvoid Other::go() {}\n',
    # two classes of one label in directories that are not the caller's
    "a/twin.h": "class Twin { public: void go(); };\n",
    "b/twin.h": "class Twin { public: void go(); };\n",
    "src/owner.h": (
        '#pragma once\n#include "target.h"\n'
        "class Owner {\npublic:\n    void run();\nprivate:\n"
        "    Target *m_target;\n"
        "    ExtHandle<Target> m_handle;\n"
        "    ExtThing *m_ext;\n"
        "    Target *m_noMethod;\n"
        "    Twin *m_twin;\n};\n"
        "class BaseA { Target *m_dup; };\nclass BaseB { Other *m_dup; };\n"
        "class Multi : public BaseA, public BaseB { public: void run(); };\n"
    ),
    "src/owner.cpp": (
        '#include "owner.h"\n'
        "void Owner::run() {\n"
        "    m_target->go();\n"          # resolves: not in any bucket
        "    m_handle->go();\n"          # wrapper around a repository class
        "    m_ext->go();\n"             # declared type is outside the repository
        "    m_undeclared->go();\n"      # no class declares it
        "    m_noMethod->missing();\n"   # type resolves, the method does not exist
        "    m_twin->go();\n"            # two classes of that label
        "}\n"
        "void Multi::run() { m_dup->go(); }\n"   # the two bases declare different types
        "void Ghost::run() { m_x->go(); }\n"     # no class node for Ghost
    ),
}

_MEMBER_BUCKETS = {
    "m_handle": "member:type_wrapped",
    "m_ext": "member:type_external",
    "m_undeclared": "member:not_declared",
    "m_noMethod": "member:method_not_found",
    "m_twin": "member:ambiguous",
    "m_dup": "member:ambiguous",
    "m_x": "member:no_enclosing_class",
}


def test_each_member_sub_bucket_on_a_real_cpp_repo(tmp_path: Path) -> None:
    from cortex.store import CortexStore, default_db_path

    repo = make_repo(tmp_path / "repo", _MEMBER_REPO)
    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)
    index = _Index(nodes, [e for e in edges if e.relation in ("imports", "inherits")])
    got = {
        str(e.metadata["receiver_member"]): audit_script.classify(e, index, repo)
        for e in edges
        if e.relation == "calls" and e.target.startswith("name:") and e.metadata.get("receiver_member")
    }
    assert got == _MEMBER_BUCKETS  # `m_target->go()` resolved, so it is not an unresolved edge

    report = audit_script.audit(repo, 0, None, all_edges=True)
    for bucket in set(_MEMBER_BUCKETS.values()):
        assert report["buckets"][bucket] == list(_MEMBER_BUCKETS.values()).count(bucket)
    assert sum(report["buckets"].values()) == report["unresolved_calls"]


def _member_class(label: str, members: dict[str, str], path: str = "src/owner.h") -> GraphNode:
    node = _symbol(path, label, "class", f"class {label} {{")
    node.metadata = {"member_types": members}
    return node


def _member_call(member: str, **md: object) -> GraphEdge:
    return _call("src/owner.cpp", "go", receiver_kind="var", receiver=member, receiver_member=member,
                 enclosing_class="Owner", **md)


def test_member_sub_buckets_use_the_resolvers_member_join() -> None:
    nodes = [
        _member_class("Owner", {"m_wrapped": "Handle<Target>", "m_plain": "Target", "m_ext": "Handle<Ext>",
                                "m_both": "Target"}),
        _member_class("Base", {"m_both": "Other", "m_inherited": "Handle<Target>"}, "src/base.h"),
        _symbol("src/target.h", "Target", "class", "class Target {"),
        _symbol("src/target.h", "Other", "class", "class Other {"),
        _symbol("src/target.h", "go", "func"),
    ]
    inherits = GraphEdge(edge_id="i", source="symbol:src/owner.h:Owner", target="symbol:src/base.h:Base",
                         relation="inherits", metadata={})
    index = _Index(nodes, [inherits])

    def bucket(member: str, **md: object) -> str:
        return audit_script.classify(_member_call(member, **md), index)

    assert bucket("m_wrapped") == "member:type_wrapped"   # the template argument names a repository class
    assert bucket("m_ext") == "member:type_external"      # ... and here it names none
    assert bucket("m_nowhere") == "member:not_declared"
    # the nearest declaration wins, as in `_member_type`: Owner's own `m_both`, not Base's
    assert bucket("m_both") == "member:method_not_found"
    # a typed receiver, a scope or an `ambiguous` mark is decided before the member join is consulted
    assert bucket("m_nowhere", receiver_type="Target") == "member_not_found"
    assert bucket("m_nowhere", ambiguous=True) == "ambiguous"
    assert bucket("m_nowhere", scope="Util") == "type_not_in_repo"
    # a receiver that is not a member variable keeps the old bucket
    assert audit_script.classify(_call("src/owner.cpp", "go", receiver_kind="var"), index) == "untyped_receiver"
    # a base is searched when the class does not declare the member
    assert bucket("m_inherited") == "member:type_wrapped"
