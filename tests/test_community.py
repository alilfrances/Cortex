# tests/test_community.py
from __future__ import annotations
from cortex.models import GraphEdge, GraphNode
from cortex.community import detect_communities


def _node(nid: str) -> GraphNode:
    return GraphNode(node_id=nid, kind='file', label=nid, source_ref=nid)


def _edge(src: str, tgt: str) -> GraphEdge:
    return GraphEdge(edge_id=f'{src}:{tgt}', source=src, target=tgt, relation='imports')


def test_isolated_nodes_each_get_own_community():
    nodes = [_node('a'), _node('b'), _node('c')]
    edges = []
    communities = detect_communities(nodes, edges)
    assert len(communities) == 3


def test_connected_cluster_merges_into_one_community():
    nodes = [_node('a'), _node('b'), _node('c')]
    edges = [_edge('a', 'b'), _edge('b', 'c'), _edge('a', 'c')]
    communities = detect_communities(nodes, edges)
    assert len(communities) == 1
    assert len(communities[0].node_ids) == 3


def test_two_disconnected_clusters():
    nodes = [_node('a'), _node('b'), _node('c'), _node('x'), _node('y')]
    edges = [_edge('a', 'b'), _edge('b', 'c'), _edge('x', 'y')]
    communities = detect_communities(nodes, edges)
    sizes = sorted(len(c.node_ids) for c in communities)
    assert sizes == [2, 3]


def test_empty_input_returns_empty():
    assert detect_communities([], []) == []


def test_community_ids_are_unique():
    nodes = [_node(str(i)) for i in range(10)]
    edges = [_edge(str(i), str(i + 1)) for i in range(5)]
    communities = detect_communities(nodes, edges)
    ids = [c.community_id for c in communities]
    assert len(ids) == len(set(ids))


def test_heading_contains_edges_do_not_merge_communities():
    nodes = [_node('file:doc.md'), _node('section:doc.md:1'), _node('file:other.md')]
    edges = [
        GraphEdge(edge_id='h1', source='file:doc.md', target='section:doc.md:1', relation='contains', layer='HEADING'),
        GraphEdge(edge_id='h2', source='section:doc.md:1', target='file:other.md', relation='contains', layer='HEADING'),
    ]

    communities = detect_communities(nodes, edges)

    assert len(communities) == 3


def test_weak_cochange_edges_do_not_collapse_independent_clusters():
    nodes = [_node('a'), _node('b'), _node('x'), _node('y')]
    edges = [
        GraphEdge(edge_id='ab', source='a', target='b', relation='imports', layer='STRUCTURAL', weight=1.0),
        GraphEdge(edge_id='xy', source='x', target='y', relation='imports', layer='STRUCTURAL', weight=1.0),
        GraphEdge(edge_id='weak', source='b', target='x', relation='cochange', layer='COCHANGE', weight=0.05),
    ]

    communities = detect_communities(nodes, edges)
    sizes = sorted(len(c.node_ids) for c in communities)

    assert sizes == [2, 2]


def test_high_fanout_commit_touch_edges_do_not_collapse_file_communities():
    file_ids = [f'file:cluster_{i}.py' for i in range(12)]
    nodes = [
        GraphNode(node_id='commit:wide', kind='commit', label='wide commit', source_ref='commit:wide'),
        *[_node(file_id) for file_id in file_ids],
    ]
    edges = [
        GraphEdge(
            edge_id=f'touch-{index}',
            source='commit:wide',
            target=file_id,
            relation='touches',
            layer='COCHANGE',
            weight=1.0,
        )
        for index, file_id in enumerate(file_ids)
    ]

    communities = detect_communities(nodes, edges)
    file_community_ids = {
        community.community_id
        for community in communities
        if any(node_id in file_ids for node_id in community.node_ids)
    }

    assert len(file_community_ids) > 1
