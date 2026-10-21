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


# --- collapse_to_file_graph (0.12) ---


def _sym(path: str, name: str) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="func", label=name, source_ref=path, granularity="symbol"
    )


def _edge(edge_id: str, source: str, target: str, layer: str = "STRUCTURAL", weight: float = 1.0) -> GraphEdge:
    return GraphEdge(
        edge_id=edge_id, source=source, target=target, relation="calls", layer=layer, weight=weight
    )


def _files_and_symbols():
    nodes = [
        _node("file:a.py"), _node("file:b.py"), _node("module:os", kind="module"),
        _sym("a.py", "f"), _sym("a.py", "g"), _sym("b.py", "h"),
        _node("commit:abc", kind="commit"),
    ]
    edges = [
        _edge("1", "symbol:a.py:f", "symbol:b.py:h", weight=1.0),
        _edge("2", "symbol:a.py:g", "symbol:b.py:h", weight=2.0),
        _edge("3", "symbol:a.py:f", "symbol:a.py:g"),            # self-loop once collapsed
        _edge("4", "file:a.py", "module:os"),
        _edge("5", "file:a.py", "commit:abc"),                   # commit nodes are omitted
        _edge("6", "file:a.py", "symbol:b.py:h", layer="COCHANGE", weight=0.5),
        _edge("7", "symbol:a.py:f", "file:ghost.py"),            # unknown endpoint
    ]
    return nodes, edges


def test_collapse_merges_parallel_edges_and_drops_self_loops_and_unknown_endpoints() -> None:
    from cortex.rank import collapse_to_file_graph

    nodes, edges = collapse_to_file_graph(*_files_and_symbols())

    assert [n.node_id for n in nodes] == ["file:a.py", "file:b.py", "module:os"]
    assert [(e.source, e.target, e.layer, e.weight) for e in edges] == [
        ("file:a.py", "file:b.py", "COCHANGE", 0.5),
        ("file:a.py", "file:b.py", "STRUCTURAL", 3.0),
        ("file:a.py", "module:os", "STRUCTURAL", 1.0),
    ]


def test_collapse_is_deterministic_and_does_not_change_ranking_of_a_simple_chain() -> None:
    from cortex.rank import collapse_to_file_graph

    first = collapse_to_file_graph(*_files_and_symbols())
    second = collapse_to_file_graph(*reversed(_files_and_symbols()[::-1]))
    assert [e.to_dict() for e in first[1]] == [e.to_dict() for e in second[1]]

    ranks = personalized_pagerank(*first, {"file:a.py": 1.0})
    again = personalized_pagerank(*collapse_to_file_graph(*_files_and_symbols()), {"file:a.py": 1.0})
    assert ranks == again
    assert ranks["file:b.py"] > 0
