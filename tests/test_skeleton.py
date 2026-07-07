# tests/test_skeleton.py
from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.ast_extract import extract_python_edges
from cortex.bundle import SKELETON_MARKER, _render_skeleton, generate_bundle
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore

_FUNC_BODY = '\n'.join(f'    value_{i} = {i}' for i in range(200))
_METHOD_BODY = '\n'.join(f'        value_{i} = {i}' for i in range(200))
BIG_SOURCE = f'''"""login helpers"""
import os

class LoginService:
    def authenticate(self, token: str) -> bool:
{_METHOD_BODY}
        return True

def login_handler(request: str) -> str:
{_FUNC_BODY}
    return request
'''


def _symbols_for(path: str, content: str) -> list[GraphNode]:
    nodes, _ = extract_python_edges(path, content, known_paths=set())
    return nodes


def _make_repo(tmp_path: Path, sources: list[SourceRecord]) -> tuple[CortexStore, Path]:
    db_path = tmp_path / 'cortex.db'
    store = CortexStore(db_path)
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, capture_output=True)
    nodes: list[GraphNode] = []
    for source in sources:
        nodes.append(GraphNode(node_id=f'file:{source.path}', kind='file', label=source.path, source_ref=source.path))
        if source.path.endswith('.py'):
            nodes.extend(_symbols_for(source.path, source.content))
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_render_skeleton_signatures_only():
    symbols = _symbols_for('auth.py', BIG_SOURCE)
    skeleton = _render_skeleton(BIG_SOURCE, symbols, set())
    assert skeleton.startswith(SKELETON_MARKER)
    assert 'import os' in skeleton
    assert 'class LoginService:' in skeleton
    assert '    def authenticate(self, token: str) -> bool:' in skeleton
    assert 'def login_handler(request: str) -> str:' in skeleton
    assert '...' in skeleton
    assert 'value_0 = 0' not in skeleton


def test_render_skeleton_inlines_selected_body():
    symbols = _symbols_for('auth.py', BIG_SOURCE)
    handler_id = 'symbol:auth.py:login_handler'
    skeleton = _render_skeleton(BIG_SOURCE, symbols, {handler_id})
    assert 'value_0 = 0' in skeleton
    assert 'class LoginService:' in skeleton


def test_tight_budget_produces_skeleton_item(tmp_path):
    source = SourceRecord(path='auth.py', content=BIG_SOURCE, kind='code', size_bytes=len(BIG_SOURCE), modified_at=0.0, content_hash='h1')
    store, repo = _make_repo(tmp_path, [source])
    bundle = generate_bundle(repo, task='login', budget=200, db_path=store.db_path, output_format='json')
    items = [item for item in bundle['items'] if item['path'] == 'auth.py']
    assert items, 'expected auth.py in bundle'
    item = items[0]
    assert item['metadata'].get('skeleton') is True
    assert item['content'].startswith(SKELETON_MARKER)
    assert item['metadata']['content_hash']
    assert item['metadata']['elided_spans']
    assert item['token_count'] <= 200


def test_loose_budget_keeps_full_content(tmp_path):
    source = SourceRecord(path='auth.py', content=BIG_SOURCE, kind='code', size_bytes=len(BIG_SOURCE), modified_at=0.0, content_hash='h1')
    store, repo = _make_repo(tmp_path, [source])
    bundle = generate_bundle(repo, task='login', budget=100000, db_path=store.db_path, output_format='json')
    item = [i for i in bundle['items'] if i['path'] == 'auth.py'][0]
    assert 'skeleton' not in item['metadata']
    assert item['content'] == BIG_SOURCE


def test_oversized_non_python_still_truncates(tmp_path):
    big_text = 'login docs\n' + ('lorem ipsum dolor sit amet\n' * 400)
    source = SourceRecord(path='guide.md', content=big_text, kind='markdown', size_bytes=len(big_text), modified_at=0.0, content_hash='h1')
    store, repo = _make_repo(tmp_path, [source])
    bundle = generate_bundle(repo, task='login docs', budget=200, db_path=store.db_path, output_format='json')
    item = [i for i in bundle['items'] if i['path'] == 'guide.md'][0]
    assert item['metadata'].get('truncated') is True
    assert 'skeleton' not in item['metadata']


def test_tight_budget_uses_symbol_skeleton_for_non_python_source(tmp_path):
    body = '\n'.join(f'    value += {i};' for i in range(200))
    content = f'''#include "engine.hpp"

namespace Engine {{
class Runner {{
public:
    void start() {{
{body}
    }}
}};
}}
'''
    source = SourceRecord(path='engine.cpp', content=content, kind='code', size_bytes=len(content), modified_at=0.0, content_hash='cpp1')
    store, repo = _make_repo(tmp_path, [source])
    store.save_graph(repo, [
        GraphNode(node_id='file:engine.cpp', kind='file', label='engine.cpp', source_ref='engine.cpp'),
        GraphNode(
            node_id='symbol:engine.cpp:Runner',
            kind='class',
            label='Runner',
            source_ref='engine.cpp',
            granularity='symbol',
            signature='class Runner {',
            span_start=4,
            span_end=len(content.splitlines()) - 1,
        ),
    ], [])

    bundle = generate_bundle(repo, task='Runner start engine', budget=80, db_path=store.db_path, output_format='json')
    item = [i for i in bundle['items'] if i['path'] == 'engine.cpp'][0]

    assert item['metadata'].get('skeleton') is True
    assert 'class Runner {' in item['content']
    assert '[body elided]' in item['content']
    assert 'value += 0' not in item['content']
