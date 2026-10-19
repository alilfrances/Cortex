from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("def a():\n    return 1\n")
    (root / "b.py").write_text("def b():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    (root / "a.py").write_text("def a():\n    return 2\n")
    _git(root, "commit", "-qam", "two")
    ingest_repository(root)
    return root


def _counting(monkeypatch) -> list[int]:
    calls = [0]
    real = tools.analyze_risk
    monkeypatch.setattr(tools, "analyze_risk", lambda *a, **k: calls.__setitem__(0, calls[0] + 1) or real(*a, **k))
    return calls


def _risk(repo: Path, **arguments) -> dict:
    return json.loads(tools.call_tool("cortex_risk", {"repo_path": str(repo), **arguments})["content"][0]["text"])


def test_identical_calls_analyse_once(repo, monkeypatch):
    calls = _counting(monkeypatch)
    first = _risk(repo, range="HEAD~1..HEAD")
    second = _risk(repo, range="HEAD~1..HEAD")
    assert calls[0] == 1
    assert second["files"] == first["files"]


def test_a_new_commit_changes_the_default_range_and_misses_the_cache(repo, monkeypatch):
    calls = _counting(monkeypatch)
    before = _risk(repo)
    (repo / "b.py").write_text("def b():\n    return 5\n")
    _git(repo, "commit", "-qam", "three")
    after = _risk(repo)
    assert calls[0] == 2
    assert [f["path"] for f in before["files"]] != [f["path"] for f in after["files"]]


def test_staged_changes_miss_the_cache(repo, monkeypatch):
    calls = _counting(monkeypatch)
    _risk(repo, staged=True)
    (repo / "b.py").write_text("def b():\n    return 9\n")
    _git(repo, "add", "b.py")
    staged = _risk(repo, staged=True)
    assert calls[0] == 2
    assert [f["path"] for f in staged["files"]] == ["b.py"]


def test_errors_are_not_cached(repo, monkeypatch):
    calls = _counting(monkeypatch)
    _risk(repo, range="nope..nothing")
    _risk(repo, range="nope..nothing")
    assert calls[0] == 2
