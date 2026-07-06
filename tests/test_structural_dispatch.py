from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cortex.graph import build_graph
from cortex.ingest import ingest_repository
from cortex.models import SourceRecord
from cortex.store import CortexStore


FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "multilang_repo"


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(path=path, content=content, kind="code", size_bytes=len(content), modified_at=0)


def test_regex_fallback_when_tree_sitter_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tree_sitter", None)

    from cortex.structural import extract_structural_edges

    nodes, edges = extract_structural_edges("app.js", "import thing from './thing';\nfunction run() {}\n", set())

    assert "symbol:app.js:run" in {node.node_id for node in nodes}
    assert {edge.confidence for edge in edges} == {"LOW"}
    assert {"imports", "contains"}.issubset({edge.relation for edge in edges})


def test_python_files_are_not_routed_to_structural_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    import cortex.graph as graph

    def fail_dispatch(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise AssertionError("Python files must stay on ast_extract.extract_python_edges")

    monkeypatch.setattr(graph, "extract_structural_edges", fail_dispatch)

    nodes, edges = build_graph([_source("app.py", "def run():\n    return 1\n")], commits=[])

    assert "symbol:app.py:run" in {node.node_id for node in nodes}
    assert any(edge.confidence == "EXTRACTED" for edge in edges if edge.relation == "contains")


def test_dispatcher_falls_back_to_regex_when_tree_sitter_parse_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    nodes, edges = extract_structural_edges("service.go", "import \"fmt\"\nfunc Run() {}\n", set())

    assert "symbol:service.go:Run" in {node.node_id for node in nodes}
    assert edges
    assert {edge.confidence for edge in edges} == {"LOW"}


def test_dispatcher_returns_empty_when_regex_fallback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.regex_backend as regex_backend
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail)
    monkeypatch.setattr(regex_backend, "extract_regex_edges", fail)

    assert extract_structural_edges("broken.rs", "pub fn run( {", set()) == ([], [])


def test_ingest_multilang_fixture_uses_low_confidence_regex_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    repo = tmp_path / "multilang_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixtures"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    db_path = tmp_path / "cortex.db"
    ingest_repository(repo, commit_limit=0, db_path=db_path)

    nodes, edges = CortexStore(db_path).fetch_graph(repo)
    node_ids = {node.node_id for node in nodes}

    assert "symbol:app.js:start" in node_ids
    assert "symbol:client.ts:Client" in node_ids
    assert "symbol:main.go:Run" in node_ids
    assert "symbol:lib.rs:run" in node_ids
    assert "symbol:App.java:App" in node_ids
    assert "symbol:worker.rb:Worker" in node_ids

    fixture_paths = {path.name for path in FIXTURE_REPO.iterdir()}
    structural = [
        edge
        for edge in edges
        if edge.layer == "STRUCTURAL" and edge.metadata.get("source_file") in fixture_paths
    ]
    assert structural
    assert {edge.confidence for edge in structural} == {"LOW"}
