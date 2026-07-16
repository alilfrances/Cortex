from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.mcp.tools import TOOL_DEFINITIONS, call_tool
from cortex.mcp.server import _handle
from cortex.store import CortexStore, default_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _ingest_repo_with_error_message(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app").mkdir()
    (repo / "app" / "messages.py").write_text(
        'DEVICE_OFFLINE_ERROR = "please power-cycle the gateway before retrying"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add message"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


def test_cortex_search_text_registered_in_tool_definitions():
    names = {tool["name"] for tool in TOOL_DEFINITIONS}
    assert "cortex_search_text" in names
    definition = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "cortex_search_text")
    assert definition["inputSchema"]["required"] == ["query"]


def test_cortex_search_text_discoverable_via_mcp_tools_list():
    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert "cortex_search_text" in names


def test_cortex_search_text_finds_body_text_string_literal(tmp_path, monkeypatch):
    repo = _ingest_repo_with_error_message(tmp_path, monkeypatch)

    result = call_tool("cortex_search_text", {"repo_path": str(repo), "query": "power-cycle gateway"})
    payload = _payload(result)

    assert payload["fts_available"] is True
    paths = [item["path"] for item in payload["items"]]
    assert "app/messages.py" in paths
    item = next(item for item in payload["items"] if item["path"] == "app/messages.py")
    assert "snippet" in item
    assert item["snippet"].startswith("L1:")


def test_cortex_search_text_missing_query_is_error(tmp_path, monkeypatch):
    repo = _ingest_repo_with_error_message(tmp_path, monkeypatch)
    result = call_tool("cortex_search_text", {"repo_path": str(repo), "query": ""})
    assert result["isError"] is True
    assert _payload(result)["error"] == "missing_query"


def test_cortex_search_text_missing_db_uses_existing_error_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    result = call_tool("cortex_search_text", {"repo_path": str(repo), "query": "anything"})
    assert result["isError"] is True
    assert _payload(result)["error"] == "missing_db"


def test_cortex_search_text_budget_truncates_items(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app").mkdir()
    for i in range(20):
        (repo / "app" / f"mod_{i}.py").write_text(
            f'MSG_{i} = "distinctivephrase occurrence number {i} padded with extra words to add tokens"\n',
            encoding="utf-8",
        )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add many"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    full = _payload(call_tool("cortex_search_text", {"repo_path": str(repo), "query": "distinctivephrase", "limit": 20, "budget": 100000}))
    capped = _payload(call_tool("cortex_search_text", {"repo_path": str(repo), "query": "distinctivephrase", "limit": 20, "budget": 20}))

    assert full["truncated"] is False
    assert capped["truncated"] is True
    assert capped["returned_count"] < full["returned_count"]


def test_cortex_search_text_no_fts5_returns_empty_items_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(CortexStore, "_init_fts5", lambda self: False)
    repo = _ingest_repo_with_error_message(tmp_path, monkeypatch)

    result = call_tool("cortex_search_text", {"repo_path": str(repo), "query": "power-cycle gateway"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["fts_available"] is False
    assert payload["items"] == []


def test_cortex_search_text_records_ledger_row_with_referenced_file_baseline(tmp_path, monkeypatch):
    repo = _ingest_repo_with_error_message(tmp_path, monkeypatch)

    call_tool("cortex_search_text", {"repo_path": str(repo), "query": "power-cycle gateway"})

    store = CortexStore(default_db_path(repo))
    rows = store.fetch_tool_usage(repo)
    matching = [row for row in rows if row["tool"] == "cortex_search_text"]
    assert len(matching) == 1
    assert matching[0]["baseline_tokens"] > 0
    assert matching[0]["response_tokens"] > 0
