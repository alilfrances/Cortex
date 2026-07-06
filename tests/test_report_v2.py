from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.models import GraphEdge, GraphNode
from cortex.report import generate_report
from cortex.store import CortexStore


def _setup_store(tmp_path: Path) -> tuple[CortexStore, Path]:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)

    nodes = [
        GraphNode(node_id="file:auth.py", kind="file", label="auth.py", source_ref="auth.py"),
        GraphNode(node_id="file:session.py", kind="file", label="session.py", source_ref="session.py"),
        GraphNode(node_id="file:db.py", kind="file", label="db.py", source_ref="db.py"),
    ]
    edges = [
        GraphEdge(edge_id="e1", source="file:auth.py", target="file:session.py", relation="imports", layer="STRUCTURAL"),
        GraphEdge(edge_id="e2", source="file:auth.py", target="file:db.py", relation="imports", layer="STRUCTURAL"),
        GraphEdge(edge_id="e3", source="file:session.py", target="file:db.py", relation="cochange", layer="COCHANGE"),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, [])
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, edges)
    return store, repo


def test_report_contains_god_nodes_section(tmp_path: Path) -> None:
    store, repo = _setup_store(tmp_path)

    report = generate_report(repo, db_path=store.db_path)

    assert "## God Nodes" in report
    assert "`auth.py`" in report


def test_report_contains_communities_section(tmp_path: Path) -> None:
    store, repo = _setup_store(tmp_path)

    report = generate_report(repo, db_path=store.db_path)

    assert "## Communities" in report
    assert "Community" in report


def test_report_contains_node_edge_and_community_counts(tmp_path: Path) -> None:
    store, repo = _setup_store(tmp_path)

    report = generate_report(repo, db_path=store.db_path)

    assert "- Total Nodes: 3" in report
    assert "- Edges: 3" in report
    assert "- Communities:" in report


def test_report_mentions_surprising_cross_community_connections(tmp_path: Path) -> None:
    store, repo = _setup_store(tmp_path)

    report = generate_report(repo, db_path=store.db_path)

    assert "## Surprising Cross-Community Connections" in report


def test_report_filters_src_test_cochange_pairs_by_default(tmp_path: Path) -> None:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    nodes = [
        GraphNode(node_id="file:src/auth.py", kind="file", label="src/auth.py", source_ref="src/auth.py"),
        GraphNode(node_id="file:tests/test_auth.py", kind="file", label="tests/test_auth.py", source_ref="tests/test_auth.py"),
    ]
    edges = [
        GraphEdge(
            edge_id="co",
            source="file:src/auth.py",
            target="file:tests/test_auth.py",
            relation="cochange",
            layer="COCHANGE",
            weight=0.05,
        )
    ]
    store.reset_repo(repo)
    store.save_sources(repo, [])
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, edges)

    report = generate_report(repo, db_path=store.db_path)
    report_with_tests = generate_report(repo, db_path=store.db_path, include_test_pairs=True)

    assert "`src/auth.py` ↔ `tests/test_auth.py`" not in report
    assert "`src/auth.py` ↔ `tests/test_auth.py`" in report_with_tests
