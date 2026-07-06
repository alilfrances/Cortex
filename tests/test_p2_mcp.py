from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from cortex.impact import rank_file_impact
from cortex.ingest import compute_repo_fingerprint
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)


def test_mcp_stdio_roundtrip_outputs_only_json_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app.py").write_text("def run():\n    return 'ok'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)

    env = {**os.environ, "PYTHONPATH": str(Path.cwd() / "src")}
    process = subprocess.Popen(
        [sys.executable, "-m", "cortex.mcp.server"],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cortex_refresh", "arguments": {}}},
    ]
    input_text = "".join(json.dumps(frame) + "\n" for frame in frames)
    stdout, stderr = process.communicate(input=input_text, timeout=10)

    assert process.returncode == 0, stderr
    lines = [line for line in stdout.splitlines() if line]
    decoded = [json.loads(line) for line in lines]
    assert [frame["id"] for frame in decoded] == [1, 2, 3]
    tool_names = {tool["name"] for tool in decoded[1]["result"]["tools"]}
    assert {"cortex_query", "cortex_overview", "cortex_impact", "cortex_search_symbols", "cortex_refresh"} <= tool_names
    refresh = decoded[2]["result"]
    assert refresh["content"][0]["type"] == "text"
    assert refresh["isError"] is False


def test_rank_file_impact_prefers_heavier_cochange_and_structural_edges() -> None:
    nodes = [
        GraphNode("file:app.py", "file", "app.py", "app.py"),
        GraphNode("file:db.py", "file", "db.py", "db.py"),
        GraphNode("file:ui.py", "file", "ui.py", "ui.py"),
        GraphNode("symbol:app.py:run", "function", "run", "app.py", granularity="symbol"),
    ]
    edges = [
        GraphEdge("e1", "file:app.py", "file:db.py", "cochange", layer="COCHANGE", weight=4.0),
        GraphEdge("e2", "file:app.py", "file:ui.py", "imports", layer="STRUCTURAL", weight=2.0),
        GraphEdge("e3", "file:app.py", "symbol:app.py:run", "contains", layer="STRUCTURAL", weight=9.0),
    ]

    impact = rank_file_impact("app.py", nodes, edges)

    assert [item["path"] for item in impact] == ["db.py", "ui.py"]
    assert impact[0]["why"] == [{"edge_id": "e1", "layer": "COCHANGE", "relation": "cochange", "weight": 4.0}]
    assert impact[1]["score"] == 2.0


def test_compute_repo_fingerprint_includes_path_size_and_mtime(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("print('bb')\n", encoding="utf-8")

    first = compute_repo_fingerprint(tmp_path)
    os.utime(tmp_path / "a.py", (1_700_000_000, 1_700_000_000))
    second = compute_repo_fingerprint(tmp_path)
    (tmp_path / "renamed.py").write_text((tmp_path / "b.py").read_text(encoding="utf-8"), encoding="utf-8")
    third = compute_repo_fingerprint(tmp_path)

    assert first != second
    assert second != third
    assert len(first) == 64


def test_store_search_nodes_matches_label_signature_and_source_ref(tmp_path: Path) -> None:
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    nodes = [
        GraphNode("file:src/app.py", "file", "src/app.py", "src/app.py"),
        GraphNode(
            "symbol:src/app.py:LoginService",
            "class",
            "LoginService",
            "src/app.py",
            granularity="symbol",
            signature="class LoginService:",
        ),
        GraphNode("symbol:src/payments.py:charge", "function", "charge", "src/payments.py", granularity="symbol"),
    ]
    store.reset_repo(repo)
    store.save_graph(repo, nodes, [])

    by_label = store.search_nodes(repo, "login")
    by_source = store.search_nodes(repo, "payments")

    assert [node.node_id for node in by_label] == ["symbol:src/app.py:LoginService"]
    assert [node.node_id for node in by_source] == ["symbol:src/payments.py:charge"]
