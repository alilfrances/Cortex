from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import cortex.bundle as bundle_mod
from cortex.cli import main
from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


_AUTH_PY = """\
def login(user, password):
    return user == "admin" and password == "secret"


def logout(user):
    return None
"""

_BILLING_PY = """\
from auth import login


def charge(user, password, amount):
    if not login(user, password):
        raise PermissionError("nope")
    return amount
"""


def _repo_with_index(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "auth.py").write_text(_AUTH_PY, encoding="utf-8")
    (repo / "billing.py").write_text(_BILLING_PY, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


def _count_pagerank_calls(monkeypatch) -> dict:
    """Monkeypatch cortex.bundle.personalized_pagerank with a passthrough
    call counter, per P1-3's acceptance criterion: a cache hit must perform
    NO PageRank recomputation."""
    calls = {"n": 0}
    original = bundle_mod.personalized_pagerank

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(bundle_mod, "personalized_pagerank", _counting)
    return calls


# --- (a) a cache hit performs no PageRank recomputation ---


def test_second_identical_query_skips_pagerank(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = _count_pagerank_calls(monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing"}

    call_tool("cortex_query", args)
    assert calls["n"] == 1, "first call is a cache miss and must recompute"

    call_tool("cortex_query", args)
    assert calls["n"] == 1, "second identical call must hit the cache and skip PageRank"


def test_second_identical_impact_hits_cache(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = {"n": 0}
    original = mcp_tools.rank_file_impact

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "rank_file_impact", _counting)
    args = {"repo_path": str(repo), "path": "auth.py"}

    call_tool("cortex_impact", args)
    assert calls["n"] == 1
    call_tool("cortex_impact", args)
    assert calls["n"] == 1, "second identical cortex_impact call must hit the cache"


def test_second_identical_overview_hits_cache(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = {"n": 0}
    original = mcp_tools.generate_report

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "generate_report", _counting)
    args = {"repo_path": str(repo)}

    call_tool("cortex_overview", args)
    # The P0-1 ledger hook estimates cortex_overview's baseline by
    # re-dispatching a `response_format: detailed` render of the same call
    # (see mcp/tools._detailed_rendering_tokens) -- that shadow call has a
    # different cache key (response_format is part of it) and is itself a
    # miss the first time, so the first outer call legitimately triggers
    # two generate_report runs: one for the concise response, one for the
    # ledger's detailed baseline estimate. Both then land in the cache.
    after_first = calls["n"]
    assert after_first == 2

    call_tool("cortex_overview", args)
    assert calls["n"] == after_first, (
        "second identical cortex_overview call must hit the cache for both "
        "the concise response and the ledger's detailed baseline re-render"
    )


# --- (b) a file edit changes the fingerprint and misses the cache ---


def test_file_edit_invalidates_cache_and_recomputes(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = _count_pagerank_calls(monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing"}

    call_tool("cortex_query", args)
    assert calls["n"] == 1

    (repo / "auth.py").write_text(_AUTH_PY + "\n\ndef extra():\n    return 1\n", encoding="utf-8")

    call_tool("cortex_query", args)
    assert calls["n"] == 2, "edited source changes the fingerprint, so this must be a cache miss"


# --- (c) a cache hit is byte-identical to a fresh computation ---


def test_cached_payload_is_byte_identical_to_fresh_recompute(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing"}

    first = call_tool("cortex_query", args)  # cache miss -> fresh compute, writes cache
    second = call_tool("cortex_query", args)  # cache hit
    assert first == second

    # Confirm the hit matches a genuinely independent fresh computation too,
    # not just the entry it happened to write.
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "0")
    third = call_tool("cortex_query", args)  # caching disabled -> guaranteed fresh
    assert third == second


def test_cached_impact_and_overview_are_byte_identical(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    impact_args = {"repo_path": str(repo), "path": "auth.py"}
    first_impact = call_tool("cortex_impact", impact_args)
    second_impact = call_tool("cortex_impact", impact_args)
    assert first_impact == second_impact

    overview_args = {"repo_path": str(repo)}
    first_overview = call_tool("cortex_overview", overview_args)
    second_overview = call_tool("cortex_overview", overview_args)
    assert first_overview == second_overview


# --- (d) CORTEX_QUERY_CACHE=0 disables both read and write ---


def test_env_kill_switch_disables_caching(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "0")
    calls = _count_pagerank_calls(monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing"}

    call_tool("cortex_query", args)
    call_tool("cortex_query", args)

    assert calls["n"] == 2, "kill-switch must force recompute on every call"

    store = CortexStore(default_db_path(repo))
    row = store.connection.execute("SELECT COUNT(*) AS n FROM query_cache").fetchone()
    assert row["n"] == 0, "kill-switch must also prevent writes"


# --- ledger interaction: a cache hit still records tool_usage ---


def test_cache_hit_still_records_ledger_row(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    args = {"repo_path": str(repo), "task": "login billing"}

    call_tool("cortex_query", args)  # miss
    call_tool("cortex_query", args)  # hit

    rows = store.fetch_tool_usage(repo)
    assert len(rows) == 2, "both the miss and the cache hit must record a ledger row"
    assert all(row["tool"] == "cortex_query" for row in rows)
    assert all(row["response_tokens"] > 0 for row in rows)


# --- (e) cortex gc prunes old/excess cache rows ---


def test_prune_query_cache_removes_old_and_excess_rows(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    repo_key = str(repo.resolve())

    now = int(time.time())
    store.connection.execute(
        "INSERT INTO query_cache(repo_path, cache_key, created_at, payload_json) VALUES (?, ?, ?, ?)",
        (repo_key, "old", now - 40 * 86400, "{}"),
    )
    store.connection.commit()
    for i in range(5):
        store.set_query_cache(repo, f"k{i}", "{}")

    deleted = store.prune_query_cache(repo, max_age_days=30, max_rows=3)

    remaining = store.connection.execute(
        "SELECT cache_key FROM query_cache WHERE repo_path = ?", (repo_key,)
    ).fetchall()
    remaining_keys = {row["cache_key"] for row in remaining}

    assert deleted == 3  # 1 aged-out row + 2 oldest of the excess-over-3
    assert len(remaining_keys) == 3
    assert "old" not in remaining_keys


def test_cli_gc_prunes_query_cache_for_active_repos(tmp_path, monkeypatch, capsys):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    repo_key = str(repo.resolve())

    old_time = int(time.time()) - 40 * 86400
    store.connection.execute(
        "INSERT INTO query_cache(repo_path, cache_key, created_at, payload_json) VALUES (?, ?, ?, ?)",
        (repo_key, "stale-entry", old_time, "{}"),
    )
    store.connection.commit()

    monkeypatch.setattr("sys.argv", ["cortex", "gc"])
    main()

    out = json.loads(capsys.readouterr().out)
    pruned = out["query_cache"]["pruned"]
    assert pruned == [{"repo_path": repo_key, "rows_deleted": 1}]

    remaining = store.connection.execute(
        "SELECT COUNT(*) AS n FROM query_cache WHERE repo_path = ?", (repo_key,)
    ).fetchone()
    assert remaining["n"] == 0


def test_cli_gc_is_a_noop_when_no_cache_rows_are_stale(tmp_path, monkeypatch, capsys):
    _repo_with_index(tmp_path, monkeypatch)

    monkeypatch.setattr("sys.argv", ["cortex", "gc"])
    main()

    out = json.loads(capsys.readouterr().out)
    assert out["query_cache"]["pruned"] == []
