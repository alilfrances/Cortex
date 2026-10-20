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
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "0")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "app.py").write_text("from helper import run_it\n\n\ndef main():\n    return run_it()\n")
    (root / "helper.py").write_text("def run_it():\n    return 1\n")
    (root / "test_app.py").write_text("from app import main\n\n\ndef test_main():\n    assert main() == 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    (root / "helper.py").write_text("def run_it():\n    return 2\n")
    _git(root, "commit", "-qam", "two")
    ingest_repository(root)
    return root


CASES = [
    ("cortex_query", {"task": "run it helper"}),
    ("cortex_risk", {"range": "HEAD~1..HEAD"}),
    ("cortex_impact", {"path": "helper.py"}),
    ("cortex_path", {"symbol_a": "file:app.py", "symbol_b": "file:helper.py"}),
    ("cortex_references", {"symbol": "run_it"}),
]


@pytest.mark.parametrize(("tool", "arguments"), CASES, ids=[name for name, _ in CASES])
def test_read_tool_stops_at_its_budget(repo, monkeypatch, tool, arguments):
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0.000001")
    result = tools.call_tool(tool, {"repo_path": str(repo), **arguments})
    payload = json.loads(result["content"][0]["text"])
    assert result.get("isError") is True
    assert payload["error"] == "deadline_exceeded"
    assert payload["tool"] == tool


@pytest.mark.parametrize(("tool", "arguments"), CASES, ids=[name for name, _ in CASES])
def test_read_tool_finishes_without_a_budget(repo, monkeypatch, tool, arguments):
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")
    result = tools.call_tool(tool, {"repo_path": str(repo), **arguments})
    assert result.get("isError") is not True, result["content"][0]["text"][:200]


def test_dead_code_soft_stop_is_counted(repo, monkeypatch):
    from cortex.mcp.status import REGISTRY

    before = REGISTRY.snapshot()["total_soft_deadline"]
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0.000001")
    result = tools.call_tool("cortex_dead_code", {"repo_path": str(repo)})
    payload = json.loads(result["content"][0]["text"])
    assert result.get("isError") is not True
    assert payload.get("partial") is True
    assert REGISTRY.snapshot()["total_soft_deadline"] == before + 1
