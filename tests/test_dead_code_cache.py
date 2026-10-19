from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.py").write_text("def used():\n    return 1\n\n\ndef unused_helper():\n    return 2\n\n\nused()\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _count_analyses(monkeypatch) -> list[int]:
    calls = [0]
    real = tools.analyze_dead_code

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(tools, "analyze_dead_code", counted)
    return calls


def _payload(name: str, repo: Path, **args) -> dict:
    return json.loads(tools.call_tool(name, {"repo_path": str(repo), **args})["content"][0]["text"])


def test_repeat_dead_code_call_analyses_once(repo, monkeypatch):
    calls = _count_analyses(monkeypatch)
    first = _payload("cortex_dead_code", repo)
    second = _payload("cortex_dead_code", repo)
    assert calls[0] == 1
    assert first["findings"] == second["findings"]
    assert any(item["symbol"] == "unused_helper" for item in first["findings"])


def test_dead_code_after_overview_reuses_the_overview_analysis(repo, monkeypatch):
    from cortex import report

    calls = [0]
    real = report.analyze_dead_code

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(report, "analyze_dead_code", counted)
    counted_tools = _count_analyses(monkeypatch)
    _payload("cortex_overview", repo)
    result = _payload("cortex_dead_code", repo)
    assert calls[0] == 1
    assert counted_tools[0] == 0
    assert any(item["symbol"] == "unused_helper" for item in result["findings"])
