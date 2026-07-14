from __future__ import annotations

from pathlib import Path

from cortex.graph import build_graph
from cortex.models import GraphEdge, GraphNode, SourceRecord
from cortex.store import CortexStore


def _symbol(path: str, name: str, kind: str = "func", degree: int = 0) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind=kind,
        label=name,
        source_ref=path,
        granularity="symbol",
        signature=f"void {name}()",
        span_start=1,
        span_end=2,
        metadata={"degree": degree},
    )


def test_search_ranks_high_degree_symbol_first(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    nodes = [
        _symbol("a.cpp", "refresh", degree=1),
        _symbol("b.cpp", "refresh", degree=5),
        _symbol("c.cpp", "refresh", degree=0),
    ]
    edges = [
        GraphEdge(
            edge_id=f"e{i}",
            source="symbol:b.cpp:refresh",
            target=f"file:n{i}.cpp",
            relation="calls",
            layer="STRUCTURAL",
        )
        for i in range(5)
    ]
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    results = store.search_nodes(repo, "refresh", limit=10)
    assert [node.node_id for node in results][:3] == [
        "symbol:b.cpp:refresh",
        "symbol:a.cpp:refresh",
        "symbol:c.cpp:refresh",
    ]


def test_search_prefers_class_over_func_within_tier(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    nodes = [
        _symbol("a.cpp", "Widget", kind="func", degree=3),
        _symbol("b.cpp", "Widget", kind="class", degree=0),
    ]
    store.reset_repo(repo)
    store.save_graph(repo, nodes, [])

    results = store.search_nodes(repo, "Widget", limit=10)
    assert results[0].node_id == "symbol:b.cpp:Widget"


def test_build_graph_stores_degree_in_node_metadata():
    source = SourceRecord(
        path="mod.py",
        content="def called():\n    pass\n\n\ndef caller():\n    called()\n",
        kind="code",
        size_bytes=10,
        modified_at=0.0,
        content_hash="h",
    )
    nodes, edges = build_graph([source], [])

    by_id = {node.node_id: node for node in nodes}
    for node in nodes:
        assert "degree" in node.metadata

    file_node = by_id["file:mod.py"]
    expected = sum(
        1 for edge in edges for endpoint in (edge.source, edge.target) if endpoint == "file:mod.py"
    )
    assert file_node.metadata["degree"] == expected
    assert expected > 0
