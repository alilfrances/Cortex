from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


def _repo_with_index(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "auth.py").write_text("def login(): pass")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo)
    return repo


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_query_auto_refreshes_stale_index(tmp_path):
    repo = _repo_with_index(tmp_path)
    (repo / "billing.py").write_text("def charge(): pass")

    payload = _payload(call_tool("cortex_query", {"repo_path": str(repo), "task": "billing charge"}))

    assert payload["stale"] is False
    assert payload["auto_refreshed"]["new_files"] == 1
    assert any(item["path"] == "billing.py" for item in payload["items"])


def test_query_fresh_index_skips_refresh(tmp_path):
    repo = _repo_with_index(tmp_path)

    payload = _payload(call_tool("cortex_query", {"repo_path": str(repo), "task": "auth login"}))

    assert payload["stale"] is False
    assert "auto_refreshed" not in payload


def test_auto_refresh_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    repo = _repo_with_index(tmp_path)
    (repo / "billing.py").write_text("def charge(): pass")

    payload = _payload(call_tool("cortex_query", {"repo_path": str(repo), "task": "billing charge"}))

    assert payload["stale"] is True
    assert "auto_refreshed" not in payload
    assert not any(item["path"] == "billing.py" for item in payload["items"])


def test_search_symbols_auto_refreshes(tmp_path):
    repo = _repo_with_index(tmp_path)
    (repo / "billing.py").write_text("def charge(): pass")

    payload = _payload(call_tool("cortex_search_symbols", {"repo_path": str(repo), "query": "charge"}))

    assert payload["stale"] is False
    assert any("charge" in item.get("label", "") for item in payload["items"])


def test_missing_db_still_errors_without_ingesting(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "auth.py").write_text("def login(): pass")

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "auth"})

    assert result["isError"] is True
    assert not (repo / ".cortex" / "cortex.db").exists()
