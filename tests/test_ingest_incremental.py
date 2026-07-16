from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.store import CortexStore, default_db_path

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "multilang_repo"


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


def _commit_all(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)


def test_incremental_update_only_reprocesses_changed_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass")
    (repo / "db.py").write_text("def connect(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    result1 = ingest_repository(repo)
    assert result1["source_count"] == 2

    # Modify only auth.py
    (repo / "auth.py").write_text("def login(): pass\ndef logout(): pass")
    result2 = ingest_repository(repo, incremental=True)

    assert result2["updated_files"] == 1
    assert result2["unchanged_files"] == 1


def test_incremental_detects_new_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)

    (repo / "new_feature.py").write_text("def feature(): pass")
    result = ingest_repository(repo, incremental=True)
    assert result["new_files"] == 1


def test_incremental_nothing_changed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)
    result = ingest_repository(repo, incremental=True)
    assert result["new_files"] == 0
    assert result["updated_files"] == 0
    assert result["unchanged_files"] == 1


def test_incremental_removes_deleted_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass")
    (repo / "old.py").write_text("def legacy(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)
    (repo / "old.py").unlink()
    result = ingest_repository(repo, incremental=True)

    assert result["deleted_files"] == 1

    from cortex.store import CortexStore, default_db_path

    store = CortexStore(default_db_path(repo))
    paths = {s.path for s in store.fetch_sources(repo)}
    assert "old.py" not in paths
    nodes, edges = store.fetch_graph(repo)
    assert not any(n.source_ref == "old.py" for n in nodes)


def test_incremental_drops_stale_symbols_from_db(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass\ndef logout(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)
    (repo / "auth.py").write_text("def login(): pass")
    ingest_repository(repo, incremental=True)

    from cortex.store import CortexStore, default_db_path

    store = CortexStore(default_db_path(repo))
    nodes, _ = store.fetch_graph(repo)
    labels = {n.label for n in nodes}
    assert "logout" not in labels


# --- P0-3 regression coverage -------------------------------------------------
#
# These tests pin down the "fast-path incremental ingest" contract: unchanged
# files must not be re-read, and re-ingesting must not duplicate or drop
# COCHANGE edges (co-change edges come from git history, not file contents).


def test_incremental_no_changes_is_byte_stable(tmp_path):
    """Re-running incremental ingest with zero file/commit changes must leave
    the persisted graph exactly as it was: same node ids, same edge ids, same
    COCHANGE edges (no duplicates, no drops)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login():\n    check()\n\ndef check():\n    pass\n")
    (repo / "session.py").write_text("def start():\n    pass\n")
    _commit_all(repo, "init")
    (repo / "auth.py").write_text("def login():\n    check()\n\ndef check():\n    pass\n# comment\n")
    (repo / "session.py").write_text("def start():\n    pass\n# comment\n")
    _commit_all(repo, "second, touches both files together")

    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    nodes_before, edges_before = store.fetch_graph(repo)
    node_ids_before = sorted(n.node_id for n in nodes_before)
    edge_ids_before = sorted(e.edge_id for e in edges_before)
    cochange_before = sorted(
        (e.edge_id, e.weight) for e in edges_before if e.relation == "cochange"
    )
    assert cochange_before  # sanity: the fixture actually produced a cochange edge

    result = ingest_repository(repo, incremental=True)
    assert result["new_files"] == 0
    assert result["updated_files"] == 0
    assert result["deleted_files"] == 0

    nodes_after, edges_after = store.fetch_graph(repo)
    node_ids_after = sorted(n.node_id for n in nodes_after)
    edge_ids_after = sorted(e.edge_id for e in edges_after)
    cochange_after = sorted(
        (e.edge_id, e.weight) for e in edges_after if e.relation == "cochange"
    )

    assert edge_ids_after == edge_ids_before, "no-op incremental run must not add/drop/duplicate edges"
    assert node_ids_after == node_ids_before, "no-op incremental run must not add/drop/duplicate nodes"
    assert cochange_after == cochange_before, "COCHANGE edges must not be duplicated or altered"
    assert len(edge_ids_after) == len(set(edge_ids_after)), "edge ids must stay unique"


def test_incremental_single_file_change_reads_only_that_file(tmp_path, monkeypatch):
    """Stat-first scan: a one-file change must trigger exactly one content
    read, not a full-repo re-read+rehash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    for i in range(10):
        (repo / f"module_{i}.py").write_text(f"def fn_{i}(): pass\n")
    _commit_all(repo, "init")

    ingest_repository(repo)

    read_paths: list[Path] = []
    original_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.is_relative_to(repo) and self.suffix == ".py":
            read_paths.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    (repo / "module_3.py").write_text("def fn_3(): pass\ndef fn_3b(): pass\n")

    result = ingest_repository(repo, incremental=True)

    assert result["updated_files"] == 1
    assert result["unchanged_files"] == 9
    assert read_paths == [repo / "module_3.py"], (
        f"expected exactly one file read, got {read_paths}"
    )


def test_incremental_prunes_stale_edges_for_changed_file(tmp_path):
    """When a call site is removed from a Python file, the incremental update
    must prune the now-stale STRUCTURAL edge, not accumulate it forever."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login():\n    check()\n    extra()\n\ndef check():\n    pass\n\ndef extra():\n    pass\n")
    _commit_all(repo, "init")

    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    _, edges = store.fetch_graph(repo)
    assert any(e.edge_id == "ast:auth.py:calls:login:extra" for e in edges)

    (repo / "auth.py").write_text("def login():\n    check()\n\ndef check():\n    pass\n")
    _commit_all(repo, "remove extra() call")
    ingest_repository(repo, incremental=True)

    _, edges_after = store.fetch_graph(repo)
    stale = [e for e in edges_after if e.edge_id == "ast:auth.py:calls:login:extra"]
    assert stale == [], "stale call edge for a removed call site must be pruned on incremental ingest"


def test_incremental_drops_dangling_cochange_edges_for_deleted_file(tmp_path):
    """A COCHANGE edge referencing a file that has since been deleted must not
    persist forever — it should be dropped once the file is gone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    (repo / "auth.py").write_text("def login(): pass\n")
    (repo / "session.py").write_text("def start(): pass\n")
    _commit_all(repo, "init")

    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    _, edges = store.fetch_graph(repo)
    assert any(e.relation == "cochange" for e in edges)

    (repo / "session.py").unlink()
    _commit_all(repo, "remove session.py")
    ingest_repository(repo, incremental=True)

    _, edges_after = store.fetch_graph(repo)
    dangling = [
        e for e in edges_after
        if e.relation in ("cochange", "touches")
        and ("session.py" in e.source or "session.py" in e.target)
    ]
    assert dangling == [], f"expected no dangling edges referencing deleted file, got {dangling}"


def test_incremental_prunes_stale_edges_for_changed_cpp_file(tmp_path, monkeypatch):
    """Qt/C++ parity: the delta-write path must behave identically for the
    regex/tree-sitter structural backend (used for .cpp/.hpp/.qml) as it does
    for the Python AST backend -- one file's content edit must read only that
    file and prune only that file's stale STRUCTURAL edges, leaving the rest
    of a multi-language repo (JS/TS/Go/Rust/Java/Ruby/C/QML) untouched."""
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE_REPO, repo)
    _init_git_repo(repo)
    _commit_all(repo, "init")

    ingest_repository(repo, commit_limit=0)
    store = CortexStore(default_db_path(repo))
    _, edges = store.fetch_graph(repo)
    compute_edges = [
        edge
        for edge in edges
        if edge.relation == "contains" and edge.target == "symbol:engine.cpp:compute"
    ]
    assert len(compute_edges) == 1
    old_compute_edge_id = compute_edges[0].edge_id
    assert old_compute_edge_id.startswith(("regex:", "treesitter:"))

    read_paths: list[Path] = []
    original_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.is_relative_to(repo):
            read_paths.append(self)
        return original_read_text(self, *args, **kwargs)

    # Rename compute() -> computeValue(): the old STRUCTURAL edge/node must
    # disappear and a new one must appear, without touching any other file.
    engine_cpp = repo / "engine.cpp"
    engine_cpp.write_text(engine_cpp.read_text().replace("compute(int value)", "computeValue(int value)"))

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    result = ingest_repository(repo, commit_limit=0, incremental=True)

    assert result["updated_files"] == 1
    assert read_paths == [engine_cpp], f"expected exactly one file read, got {read_paths}"

    _, edges_after = store.fetch_graph(repo)
    edge_ids_after = {e.edge_id for e in edges_after}
    assert old_compute_edge_id not in edge_ids_after, "stale C++ symbol edge must be pruned"
    assert any(
        edge.relation == "contains" and edge.target == "symbol:engine.cpp:computeValue"
        for edge in edges_after
    ), "new C++ symbol edge must be present"
    # Every other fixture file's edges must be byte-for-byte untouched. This
    # deliberately accepts either the optional tree-sitter or stdlib regex
    # backend's edge-id namespace while still asserting the delta boundary.
    other_files_edges_before = {
        edge.edge_id for edge in edges if edge.metadata.get("source_file") != "engine.cpp"
    }
    other_files_edges_after = {
        edge.edge_id for edge in edges_after if edge.metadata.get("source_file") != "engine.cpp"
    }
    assert other_files_edges_after == other_files_edges_before
