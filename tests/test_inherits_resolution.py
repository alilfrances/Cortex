"""B.1 (0.12.2): `inherits` edges are resolved to `symbol:` ids in the graph, like `calls` edges."""
from __future__ import annotations

import json

from cortex.callresolve import MAX_CANDIDATES, resolve_call_endpoints
from cortex.ingest import ingest_repository
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore, default_db_path
from tests.bundle_shapes import make_repo


def test_inherits_resolution_rate(field_shape_repo):
    """Hard gate: at least 80% of the `inherits` edges whose base class is defined in the repository resolve to
    a `symbol:` target on the --field-shape repository (0% in 0.12.1)."""
    nodes, edges = CortexStore(default_db_path(field_shape_repo)).fetch_graph(field_shape_repo)
    class_labels = {node.label for node in nodes if node.kind == "class" and node.granularity == "symbol"}
    in_repo = []
    for edge in edges:
        if edge.relation != "inherits":
            continue
        original = edge.metadata.get("unresolved_target") or edge.target
        label = original.split(":", 1)[1].split("::")[-1]
        if original.startswith("name:") and label in class_labels:
            in_repo.append(edge)
    assert len(in_repo) >= 10, "the profile must contain in-repo bases"
    resolved = [edge for edge in in_repo if edge.target.startswith("symbol:")]
    assert len(resolved) / len(in_repo) >= 0.8, (len(resolved), len(in_repo))


def test_status_reports_the_resolution_block(field_shape_repo):
    from cortex.mcp.tools import call_tool

    result = call_tool("cortex_status", {"repo_path": str(field_shape_repo), "response_format": "detailed"})
    payload = json.loads(result["content"][0]["text"])

    resolution = payload["resolution"]
    assert {"symbol", "name", "external"} <= set(resolution["calls"])
    assert {"symbol", "name", "module"} <= set(resolution["inherits"])
    assert resolution["inherits"]["symbol"] > 0 and resolution["calls"]["symbol"] > 0


# --- the resolver on hand-built graphs --------------------------------------------------------------

def cls(path: str, name: str, *, span=(1, 5)) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="class", label=name, source_ref=path, granularity="symbol",
        span_start=span[0], span_end=span[1],
    )


def inherits(path: str, derived: str, base: str, *, target: str | None = None, confidence="EXTRACTED") -> GraphEdge:
    return GraphEdge(
        edge_id=f"treesitter:{path}:inherits:{derived}:{base}", source=f"symbol:{path}:{derived}",
        target=target or f"name:{base}", relation="inherits", layer="STRUCTURAL", confidence=confidence,
        metadata={"lineno": 1, "source_file": path},
    )


def resolved(nodes, edges, **scope):
    return {e.edge_id: e for e in resolve_call_endpoints(nodes, edges, **scope) if e.relation == "inherits"}


def test_a_unique_base_becomes_its_symbol_and_keeps_the_label():
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:src/b.h:Base"
    assert out.metadata["unresolved_target"] == "name:Base"
    assert out.metadata["resolution"] == "label"
    assert out.confidence == "EXTRACTED" and "ambiguous" not in out.metadata


def test_the_inheritance_pass_runs_without_any_call_edge_and_in_a_scoped_pass_with_none_affected():
    """The first version returned early when the graph had no contract call."""
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    assert resolved(nodes, [edge])[edge.edge_id].target == "symbol:src/b.h:Base"

    edge = inherits("src/d.h", "Derived", "Base")
    out = resolved(nodes, [edge], affected_files={"src/d.h"}, changed_labels=set())[edge.edge_id]
    assert out.target == "symbol:src/b.h:Base"


def test_mocks_fixtures_other_languages_and_file_local_classes_lose_to_the_real_base():
    nodes = [
        cls("src/field/d.h", "Derived"),
        cls("src/field/coredevice.h", "CoreDevice"),
        cls("tests/mocks/mock_a/coredevice.h", "CoreDevice"),
        cls("tests/mocks/mock_b/coredevice.h", "CoreDevice"),
        cls("mocks/coredevice.h", "CoreDevice"),
        cls("src/field/unrelated.cpp", "CoreDevice"),
        cls("js/coredevice.js", "CoreDevice"),
    ]
    edge = inherits("src/field/d.h", "Derived", "CoreDevice")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:src/field/coredevice.h:CoreDevice"
    assert out.metadata["resolution"] == "preferred"


