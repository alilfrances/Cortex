from __future__ import annotations

from cortex.community import detect_communities
from cortex.models import GraphEdge, GraphNode


def _node(node_id: str) -> GraphNode:
    return GraphNode(node_id=node_id, kind="file", label=node_id, source_ref=node_id, granularity="file")


def _edge(edge_id: str, source: str, target: str, relation: str, layer: str = "STRUCTURAL") -> GraphEdge:
    return GraphEdge(edge_id=edge_id, source=source, target=target, relation=relation, layer=layer, weight=1.0)


def test_calls_edges_do_not_change_communities():
    nodes = [_node(name) for name in ("a", "b", "c", "d", "e", "f")]
    base = [
        _edge("1", "a", "b", "imports"), _edge("2", "b", "c", "imports"),
        _edge("3", "d", "e", "imports"), _edge("4", "e", "f", "imports"),
    ]
    calls = [_edge(f"c{i}", s, t, "calls") for i, (s, t) in enumerate([("a", "d"), ("b", "e"), ("c", "f"), ("a", "f")])]

    def groups(edges):
        return sorted(sorted(c.node_ids) for c in detect_communities(nodes, edges))

    assert groups(base + calls) == groups(base)
