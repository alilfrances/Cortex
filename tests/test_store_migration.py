# tests/test_store_migration.py
from __future__ import annotations

import sqlite3
from pathlib import Path

from cortex.models import GraphNode
from cortex.store import CortexStore


def test_old_db_without_symbol_columns_migrates_and_round_trips(tmp_path):
    db_path = tmp_path / 'cortex.db'
    conn = sqlite3.connect(db_path)
    conn.execute(
        '''
        CREATE TABLE graph_nodes (
            repo_path TEXT NOT NULL,
            node_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            label TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            PRIMARY KEY (repo_path, node_id)
        )
        '''
    )
    conn.execute(
        "INSERT INTO graph_nodes VALUES(?, ?, ?, ?, ?, ?)",
        (str(tmp_path.resolve()), 'file:a.py', 'file', 'a.py', 'a.py', '{}'),
    )
    conn.commit()
    conn.close()

    store = CortexStore(db_path)
    symbol = GraphNode(
        node_id='symbol:a.py:run',
        kind='func',
        label='run',
        source_ref='a.py',
        granularity='symbol',
        signature='def run() -> None:',
        span_start=3,
        span_end=5,
    )
    store.save_graph(tmp_path, [symbol], [])
    nodes, _ = store.fetch_graph(tmp_path)
    by_id = {n.node_id: n for n in nodes}

    old_node = by_id['file:a.py']
    assert old_node.granularity == 'file'
    assert old_node.signature == ''
    assert old_node.span_start is None

    new_node = by_id['symbol:a.py:run']
    assert new_node.granularity == 'symbol'
    assert new_node.signature == 'def run() -> None:'
    assert new_node.span_start == 3
    assert new_node.span_end == 5
