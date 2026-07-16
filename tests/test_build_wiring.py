from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.graph import build_graph
from cortex.mcp.tools import call_tool
from cortex.models import SourceRecord
from cortex.store import CortexStore
from cortex.structural.regex_backend import extract_regex_edges


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(path, content, "code", len(content), 0.0, "")


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)


def test_cmake_wiring_resolves_known_sources_across_lines():
    content = """
add_executable(app
    WIN32
    src/main.cpp
    ${APP_SOURCES}
)
target_sources(app PRIVATE
    src/widget.cpp
    ../outside.cpp
)
"""
    known_paths = {"CMakeLists.txt", "src/main.cpp", "src/widget.cpp"}

    nodes, edges = extract_regex_edges("CMakeLists.txt", content, known_paths)

    assert "target:app" in {node.node_id for node in nodes}
    assert {(edge.relation, edge.target) for edge in edges} == {
        ("builds", "file:src/main.cpp"),
        ("builds", "file:src/widget.cpp"),
    }
    assert all("${" not in edge.target for edge in edges)


def test_qrc_wiring_keeps_only_resolvable_entries():
    content = "<RCC><qresource><file>icon.png</file><file>missing.png</file></qresource></RCC>"

    _nodes, edges = extract_regex_edges(
        "resources/app.qrc",
        content,
        {"resources/app.qrc", "resources/icon.png"},
    )

    registers = [edge for edge in edges if edge.relation == "registers"]
    assert len(registers) == 1
    assert registers[0].target == "file:resources/icon.png"


def test_build_edges_are_visible_through_relations_with_regex_origin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    content = "add_executable(app src/main.cpp)\n"
    nodes, edges = build_graph(
        [_source("CMakeLists.txt", content), _source("src/main.cpp", "int main() {}\n")],
        [],
    )
    store = CortexStore(repo / ".cortex" / "cortex.db")
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)

    payload = _payload(
        call_tool(
            "cortex_relations",
            {"repo_path": str(repo), "relation": "builds", "symbol": "src/main.cpp", "direction": "in"},
        )
    )

    assert payload["items"] == [
        {
            "confidence": "EXTRACTED",
            "layer": "STRUCTURAL",
            "origin": "regex-parser",
            "relation": "builds",
            "source": "app @ CMakeLists.txt:1",
            "target": "src/main.cpp @ src/main.cpp",
        }
    ]
