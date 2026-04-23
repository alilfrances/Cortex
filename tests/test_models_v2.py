from __future__ import annotations
from cortex.models import GraphEdge, Community, SourceRecord


def test_graph_edge_has_layer_and_confidence():
    edge = GraphEdge(
        edge_id='e1',
        source='file:a.py',
        target='file:b.py',
        relation='imports',
        layer='STRUCTURAL',
        confidence='EXTRACTED',
    )
    assert edge.layer == 'STRUCTURAL'
    assert edge.confidence == 'EXTRACTED'
    assert edge.weight == 1.0


def test_graph_edge_defaults():
    edge = GraphEdge(
        edge_id='e2',
        source='file:a.py',
        target='section:a.py:1',
        relation='contains',
    )
    assert edge.layer == 'HEADING'
    assert edge.confidence == 'EXTRACTED'
    assert edge.weight == 1.0


def test_community_dataclass():
    c = Community(community_id=0, node_ids=['file:a.py', 'file:b.py'])
    assert c.community_id == 0
    assert len(c.node_ids) == 2
    assert c.label == ''


def test_source_record_has_content_hash():
    s = SourceRecord(
        path='a.py',
        content='x = 1',
        kind='code',
        size_bytes=5,
        modified_at=0.0,
        content_hash='abc123',
    )
    assert s.content_hash == 'abc123'
