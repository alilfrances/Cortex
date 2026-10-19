from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.store import CortexStore, default_db_path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "a.py").write_text("def a():\n    return 1\n")
    (repo / "b.cpp").write_text("int b() { return 2; }\n")
    (repo / "c.js").write_text("function c() { return 3; }\n")
    (repo / "notes.md").write_text("# Notes\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    return repo


def test_sql_counts_match_counting_the_loaded_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _repo(tmp_path)
    summary = ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    nodes, _edges = store.fetch_graph(repo)
    expected = Counter(
        str(n.metadata["parser_backend"]) for n in nodes if n.kind == "file" and n.metadata.get("parser_backend")
    )
    assert expected  # the fixture must exercise at least one backend
    assert store.parser_backend_counts(repo) == dict(sorted(expected.items()))
    assert summary["parser_backend_counts"] == dict(sorted(expected.items()))


def test_incremental_summary_reports_the_same_counts(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _repo(tmp_path)
    full = ingest_repository(repo)
    (repo / "a.py").write_text("def a():\n    return 2\n")
    incremental = ingest_repository(repo, incremental=True)
    assert incremental["parser_backend_counts"] == full["parser_backend_counts"]