def test_a_test_class_may_derive_from_a_mock():
    nodes = [
        cls("tests/unit/t.cpp", "Probe"), cls("src/b.h", "Base"), cls("tests/mocks/base.h", "Base"),
    ]
    edge = inherits("tests/unit/t.cpp", "Probe", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target.startswith("name:")  # both stay candidates: a test may mean either
    assert out.metadata["ambiguous"] is True and len(out.metadata["candidates"]) == 2


def test_an_ambiguous_base_keeps_name_and_records_its_candidates_like_a_call():
    paths = [f"lib{i}/base.h" for i in range(MAX_CANDIDATES + 3)]
    nodes = [cls("src/d.h", "Derived"), *[cls(p, "Base") for p in paths]]
    edge = inherits("src/d.h", "Derived", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "name:Base"
    assert out.metadata["ambiguous"] is True
    assert out.metadata["candidates"] == sorted(f"symbol:{p}:Base" for p in paths)[:MAX_CANDIDATES]
    assert out.confidence == "LOW" and out.metadata["extracted_confidence"] == "EXTRACTED"
    assert "unresolved_target" not in out.metadata


def test_a_base_outside_the_repository_and_a_class_deriving_from_itself_stay_as_written():
    nodes = [cls("src/d.h", "Derived"), cls("src/q.h", "Widget")]
    external = inherits("src/d.h", "Derived", "QObject")
    own = inherits("src/q.h", "Widget", "Widget")  # `class Widget : public ns::Widget`, base elsewhere

    out = resolved(nodes, [external, own])

    assert out[external.edge_id].target == "name:QObject"
    assert out[own.edge_id].target == "name:Widget"


def test_resolution_is_per_language_family_and_module_targets_resolve_in_their_family():
    nodes = [cls("qml/View.qml", "View"), cls("qml/Item.qml", "Item"), cls("src/item.h", "Item")]
    edge = inherits("qml/View.qml", "View", "Item", target="module:Item")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:qml/Item.qml:Item"  # not the C++ class of the same label
    assert out.metadata["unresolved_target"] == "module:Item"


def test_resolving_twice_or_after_the_base_vanished_gives_what_a_fresh_resolution_gives():
    nodes = [cls("src/d.h", "Derived"), cls("lib1/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    resolved(nodes, [edge])
    resolved(nodes, [edge])
    assert (edge.target, edge.metadata["unresolved_target"]) == ("symbol:lib1/b.h:Base", "name:Base")

    gone = resolved([cls("src/d.h", "Derived")], [edge], affected_files=set(), changed_labels=set(), widen_typed=False)
    assert gone[edge.edge_id].target == "name:Base"  # the stored target vanished: back to the label
    assert "unresolved_target" not in edge.metadata and "resolution" not in edge.metadata

    resolved(nodes, [edge], changed_labels={"Base"})
    again = resolved([*nodes, cls("lib2/b.h", "Base")], [edge], changed_labels={"Base"})[edge.edge_id]
    assert again.metadata["ambiguous"] is True  # a new same-label class in the batch re-opens the choice
    assert again.target == "name:Base" and "unresolved_target" not in again.metadata


def test_an_unrelated_scoped_batch_leaves_resolved_inheritance_alone():
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    resolved(nodes, [edge])
    edge.metadata["marker"] = True  # a reset would drop this

    resolved(nodes, [edge], affected_files={"src/x.cpp"}, changed_labels={"Other"})

    assert edge.target == "symbol:src/b.h:Base" and edge.metadata["marker"] is True


# --- incremental ingest -------------------------------------------------------------------------------


def _inherit_targets(repo):
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    return {e.source: (e.target, e.metadata.get("unresolved_target")) for e in edges if e.relation == "inherits"}


FILES = {
    "src/base.h": "class Base {\npublic:\n    virtual void run();\n};\n",
    "src/derived.h": '#include "base.h"\nclass Derived : public Base {\npublic:\n    void run() override;\n};\n',
    "src/derived.cpp": '#include "derived.h"\nvoid Derived::run() { int x = 1; }\n',
}


def _commit(repo, message):
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", message], cwd=repo, check=True)


def _fresh_targets(repo, tmp_path, monkeypatch, name):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / name))
    ingest_repository(repo, incremental=False)
    return _inherit_targets(repo)


def test_renaming_the_base_class_does_not_leave_a_dangling_edge(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"] == ("symbol:src/base.h:Base", "name:Base")

    (repo / "src/base.h").write_text("class Foundation {\npublic:\n    virtual void run();\n};\n")
    _commit(repo, "rename")
    ingest_repository(repo)  # incremental

    incremental = _inherit_targets(repo)
    assert incremental["symbol:src/derived.h:Derived"] == ("name:Base", None)  # no class named Base any more
    assert incremental == _fresh_targets(repo, tmp_path, monkeypatch, "full-1")


def test_deleting_and_recreating_the_base_converges_with_a_full_ingest(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)

    (repo / "src/base.h").unlink()
    _commit(repo, "delete")
    ingest_repository(repo)
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"] == ("name:Base", None)

    (repo / "src/core").mkdir()
    (repo / "src/core/base.h").write_text("class Base {\npublic:\n    virtual void run();\n};\n")
    _commit(repo, "move")
    ingest_repository(repo)

    incremental = _inherit_targets(repo)
    assert incremental["symbol:src/derived.h:Derived"] == ("symbol:src/core/base.h:Base", "name:Base")
    assert incremental == _fresh_targets(repo, tmp_path, monkeypatch, "full-2")


def test_a_second_class_with_the_label_in_an_unrelated_file_reopens_then_closes_the_choice(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)

    (repo / "src/other").mkdir()
    (repo / "src/other/base.h").write_text("class Base {};\n")
    _commit(repo, "second Base")
    ingest_repository(repo)
    # the real base shares the derived class's directory: the choice stays unique
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"][0] == "symbol:src/base.h:Base"
    assert _inherit_targets(repo) == _fresh_targets(repo, tmp_path, monkeypatch, "full-3")

    (repo / "src/other/base.h").unlink()
    _commit(repo, "gone")
    ingest_repository(repo)
    assert _inherit_targets(repo) == _fresh_targets(repo, tmp_path, monkeypatch, "full-4")
