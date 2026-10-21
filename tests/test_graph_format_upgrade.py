"""An index written by a pre-0.10.0 Cortex is rebuilt in full once (Task 12 audit).

0.10.0 changed extraction (call-site metadata on every `calls` edge, call edges
for C/C++/JS/TS/Go/Rust/Swift/Java/Ruby, Ruby method symbols).  An incremental
refresh only re-extracts changed files, so an upgraded database used to keep
call-less / metadata-less rows for every untouched file until a manual full
`cortex ingest`.  The `repos.graph_version` stamp makes the upgrade automatic:
`cortex ingest --update`, `cortex_refresh` and the MCP auto-refresh all rebuild
an older-format index in full, and the MCP freshness check reports it stale even
when no file changed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.mcp import tools as tools_module
from cortex.store import GRAPH_FORMAT_VERSION, CortexStore, default_db_path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.t"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "util.py").write_text("def helper():\n    return 1\n")
    (repo / "app.py").write_text("from util import helper\n\n\ndef run():\n    return helper()\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _downgrade_to_pre_010(repo: Path) -> None:
    """Make the index look like a pre-0.10.0 one: no format stamp, and call edges
    without the call-site contract (old `name:` targets, no metadata)."""
    store = CortexStore(default_db_path(repo))
    key = str(repo.resolve())
    with store.connection:
        store.connection.execute("UPDATE repos SET graph_version = 0 WHERE repo_path = ?", (key,))
        store.connection.execute(
            "UPDATE graph_edges SET target = 'name:helper', metadata_json = '{}' WHERE repo_path = ? AND relation = 'calls'",
            (key,),
        )
    store.connection.close()


def _calls(repo: Path) -> list:
    store = CortexStore(default_db_path(repo))
    try:
        return [edge for edge in store.fetch_graph(repo)[1] if edge.relation == "calls"]
    finally:
        store.connection.close()


def test_fresh_full_ingest_stamps_the_current_format(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    summary = ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    assert store.get_repo_graph_version(repo) == GRAPH_FORMAT_VERSION >= 1
    assert "full_reindex_reason" not in summary


def test_incremental_refresh_of_an_old_index_rebuilds_in_full(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ingest_repository(repo)
    _downgrade_to_pre_010(repo)
    assert all(not edge.metadata for edge in _calls(repo))

    summary = ingest_repository(repo, incremental=True)  # what `cortex ingest --update` runs

    assert summary["full_reindex_reason"].startswith("index graph format 0 is older than")
    calls = _calls(repo)
    assert calls and all(edge.metadata.get("callee") for edge in calls)
    assert any(edge.target == "symbol:util.py:helper" for edge in calls)
    assert CortexStore(default_db_path(repo)).get_repo_graph_version(repo) == GRAPH_FORMAT_VERSION

    # Once upgraded, refreshes are incremental again.
    again = ingest_repository(repo, incremental=True)
    assert "full_reindex_reason" not in again and "node_count" not in again


def test_mcp_without_a_budget_auto_refreshes_an_old_index_in_full(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")
    repo = _repo(tmp_path)
    ingest_repository(repo)
    _downgrade_to_pre_010(repo)

    payload = json.loads(call_tool("cortex_query", {"repo_path": str(repo), "task": "helper"})["content"][0]["text"])

    assert payload["stale"] is False
    assert payload["auto_refreshed"]["full_reindex_reason"].startswith("index graph format 0")
    assert any(edge.target == "symbol:util.py:helper" for edge in _calls(repo))


def test_mcp_with_a_budget_does_not_rebuild_in_full_inside_a_read_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "30")
    repo = _repo(tmp_path)
    ingest_repository(repo)
    _downgrade_to_pre_010(repo)
    ingested: list[bool] = []
    real = tools_module.ingest_repository
    monkeypatch.setattr(tools_module, "ingest_repository", lambda *a, **k: ingested.append(True) or real(*a, **k))

    payload = json.loads(call_tool("cortex_search_symbols", {"repo_path": str(repo), "query": "helper"})["content"][0]["text"])

    assert ingested == []
    assert payload["stale"] is True
    assert "cortex_refresh" in payload["refresh_hint"]
    assert "auto_refreshed" not in payload
    # The explicit refresh does the rebuild.
    refreshed = json.loads(call_tool("cortex_refresh", {"repo_path": str(repo), "mode": "full"})["content"][0]["text"])
    assert refreshed["stale"] is False
    assert CortexStore(default_db_path(repo)).get_repo_graph_version(repo) == GRAPH_FORMAT_VERSION


def test_refresh_reports_full_mode_when_an_old_index_is_rebuilt_in_full(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ingest_repository(repo)
    _downgrade_to_pre_010(repo)

    upgraded = json.loads(call_tool("cortex_refresh", {"repo_path": str(repo)})["content"][0]["text"])  # incremental by default

    assert upgraded["summary"]["full_reindex_reason"].startswith("index graph format 0")
    assert upgraded["mode"] == "full"
    # Once upgraded, an incremental request is incremental again, and says so.
    again = json.loads(call_tool("cortex_refresh", {"repo_path": str(repo)})["content"][0]["text"])
    assert "full_reindex_reason" not in again["summary"] and again["mode"] == "incremental"


def test_old_index_without_auto_refresh_is_reported_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    repo = _repo(tmp_path)
    ingest_repository(repo)
    _downgrade_to_pre_010(repo)

    payload = json.loads(call_tool("cortex_query", {"repo_path": str(repo), "task": "helper"})["content"][0]["text"])

    assert payload["stale"] is True


def test_upgrade_drops_query_cache_rows_computed_from_the_old_graph(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    store.set_query_cache(repo, "old-key", "{}")
    store.connection.close()
    _downgrade_to_pre_010(repo)

    ingest_repository(repo, incremental=True)

    assert CortexStore(default_db_path(repo)).get_query_cache(repo, "old-key") is None
