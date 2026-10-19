from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.cochange import build_cochange_edges
from cortex.impact import rank_file_impact
from cortex.ingest import ingest_repository
from cortex.models import CommitRecord
from cortex.store import CortexStore, default_db_path


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)


def _build_coupled_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "h.h").write_text("#pragma once\nvoid helper();\n")
    (repo / "a.cpp").write_text('#include "h.h"\nvoid alpha() {}\n')
    _commit_all(repo, "init")
    (repo / "b.cpp").write_text("void beta() {}\n")
    _commit_all(repo, "add b")

    for i in range(3):
        (repo / "a.cpp").write_text(f'#include "h.h"\nvoid alpha() {{}} // rev {i}\n')
        (repo / "b.cpp").write_text(f"void beta() {{}} // rev {i}\n")
        _commit_all(repo, f"change a and b together {i}")
    return repo


def test_cochange_partner_ranks_at_or_above_include_target(tmp_path):
    repo = _build_coupled_repo(tmp_path)
    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)

    items, _truncated = rank_file_impact("a.cpp", nodes, edges)
    by_path = {item["path"]: item for item in items}
    assert "b.cpp" in by_path
    assert "h.h" in by_path
    ranks = [item["path"] for item in items]
    assert ranks.index("b.cpp") <= ranks.index("h.h")

    cochange_entries = [
        entry for entry in by_path["b.cpp"]["why"] if entry["relation"] == "cochange"
    ]
    assert cochange_entries
    assert cochange_entries[0]["cochange_count"] == 3

    for item in items:
        for entry in item["why"]:
            assert "layer" in entry
            assert "confidence" in entry


def test_incremental_refresh_does_not_duplicate_cochange_layer(tmp_path):
    repo = _build_coupled_repo(tmp_path)
    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))

    def counts() -> tuple[int, int]:
        nodes, edges = store.fetch_graph(repo)
        cochange_edges = [e for e in edges if e.layer == "COCHANGE"]
        commit_nodes = [n for n in nodes if n.kind == "commit"]
        return len(cochange_edges), len(commit_nodes)

    baseline = counts()
    assert baseline[0] > 0
    assert baseline[1] > 0

    # A changed file forces the incremental merge path.
    (repo / "b.cpp").write_text("void beta() {} // touched\n")
    ingest_repository(repo, incremental=True)
    assert counts() == baseline

    # Repeat runs with no file changes must also stay stable.
    ingest_repository(repo, incremental=True)
    ingest_repository(repo, incremental=True)
    assert counts() == baseline


def test_bulk_commit_contributes_no_cochange_pairs():
    bulk = CommitRecord(
        sha="bulk",
        summary="vendor import",
        author="a",
        authored_at=0,
        files=[f"vendor/f{i}.cpp" for i in range(60)],
    )
    assert build_cochange_edges([bulk]) == []

    normal = CommitRecord(sha="ok", summary="msg", author="a", authored_at=0, files=["a.py", "b.py"])
    edges = build_cochange_edges([bulk, normal])
    assert len(edges) == 1
    assert edges[0].metadata["count"] == 1


def test_cross_file_cpp_call_makes_the_defining_file_an_impact_neighbor(tmp_path):
    """Task 9: ``engine.cpp`` calls ``compute`` (declared in ``util.hpp``,
    defined in ``util.cpp``).  Before, ``util.cpp`` had no file-level edge to
    ``engine.cpp`` and did not appear; the resolved call edge now makes it a
    neighbour with a ``calls`` why-entry."""
    from tests.golden_utils import treesitter_available

    if not treesitter_available():
        import pytest

        pytest.skip("C++ call edges are resolved only by the tree-sitter backend")
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "util.hpp").write_text("#pragma once\nint compute(int x);\n")
    (repo / "util.cpp").write_text('#include "util.hpp"\nint compute(int x) {\n    return x * 2;\n}\n')
    (repo / "engine.cpp").write_text('#include "util.hpp"\nint run() {\n    return compute(21);\n}\n')
    _commit_all(repo, "init")
    ingest_repository(repo)
    nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)

    items, _truncated = rank_file_impact("engine.cpp", nodes, edges)
    by_path = {item["path"]: item for item in items}

    assert "util.cpp" in by_path
    assert [w for w in by_path["util.cpp"]["why"] if w["layer"] == "STRUCTURAL"] == [
        {"relation": "calls", "symbol": "run -> compute", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"}
    ]
    assert {"relation": "imports", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"} in by_path["util.hpp"]["why"]
    reverse, _ = rank_file_impact("util.cpp", nodes, edges)
    assert "engine.cpp" in {item["path"] for item in reverse}
