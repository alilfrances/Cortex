# tests/test_bundle_v2.py
from __future__ import annotations
import tempfile
from pathlib import Path
from cortex.models import GraphEdge, GraphNode, SourceRecord, CommitRecord
from cortex.store import CortexStore
from cortex.bundle import generate_bundle, _bfs_proximity, _build_adjacency
from cortex.cli import build_parser


def test_build_adjacency_is_undirected():
    edges = [
        GraphEdge(edge_id='e1', source='file:a.py', target='file:b.py', relation='imports'),
    ]
    adj = _build_adjacency(edges)
    assert 'file:a.py' in adj
    assert 'file:b.py' in adj
    assert any(n == 'file:b.py' for n, _ in adj['file:a.py'])
    assert any(n == 'file:a.py' for n, _ in adj['file:b.py'])


def test_bfs_proximity_depth1_gets_higher_bonus_than_depth2():
    adj = {
        'seed': [('depth1', 1.0)],
        'depth1': [('depth2', 1.0)],
    }
    scores = _bfs_proximity({'seed'}, adj, max_depth=2)
    assert scores['depth1'] > scores.get('depth2', 0.0)


def test_bfs_proximity_seed_itself_not_in_scores():
    adj = {'seed': [('neighbor', 1.0)]}
    scores = _bfs_proximity({'seed'}, adj, max_depth=1)
    assert 'seed' not in scores


def _make_store_with_graph(tmp_path: Path) -> tuple[CortexStore, Path]:
    import subprocess
    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 't@t.com'], cwd=repo, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, capture_output=True)

    sources = [
        SourceRecord(path='auth.py', content='def login(): pass', kind='code', size_bytes=20, modified_at=0.0, content_hash='h1'),
        SourceRecord(path='session.py', content='def start_session(): pass', kind='code', size_bytes=25, modified_at=0.0, content_hash='h2'),
        SourceRecord(path='unrelated.py', content='def compute_pi(): pass', kind='code', size_bytes=22, modified_at=0.0, content_hash='h3'),
    ]
    nodes = [
        GraphNode(node_id='file:auth.py', kind='file', label='auth.py', source_ref='auth.py'),
        GraphNode(node_id='file:session.py', kind='file', label='session.py', source_ref='session.py'),
        GraphNode(node_id='file:unrelated.py', kind='file', label='unrelated.py', source_ref='unrelated.py'),
    ]
    edges = [
        GraphEdge(edge_id='e1', source='file:auth.py', target='file:session.py', relation='imports', layer='STRUCTURAL', confidence='EXTRACTED', weight=1.0),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, edges)
    return store, repo


def test_graph_neighbor_included_when_seed_matches(tmp_path):
    store, repo = _make_store_with_graph(tmp_path)
    result = generate_bundle(repo, task='login', budget=2000, db_path=store.db_path)
    assert 'session.py' in result
    assert 'unrelated.py' not in result


def test_pagerank_surfaces_three_hop_relevant_node_that_bfs_depth_two_misses(tmp_path):
    import subprocess

    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)

    sources = [
        SourceRecord(path='seed.py', content='graph ranking seed\ndef graph_ranking_seed(): pass', kind='code', size_bytes=30, modified_at=0.0, content_hash='h1'),
        SourceRecord(path='hop1.py', content='def hop_one(): pass', kind='code', size_bytes=20, modified_at=0.0, content_hash='h2'),
        SourceRecord(path='hop2.py', content='def hop_two(): pass', kind='code', size_bytes=20, modified_at=0.0, content_hash='h3'),
        SourceRecord(path='hop3.py', content='def hop_three(): pass', kind='code', size_bytes=20, modified_at=0.0, content_hash='h4'),
    ]
    nodes = [
        GraphNode(node_id=f'file:{source.path}', kind='file', label=source.path, source_ref=source.path)
        for source in sources
    ]
    edges = [
        GraphEdge(edge_id='e1', source='file:seed.py', target='file:hop1.py', relation='imports', layer='STRUCTURAL'),
        GraphEdge(edge_id='e2', source='file:hop1.py', target='file:hop2.py', relation='imports', layer='STRUCTURAL'),
        GraphEdge(edge_id='e3', source='file:hop2.py', target='file:hop3.py', relation='imports', layer='STRUCTURAL'),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, edges)

    pagerank_result = generate_bundle(repo, task='graph ranking', budget=2000, db_path=store.db_path, rank='pagerank')
    bfs_result = generate_bundle(repo, task='graph ranking', budget=2000, db_path=store.db_path, rank='bfs')

    assert 'hop3.py' in pagerank_result
    assert 'hop3.py' not in bfs_result


def test_bundle_cli_rank_flag_defaults_to_pagerank_and_accepts_bfs():
    parser = build_parser()

    default_args = parser.parse_args(['bundle', '.', '--task', 'graph ranking'])
    bfs_args = parser.parse_args(['bundle', '.', '--task', 'graph ranking', '--rank', 'bfs'])

    assert default_args.rank == 'pagerank'
    assert bfs_args.rank == 'bfs'
