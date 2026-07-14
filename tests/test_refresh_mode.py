from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.cli import build_parser
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app.py").write_text("def run():\n    return 'ok'\n")
    (repo / "lib.py").write_text("def helper():\n    return 42\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_refresh_tool_falls_back_to_full_without_db(tmp_path):
    repo = _repo(tmp_path)

    first = _payload(call_tool("cortex_refresh", {"repo_path": str(repo)}))
    assert first["mode"] == "full"
    assert first["summary"]["new_files"] == 2


def test_refresh_tool_is_incremental_by_default(tmp_path):
    repo = _repo(tmp_path)

    first = _payload(call_tool("cortex_refresh", {"repo_path": str(repo)}))
    store = CortexStore(default_db_path(repo))
    _nodes, edges_before = store.fetch_graph(repo)

    second = _payload(call_tool("cortex_refresh", {"repo_path": str(repo)}))
    assert second["mode"] == "incremental"
    assert second["summary"]["updated_files"] == 0
    assert second["summary"]["new_files"] == 0

    _nodes, edges_after = store.fetch_graph(repo)
    assert len(edges_after) == len(edges_before)


def test_refresh_tool_mode_full_forces_full_reingest(tmp_path):
    repo = _repo(tmp_path)

    call_tool("cortex_refresh", {"repo_path": str(repo)})
    result = _payload(call_tool("cortex_refresh", {"repo_path": str(repo), "mode": "full"}))
    assert result["mode"] == "full"
    assert result["summary"]["new_files"] == 2


def test_cli_refresh_accepts_full_flag():
    parser = build_parser()
    args = parser.parse_args(["refresh", ".", "--full"])
    assert args.full is True
    args = parser.parse_args(["refresh", "."])
    assert args.full is False


def test_incremental_refresh_recomputes_degrees_and_resolves_new_member(tmp_path, monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "wiring.cpp").write_text(
        "void wire() {\n"
        "    connect(a, &Cls::member, b, &Other::slot);\n"
        "}\n"
    )
    (repo / "definition.cpp").write_text("void unrelated() {}\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    db_path = repo / ".cortex" / "cortex.db"

    ingest_repository(repo, commit_limit=0, db_path=db_path)
    (repo / "definition.cpp").write_text("void Cls::member() {}\n")
    ingest_repository(repo, commit_limit=0, db_path=db_path, incremental=True)

    store = CortexStore(db_path)
    incremental_nodes, incremental_edges = store.fetch_graph(repo)
    connect = next(edge for edge in incremental_edges if edge.relation == "connects")
    assert connect.source == "symbol:definition.cpp:member"
    incremental_degrees = {node.node_id: node.metadata.get("degree") for node in incremental_nodes}

    ingest_repository(repo, commit_limit=0, db_path=db_path, incremental=False)
    full_nodes, _full_edges = store.fetch_graph(repo)
    full_degrees = {node.node_id: node.metadata.get("degree") for node in full_nodes}

    assert incremental_degrees == full_degrees
