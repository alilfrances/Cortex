from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.bundle import generate_bundle
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    return repo


def _source(path: str, content: str, kind: str = "code") -> SourceRecord:
    return SourceRecord(
        path=path,
        content=content,
        kind=kind,
        size_bytes=len(content),
        modified_at=0.0,
        content_hash=path,
    )


def _file_node(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path)


def _symbol_node(path: str, name: str, start: int, end: int) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind="func",
        label=name,
        source_ref=path,
        granularity="symbol",
        signature=f"def {name}():",
        span_start=start,
        span_end=end,
    )


def test_symbol_span_from_lower_ranked_file_surfaces(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    # X: keyword magnet made of comments and imports only.
    noise_lines = ["import os", "import sys"]
    noise_lines += ["# frobnicate flux frobnicate flux fix" for _ in range(30)]
    noise = "\n".join(noise_lines) + "\n"

    # Y: large file whose bulk is unrelated padding, plus the real fix site.
    padding = "\n".join(
        f"def pad_{i}():\n    return unrelated_widget_{i} + gadget_{i}\n" for i in range(60)
    )
    target = "def frobnicate_flux():\n    return flux + 1\n"
    y_content = target + "\n" + padding + "\n"

    sources = [_source("notes.py", noise), _source("y.py", y_content)]
    y_lines = y_content.splitlines()
    nodes = [
        _file_node("notes.py"),
        _file_node("y.py"),
        _symbol_node("y.py", "frobnicate_flux", 1, 2),
    ] + [
        _symbol_node("y.py", f"pad_{i}", y_lines.index(f"def pad_{i}():") + 1, y_lines.index(f"def pad_{i}():") + 2)
        for i in range(60)
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=400, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    assert items, "bundle must not be empty"

    symbol_items = [item for item in items if item["kind"] == "symbol"]
    assert any(
        item["item_id"] == "symbol-span:symbol:y.py:frobnicate_flux" and item["path"] == "y.py"
        for item in symbol_items
    )

    code_like = [item for item in items if item["kind"] in ("code", "symbol")]
    first = code_like[0]
    assert "frobnicate_flux" in first["content"]
    for item in code_like:
        body_lines = [line for line in item["content"].splitlines() if line.strip()]
        assert not all(line.strip().startswith(("import ", "#include")) for line in body_lines)


def test_symbol_item_metadata_and_no_include_block_content(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    padding = "\n".join(
        f"def filler_{i}():\n    return other_thing_{i}\n" for i in range(80)
    )
    content = "import os\nimport sys\n\ndef frobnicate_flux():\n    return flux + 1\n\n" + padding + "\n"
    sources = [_source("engine.py", content)]
    nodes = [
        _file_node("engine.py"),
        _symbol_node("engine.py", "frobnicate_flux", 4, 5),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=200, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    assert items
    span_items = [item for item in items if item["kind"] == "symbol"]
    assert len(span_items) == 1
    span = span_items[0]
    assert span["metadata"]["node_id"] == "symbol:engine.py:frobnicate_flux"
    assert span["metadata"]["span_start"] == 4
    assert span["metadata"]["span_end"] == 5
    assert "frobnicate_flux" in span["content"]
    assert "import os" not in span["content"]


def test_whole_file_still_returned_when_it_fits(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    content = "def frobnicate_flux():\n    return flux + 1\n"
    sources = [_source("small.py", content)]
    nodes = [
        _file_node("small.py"),
        _symbol_node("small.py", "frobnicate_flux", 1, 2),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=4000, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    code_items = [item for item in items if item["kind"] == "code"]
    assert len(code_items) == 1
    assert code_items[0]["content"] == content
    assert not any(item["kind"] == "symbol" for item in items)
