from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from cortex.export import export_graphml, export_json, export_obsidian
from cortex.models import Community, GraphEdge, GraphNode


def _sample_graph() -> tuple[list[GraphNode], list[GraphEdge], dict[str, int]]:
    nodes = [
        GraphNode("file:src/app.py", "file", "src/app.py", "src/app.py", granularity="file"),
        GraphNode("file:src/db.py", "file", "src/db.py", "src/db.py", granularity="file"),
        GraphNode("symbol:src/app.py:run", "function", "run", "src/app.py", granularity="symbol"),
    ]
    edges = [
        GraphEdge(
            "e1",
            "file:src/app.py",
            "file:src/db.py",
            "imports",
            layer="STRUCTURAL",
            confidence="EXTRACTED",
            weight=2.5,
        ),
        GraphEdge(
            "e2",
            "file:src/app.py",
            "symbol:src/app.py:run",
            "contains",
            layer="STRUCTURAL",
            confidence="LOW",
            weight=1.0,
        ),
    ]
    return nodes, edges, {"file:src/app.py": 7, "file:src/db.py": 7, "symbol:src/app.py:run": 7}


def test_graphml_reparses_with_required_attributes(tmp_path: Path) -> None:
    nodes, edges, communities = _sample_graph()
    out = tmp_path / "graph.graphml"

    export_graphml(nodes, edges, communities, out)

    root = ET.parse(out).getroot()
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    graph_nodes = root.findall(".//g:node", ns)
    graph_edges = root.findall(".//g:edge", ns)
    assert len(graph_nodes) == len(nodes)
    assert len(graph_edges) == len(edges)

    data_by_node = {
        node.attrib["id"]: {data.attrib["key"]: data.text for data in node.findall("g:data", ns)}
        for node in graph_nodes
    }
    data_by_edge = {
        edge.attrib["id"]: {data.attrib["key"]: data.text for data in edge.findall("g:data", ns)}
        for edge in graph_edges
    }
    assert data_by_node["file:src/app.py"] == {
        "kind": "file",
        "granularity": "file",
        "community": "7",
    }
    assert data_by_edge["e1"] == {
        "layer": "STRUCTURAL",
        "relation": "imports",
        "weight": "2.5",
        "confidence": "EXTRACTED",
    }


def test_json_export_round_trips(tmp_path: Path) -> None:
    nodes, edges, communities = _sample_graph()
    out = tmp_path / "graph.json"

    export_json(nodes, edges, communities, out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload == {
        "nodes": [node.to_dict() | {"community": communities.get(node.node_id)} for node in nodes],
        "edges": [edge.to_dict() for edge in edges],
        "communities": communities,
    }


def test_obsidian_export_writes_file_notes_wikilinks_community_tags_and_index(tmp_path: Path) -> None:
    nodes, edges, communities = _sample_graph()
    out_dir = tmp_path / "vault"

    export_obsidian(nodes, edges, communities, out_dir)

    notes = {path.name: path.read_text(encoding="utf-8") for path in out_dir.glob("*.md")}
    assert "index.md" in notes
    app_note = next(content for name, content in notes.items() if name.startswith("src_app.py"))
    assert "community/7" in app_note
    assert "cortex/file" in app_note
    assert "[[src_db.py]]" in app_note
    assert not any("/" in path.name for path in out_dir.glob("*.md"))


def test_obsidian_export_sanitizes_unsafe_filenames(tmp_path: Path) -> None:
    nodes = [
        GraphNode(
            "file:src/auth:login?.py",
            "file",
            "src/auth:login?.py",
            "src/auth:login?.py",
            granularity="file",
        )
    ]

    export_obsidian(nodes, [], {"file:src/auth:login?.py": 3}, tmp_path)

    filenames = [path.name for path in tmp_path.glob("*.md")]
    assert "src_auth_login_.py.md" in filenames
    assert all(not any(char in name for char in '/\\:*?"<>|') for name in filenames)
