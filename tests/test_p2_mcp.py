from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cortex.impact import rank_file_impact
from cortex.ingest import compute_repo_fingerprint, ingest_repository
from cortex.mcp.tools import call_tool
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "multilang_repo"


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _relation_graph() -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes = [
        GraphNode("symbol:engine.hpp:Base", "class", "Base", "engine.hpp", granularity="symbol"),
        GraphNode("symbol:engine.cpp:Runner", "class", "Runner", "engine.cpp", granularity="symbol"),
        GraphNode("symbol:engine.cpp:Worker", "class", "Worker", "engine.cpp", granularity="symbol"),
        GraphNode("symbol:controller.hpp:Controller", "class", "Controller", "controller.hpp", granularity="symbol"),
        GraphNode("symbol:controller.hpp:started", "signal", "started", "controller.hpp", granularity="symbol"),
        GraphNode("symbol:controller.hpp:start", "slot", "start", "controller.hpp", granularity="symbol"),
        GraphNode("file:engine.cpp", "file", "engine.cpp", "engine.cpp"),
    ]
    edges = [
        GraphEdge("e1", "symbol:engine.cpp:Runner", "symbol:engine.hpp:Base", "inherits", layer="STRUCTURAL", weight=2.0),
        GraphEdge("e2", "symbol:engine.cpp:Worker", "symbol:engine.hpp:Base", "inherits", layer="STRUCTURAL", weight=1.5),
        GraphEdge("e3", "symbol:controller.hpp:Controller", "symbol:controller.hpp:started", "emits", layer="STRUCTURAL"),
        GraphEdge("e4", "symbol:controller.hpp:started", "symbol:controller.hpp:start", "connects", layer="STRUCTURAL"),
        GraphEdge("e5", "file:engine.cpp", "symbol:engine.cpp:Runner", "contains", layer="STRUCTURAL"),
        GraphEdge("e6", "symbol:engine.cpp:Runner", "name:ExternalBase", "inherits", layer="STRUCTURAL", confidence="LOW"),
    ]
    return nodes, edges


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
    assert {"cortex_query", "cortex_overview", "cortex_impact", "cortex_search_symbols", "cortex_relations", "cortex_refresh"} <= tool_names
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


def test_store_query_edges_filters_relation_direction_symbol_and_limit(tmp_path: Path) -> None:
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    nodes, edges = _relation_graph()
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    inherits = store.query_edges(repo, relation="inherits")
    outgoing_runner = store.query_edges(repo, relation="inherits", endpoint_substr="Runner", direction="out")
    incoming_base = store.query_edges(repo, relation="inherits", endpoint_substr="Base", direction="in")
    any_started = store.query_edges(repo, endpoint_substr="started", direction="both")
    limited = store.query_edges(repo, relation="inherits", limit=2)

    assert [edge.edge_id for edge in inherits] == ["e1", "e2", "e6"]
    assert [edge.edge_id for edge in outgoing_runner] == ["e1", "e6"]
    assert [edge.edge_id for edge in incoming_base] == ["e1", "e2", "e6"]
    assert [edge.edge_id for edge in any_started] == ["e3", "e4"]
    assert [edge.edge_id for edge in limited] == ["e1", "e2"]


def test_store_get_nodes_returns_requested_nodes_only(tmp_path: Path) -> None:
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    nodes, edges = _relation_graph()
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    result = store.get_nodes(repo, ["symbol:engine.cpp:Runner", "missing", "symbol:controller.hpp:started"])

    assert set(result) == {"symbol:engine.cpp:Runner", "symbol:controller.hpp:started"}
    assert result["symbol:engine.cpp:Runner"].label == "Runner"
    assert result["symbol:controller.hpp:started"].source_ref == "controller.hpp"


def test_cortex_relations_filters_resolves_and_limits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store = CortexStore(repo / ".cortex" / "cortex.db")
    nodes, edges = _relation_graph()
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    result = call_tool(
        "cortex_relations",
        {"repo_path": str(repo), "relation": "inherits", "symbol": "Runner", "direction": "out", "limit": 1},
    )
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["repo_path"] == str(repo)
    assert [item["relation"] for item in payload["items"]] == ["inherits"]
    assert payload["items"][0]["source"] == {"node_id": "symbol:engine.cpp:Runner", "label": "Runner", "path": "engine.cpp"}
    assert payload["items"][0]["target"] == {"node_id": "symbol:engine.hpp:Base", "label": "Base", "path": "engine.hpp"}
    assert "metadata" not in payload["items"][0]


def test_cortex_relations_direction_and_unresolved_endpoint_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store = CortexStore(repo / ".cortex" / "cortex.db")
    nodes, edges = _relation_graph()
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    incoming = _payload(call_tool("cortex_relations", {"repo_path": str(repo), "relation": "inherits", "symbol": "Base", "direction": "in"}))
    outgoing = _payload(call_tool("cortex_relations", {"repo_path": str(repo), "relation": "inherits", "symbol": "Base", "direction": "out"}))
    both = _payload(call_tool("cortex_relations", {"repo_path": str(repo), "relation": "inherits", "symbol": "ExternalBase", "direction": "both"}))

    assert [item["source"]["label"] for item in incoming["items"]] == ["Runner", "Worker", "Runner"]
    assert outgoing["items"] == []
    assert both["items"][0]["target"] == {"node_id": "name:ExternalBase", "label": None, "path": None}


def test_cortex_relations_missing_db_uses_existing_error_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    result = call_tool("cortex_relations", {"repo_path": str(repo), "relation": "inherits"})
    payload = _payload(result)

    assert result["isError"] is True
    assert payload["error"] == "missing_db"
    assert payload["repo_path"] == str(repo)


def test_cortex_relations_round_trips_existing_cpp_fixture(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "multilang_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    _git_init(repo)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixtures"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    result = call_tool(
        "cortex_relations",
        {"repo_path": str(repo), "relation": "contains", "symbol": "Runner", "direction": "in"},
    )
    payload = _payload(result)

    assert result["isError"] is False
    assert any(item["target"]["label"] == "Runner" and item["target"]["path"] == "engine.cpp" for item in payload["items"])
