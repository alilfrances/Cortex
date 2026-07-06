from __future__ import annotations

from pathlib import Path

from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord


def _source(path: str, content: str, kind: str) -> SourceRecord:
    return SourceRecord(
        path=path,
        content=content,
        kind=kind,
        size_bytes=len(content),
        modified_at=0.0,
        content_hash="hash",
    )


def test_ruby_and_shell_files_are_classified_as_code():
    assert _classify_path(Path("script.rb")) == "code"
    assert _classify_path(Path("bin/setup.sh")) == "code"


def test_python_hash_comments_do_not_create_section_nodes():
    nodes, edges = build_graph(
        [_source("app.py", "# setup\nprint('ok')\n", "code")],
        [],
    )

    assert [node for node in nodes if node.kind == "section"] == []
    assert [edge for edge in edges if edge.layer == "HEADING"] == []


def test_markdown_hash_headings_still_create_section_nodes():
    nodes, edges = build_graph(
        [_source("README.md", "# Overview\nBody\n", "markdown")],
        [],
    )

    section_nodes = [node for node in nodes if node.kind == "section"]
    assert [node.label for node in section_nodes] == ["Overview"]
    assert [edge for edge in edges if edge.layer == "HEADING"]
