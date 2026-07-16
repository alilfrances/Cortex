from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.mcp.tools import TOOL_DEFINITIONS, call_tool
from cortex.models import GraphEdge, GraphNode
from cortex.pathfind import shortest_paths
from cortex.store import CortexStore


def _file(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path)


def _symbol(path: str, name: str, kind: str = "func") -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind=kind,
        label=name,
        source_ref=path,
        granularity="symbol",
        span_start=1,
        span_end=2,
    )


def _edge(edge_id: str, source: str, target: str, relation: str, layer: str = "STRUCTURAL") -> GraphEdge:
    return GraphEdge(edge_id=edge_id, source=source, target=target, relation=relation, layer=layer)


def _cross_file_graph() -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes = [
        _file("a.cpp"),
        _file("b.cpp"),
        _symbol("a.cpp", "Alpha", kind="class"),
        _symbol("a.cpp", "changed"),
        _symbol("b.cpp", "onChanged"),
    ]
    edges = [
        _edge("regex:a.cpp:contains:Alpha", "file:a.cpp", "symbol:a.cpp:Alpha", "contains"),
        _edge("regex:a.cpp:contains:changed", "file:a.cpp", "symbol:a.cpp:changed", "contains"),
        _edge("regex:a.cpp:connects:1", "symbol:a.cpp:changed", "symbol:b.cpp:onChanged", "connects"),
    ]
    return nodes, edges


def test_shortest_paths_crosses_contains_and_connects():
    nodes, edges = _cross_file_graph()
    paths = shortest_paths(nodes, edges, "symbol:a.cpp:Alpha", "symbol:b.cpp:onChanged")

    assert len(paths) == 1
    hops = paths[0]
    assert [hop["node"] for hop in hops] == ["file:a.cpp", "symbol:a.cpp:changed", "symbol:b.cpp:onChanged"]
    assert [hop["relation"] for hop in hops] == ["contains", "contains", "connects"]
    assert [hop["direction"] for hop in hops] == ["in", "out", "out"]


def test_shortest_paths_excludes_cochange_and_commit_nodes():
    nodes, edges = _cross_file_graph()
    nodes.append(GraphNode(node_id="commit:abc", kind="commit", label="msg", source_ref="abc"))
    # Shortcuts that must not be used: a cochange edge and a commit hub.
    edges.append(_edge("cochange:a.cpp:b.cpp", "file:a.cpp", "file:b.cpp", "cochange", layer="COCHANGE"))
    edges.append(_edge("edge:abc:a", "commit:abc", "symbol:a.cpp:Alpha", "touches"))
    edges.append(_edge("edge:abc:b", "commit:abc", "symbol:b.cpp:onChanged", "touches"))

    paths = shortest_paths(nodes, edges, "symbol:a.cpp:Alpha", "symbol:b.cpp:onChanged")
    assert len(paths) == 1
    assert [hop["relation"] for hop in paths[0]] == ["contains", "contains", "connects"]


def test_shortest_paths_respects_max_depth_and_max_paths():
    nodes, edges = _cross_file_graph()
    assert shortest_paths(nodes, edges, "symbol:a.cpp:Alpha", "symbol:b.cpp:onChanged", max_depth=2) == []

    # Two parallel two-hop routes; max_paths=1 keeps only one.
    nodes2 = [_file("x"), _file("m1"), _file("m2"), _file("y")]
    edges2 = [
        _edge("e1", "file:x", "file:m1", "imports"),
        _edge("e2", "file:m1", "file:y", "imports"),
        _edge("e3", "file:x", "file:m2", "imports"),
        _edge("e4", "file:m2", "file:y", "imports"),
    ]
    all_paths = shortest_paths(nodes2, edges2, "file:x", "file:y")
    assert len(all_paths) == 2
    assert len(shortest_paths(nodes2, edges2, "file:x", "file:y", max_paths=1)) == 1


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _path_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store = CortexStore(repo / ".cortex" / "cortex.db")
    nodes, edges = _cross_file_graph()
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)
    return repo


def test_cortex_path_tool_returns_labeled_hops(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _path_repo(tmp_path)

    assert "cortex_path" in {tool["name"] for tool in TOOL_DEFINITIONS}
    result = call_tool("cortex_path", {"repo_path": str(repo), "symbol_a": "Alpha", "symbol_b": "onChanged"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["source"] == "Alpha @ a.cpp:1"
    assert payload["target"] == "onChanged @ b.cpp:1"
    assert payload["returned_count"] == 1
    hops = payload["paths"][0]
    assert [hop["relation"] for hop in hops] == ["contains", "contains", "connects"]
    for hop in hops:
        assert hop["layer"] == "STRUCTURAL"
        assert hop["confidence"] == "EXTRACTED"
        assert hop["origin"] == "regex-parser"
        assert "direction" in hop and "node" in hop and "node_id" in hop


def test_cortex_path_tool_reports_no_path_with_note(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store = CortexStore(repo / ".cortex" / "cortex.db")
    nodes = [_symbol("a.cpp", "isolated_one"), _symbol("b.cpp", "isolated_two")]
    store.reset_repo(repo)
    store.save_graph(repo, nodes, [])

    result = call_tool("cortex_path", {"repo_path": str(repo), "symbol_a": "isolated_one", "symbol_b": "isolated_two"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["paths"] == []
    assert "note" in payload


def test_cortex_relations_items_carry_confidence_and_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _path_repo(tmp_path)

    payload = _payload(call_tool("cortex_relations", {"repo_path": str(repo), "symbol": "changed"}))
    assert payload["items"]
    for item in payload["items"]:
        assert item["confidence"] == "EXTRACTED"
        assert item["layer"] == "STRUCTURAL"
        assert item["origin"] == "regex-parser"
