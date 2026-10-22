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
