from __future__ import annotations

from cortex.models import GraphEdge, GraphNode
from cortex.rank import personalized_pagerank


def _node(node_id: str, kind: str = "file") -> GraphNode:
    return GraphNode(node_id=node_id, kind=kind, label=node_id, source_ref=node_id)


def test_pagerank_orders_weighted_structural_path_above_weak_heading_path() -> None:
    nodes = [_node("seed"), _node("structural"), _node("heading")]
    edges = [
        GraphEdge(
            edge_id="s",
            source="seed",
            target="structural",
            relation="imports",
            layer="STRUCTURAL",
            weight=1.0,
        ),
        GraphEdge(
            edge_id="h",
            source="seed",
            target="heading",
            relation="contains",
            layer="HEADING",
            weight=1.0,
        ),
    ]

    ranks = personalized_pagerank(nodes, edges, {"seed": 1.0})

    assert ranks["structural"] > ranks["heading"]


def test_pagerank_dangling_nodes_redistribute_to_personalization_vector() -> None:
    nodes = [_node("seed"), _node("dangling")]
    edges: list[GraphEdge] = []

    ranks = personalized_pagerank(nodes, edges, {"seed": 1.0})

    assert ranks["seed"] > ranks["dangling"]
    assert abs(sum(ranks.values()) - 1.0) < 1e-9


def test_pagerank_handles_empty_and_single_node_graphs() -> None:
    assert personalized_pagerank([], [], {}) == {}

    ranks = personalized_pagerank([_node("only")], [], {"only": 3.0})

    assert ranks == {"only": 1.0}


def test_pagerank_excludes_commit_nodes_from_walk_and_output() -> None:
    nodes = [_node("file:a.py"), _node("commit:abc", kind="commit"), _node("file:b.py")]
    edges = [
        GraphEdge(edge_id="e1", source="file:a.py", target="commit:abc", relation="touches", layer="COCHANGE"),
        GraphEdge(edge_id="e2", source="commit:abc", target="file:b.py", relation="touches", layer="COCHANGE"),
    ]

    ranks = personalized_pagerank(nodes, edges, {"file:a.py": 1.0})

    assert "commit:abc" not in ranks
    assert ranks["file:a.py"] > ranks["file:b.py"]
