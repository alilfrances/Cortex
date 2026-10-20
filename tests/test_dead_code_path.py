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
    (root / "pkg").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "pkg" / "a.py").write_text("def pkg_dead():\n    return 1\n\n\ndef pkg_used_elsewhere():\n    return 2\n")
    (root / "other" / "b.py").write_text(
        "from pkg.a import pkg_used_elsewhere\n\n\ndef other_dead():\n    return 3\n\n\ndef caller():\n    return pkg_used_elsewhere()\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _call(repo: Path, **arguments):
    result = tools.call_tool("cortex_dead_code", {"repo_path": str(repo), "response_format": "detailed", **arguments})
    return result, json.loads(result["content"][0]["text"])


def _symbols(payload: dict) -> set[str]:
    return {item["symbol"] for item in payload["findings"]}


def test_directory_scope_only_reports_symbols_defined_there(repo):
    _result, everything = _call(repo)
    _result, scoped = _call(repo, path="pkg")
    assert {"pkg_dead", "other_dead"} <= _symbols(everything)
    assert "pkg_dead" in _symbols(scoped)
    assert "other_dead" not in _symbols(scoped)
    assert all(item["file"].startswith("pkg/") for item in scoped["findings"])


def test_file_scope_and_path_spellings(repo):
    for spelling in ("other/b.py", "./other/b.py", "other/", "other"):
        _result, scoped = _call(repo, path=spelling)
        assert _symbols(scoped) >= {"other_dead"}, spelling
        assert all(item["file"].startswith("other/") for item in scoped["findings"]), spelling


def test_a_reference_outside_the_scope_still_credits_a_symbol_inside_it(repo):
    _result, scoped = _call(repo, path="pkg")
    assert "pkg_used_elsewhere" not in _symbols(scoped)


def test_unknown_path_is_an_error(repo):
    result, payload = _call(repo, path="does/not/exist")
    assert result.get("isError") is True
    assert payload["error"] == "unknown_path"


def test_a_scoped_call_after_an_unscoped_one_does_not_rescan(repo, monkeypatch):
    calls = [0]
    real = tools.analyze_dead_code
    monkeypatch.setattr(tools, "analyze_dead_code", lambda *a, **k: calls.__setitem__(0, calls[0] + 1) or real(*a, **k))
    _call(repo)
    _call(repo, path="pkg")
    assert calls[0] == 1


def test_path_is_in_the_tool_schema():
    schema = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "cortex_dead_code")["inputSchema"]
    assert "path" in schema["properties"]
