# tests/test_bundle_v2.py
from __future__ import annotations
import tempfile
from pathlib import Path
from cortex.models import GraphEdge, GraphNode, SourceRecord, CommitRecord
from cortex.store import CortexStore
from cortex.bundle import generate_bundle, _bfs_proximity, _build_adjacency, _tokenize_query
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


def _make_doc_heavy_store(tmp_path: Path, readme_tokens: int = 60) -> tuple[CortexStore, Path]:
    import subprocess
    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)

    # README contains every task term; bundle.py matches the task only via its filename.
    readme_content = 'bundle scoring rank nodes apply token budget overview\n' + ('filler words padding the document\n' * readme_tokens)
    code_content = 'def generate(items):\n    return sorted(items)\n'
    sources = [
        SourceRecord(path='README.md', content=readme_content, kind='markdown', size_bytes=len(readme_content), modified_at=0.0, content_hash='hd1'),
        SourceRecord(path='bundle.py', content=code_content, kind='code', size_bytes=len(code_content), modified_at=0.0, content_hash='hd2'),
    ]
    nodes = [
        GraphNode(node_id='file:README.md', kind='file', label='README.md', source_ref='README.md'),
        GraphNode(node_id='file:bundle.py', kind='file', label='bundle.py', source_ref='bundle.py'),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_filename_match_outranks_keyword_dense_doc(tmp_path):
    store, repo = _make_doc_heavy_store(tmp_path)
    result = generate_bundle(
        repo,
        task='how does bundle scoring rank nodes and apply the token budget',
        budget=4000,
        db_path=store.db_path,
        output_format='json',
    )
    scores = {item['path']: item['score'] for item in result['items']}
    assert 'bundle.py' in scores
    assert scores['bundle.py'] > scores.get('README.md', 0.0)


def test_symbol_name_match_gets_bonus(tmp_path):
    store, repo = _make_doc_heavy_store(tmp_path)
    # README name-drops the symbol alongside every task term; only bundle.py defines it.
    readme = 'where is generate implemented docs overview\n' + ('filler words padding the document\n' * 60)
    code = 'def generate(items):\n    return sorted(items)\n'
    store.save_sources(repo, [
        SourceRecord(path='README.md', content=readme, kind='markdown', size_bytes=len(readme), modified_at=0.0, content_hash='hs1'),
        SourceRecord(path='bundle.py', content=code, kind='code', size_bytes=len(code), modified_at=0.0, content_hash='hs2'),
    ])
    nodes = [
        GraphNode(node_id='file:README.md', kind='file', label='README.md', source_ref='README.md'),
        GraphNode(node_id='file:bundle.py', kind='file', label='bundle.py', source_ref='bundle.py'),
        GraphNode(node_id='sym:bundle.py:generate', kind='function', label='generate', source_ref='bundle.py', granularity='symbol'),
    ]
    store.save_graph(repo, nodes, [])
    result = generate_bundle(
        repo,
        task='where is generate implemented',
        budget=4000,
        db_path=store.db_path,
        output_format='json',
    )
    scores = {item['path']: item['score'] for item in result['items']}
    assert 'bundle.py' in scores
    assert scores['bundle.py'] > scores.get('README.md', 0.0)


def test_stopwords_subtokens_and_rarity_surface_matching_symbol(tmp_path):
    import subprocess
    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)

    target = '''
def _ensure_fresh(store, repo_root):
    status = detect_stale_index(store, repo_root)
    if status["stale"]:
        return auto_refreshed(status)
    return status
'''
    distractor = '''
def unrelated():
    return "fix the in a of for to and with from by auto"
'''
    sources = [
        SourceRecord(path='src/cortex/mcp/tools.py', content=target, kind='code', size_bytes=len(target), modified_at=0.0, content_hash='h0'),
        SourceRecord(path='src/cortex/cli.py', content=distractor, kind='code', size_bytes=len(distractor), modified_at=0.0, content_hash='h1'),
        SourceRecord(path='tests/test_watch.py', content=distractor, kind='code', size_bytes=len(distractor), modified_at=0.0, content_hash='h2'),
        SourceRecord(path='CHANGELOG.md', content='fix the stale in a of for to and with from by\n', kind='markdown', size_bytes=48, modified_at=0.0, content_hash='h3'),
    ]
    for i in range(8):
        content = f'def helper_{i}():\n    return "fix the in a of for to and with from by"\n'
        sources.append(SourceRecord(path=f'noise/noise_{i}.py', content=content, kind='code', size_bytes=len(content), modified_at=0.0, content_hash=f'n{i}'))
    nodes = [GraphNode(node_id=f'file:{source.path}', kind='file', label=source.path, source_ref=source.path) for source in sources]
    nodes.append(GraphNode(
        node_id='symbol:src/cortex/mcp/tools.py:_ensure_fresh',
        kind='function',
        label='_ensure_fresh',
        source_ref='src/cortex/mcp/tools.py',
        granularity='symbol',
        signature='def _ensure_fresh(store, repo_root):',
        span_start=2,
        span_end=6,
    ))
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])

    assert {'in', 'the', 'a'}.isdisjoint(_tokenize_query('fix the stale index detection in the auto refresh path'))
    result = generate_bundle(
        repo,
        task='fix the stale index detection in the auto refresh path',
        budget=4000,
        db_path=store.db_path,
        output_format='json',
    )

    assert result['items'][0]['path'] == 'src/cortex/mcp/tools.py'


def test_doc_tokens_capped_when_code_candidates_exist(tmp_path):
    store, repo = _make_doc_heavy_store(tmp_path, readme_tokens=400)
    budget = 300
    result = generate_bundle(
        repo,
        task='how does bundle scoring rank nodes and apply the token budget',
        budget=budget,
        db_path=store.db_path,
        output_format='json',
    )
    doc_tokens = sum(item['token_count'] for item in result['items'] if item['kind'] == 'markdown')
    code_paths = [item['path'] for item in result['items'] if item['kind'] == 'code']
    assert 'bundle.py' in code_paths
    assert doc_tokens <= int(budget * 0.4)


def test_doc_cap_not_applied_for_docs_only_repos(tmp_path):
    import subprocess
    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)
    content = 'setup guide install plugin\n' + ('more docs\n' * 50)
    store.reset_repo(repo)
    store.save_sources(repo, [
        SourceRecord(path='docs/setup.md', content=content, kind='markdown', size_bytes=len(content), modified_at=0.0, content_hash='do1'),
    ])
    store.save_commits(repo, [])
    store.save_graph(repo, [GraphNode(node_id='file:docs/setup.md', kind='file', label='docs/setup.md', source_ref='docs/setup.md')], [])

    result = generate_bundle(repo, task='setup guide install plugin', budget=4000, db_path=store.db_path, output_format='json')

    doc_tokens = sum(item['token_count'] for item in result['items'] if item['kind'] == 'markdown')
    assert doc_tokens > 0


def test_bundle_cli_rank_flag_defaults_to_pagerank_and_accepts_bfs():
    parser = build_parser()

    default_args = parser.parse_args(['bundle', '.', '--task', 'graph ranking'])
    bfs_args = parser.parse_args(['bundle', '.', '--task', 'graph ranking', '--rank', 'bfs'])

    assert default_args.rank == 'pagerank'
    assert bfs_args.rank == 'bfs'
