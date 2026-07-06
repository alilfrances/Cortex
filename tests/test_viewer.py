from __future__ import annotations

import re
from pathlib import Path

from cortex.models import GraphEdge, GraphNode
from cortex.viewer import write_html


def test_viewer_has_no_external_src_or_href_references(tmp_path: Path) -> None:
    nodes = [GraphNode("file:a.py", "file", "a.py", "a.py")]
    out = tmp_path / "graph.html"

    write_html(nodes, [], {"file:a.py": 1}, out)

    html = out.read_text(encoding="utf-8")
    external_attrs = re.findall(r"""(?:src|href)\s*=\s*["'][^"']*https?://""", html, flags=re.IGNORECASE)
    assert external_attrs == []


def test_viewer_embeds_node_labels(tmp_path: Path) -> None:
    nodes = [
        GraphNode("file:app.py", "file", "Application Entry", "app.py"),
        GraphNode("file:db.py", "file", "Database Layer", "db.py"),
    ]
    edges = [GraphEdge("e1", "file:app.py", "file:db.py", "imports", layer="STRUCTURAL")]
    out = tmp_path / "graph.html"

    write_html(nodes, edges, {"file:app.py": 1, "file:db.py": 2}, out)

    html = out.read_text(encoding="utf-8")
    assert "Application Entry" in html
    assert "Database Layer" in html


def test_viewer_large_graph_guard_writes_advisory_instead_of_render(tmp_path: Path) -> None:
    nodes = [
        GraphNode(f"file:{index}.py", "file", f"{index}.py", f"{index}.py")
        for index in range(2001)
    ]
    out = tmp_path / "large.html"

    write_html(nodes, [], {}, out)

    html = out.read_text(encoding="utf-8")
    assert "use the Obsidian export instead" in html
    assert "const graph =" not in html
