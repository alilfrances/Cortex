from __future__ import annotations

from pathlib import Path

from cortex.models import GraphEdge, GraphNode
from cortex.references import find_references
from cortex.store import CortexStore


def _references_repo(tmp_path: Path) -> tuple[Path, CortexStore]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        "def x(value):\n"
        "    return value\n"
        "read = x\n"
        "x = 1\n"
        "x += 2\n"
        "x.append(3)\n"
        "if x == value:\n"
        "    return x\n",
        encoding="utf-8",
    )
    store = CortexStore(repo / ".cortex" / "cortex.db")
    store.reset_repo(repo)
    store.save_graph(
        repo,
        [
            GraphNode("file:sample.py", "file", "sample.py", "sample.py"),
            GraphNode(
                "symbol:sample.py:x",
                "function",
                "x",
                "sample.py",
                granularity="symbol",
                span_start=1,
            ),
        ],
        [GraphEdge("contains", "file:sample.py", "symbol:sample.py:x", "contains")],
    )
    return repo, store


def test_find_references_tags_reads_writes_and_definitions(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    result = find_references(store, repo, "x")
    hits = {entry["text"]: entry for entries in result["items"].values() for entry in entries}

    assert hits["sample.py:1"]["access"] == "definition"
    assert hits["sample.py:3"]["access"] == "read"
    assert hits["sample.py:4"]["access"] == "write"
    assert hits["sample.py:5"]["access"] == "write"
    assert hits["sample.py:6"]["access"] == "write"
    assert hits["sample.py:7"]["access"] == "read"
    assert hits["sample.py:8"]["access"] == "read"


def test_find_references_writes_mode_keeps_definitions_and_writes(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    result = find_references(store, repo, "x", mode="writes")
    texts = {entry["text"] for entries in result["items"].values() for entry in entries}

    assert texts == {"sample.py:1", "sample.py:4", "sample.py:5", "sample.py:6"}


def test_comparison_operators_are_reads(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "operators.py").write_text(
        "x == y\n"
        "x <= y\n"
        "x >= y\n"
        "x != y\n"
        "x => y\n"
        "x[0] == y\n",
        encoding="utf-8",
    )

    result = find_references(store, repo, "x")
    hits = {entry["text"]: entry for entries in result["items"].values() for entry in entries}

    assert all(hits[f"operators.py:{line}"]["access"] == "read" for line in range(1, 7))
