from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


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
