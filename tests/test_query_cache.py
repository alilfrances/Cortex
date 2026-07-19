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


def test_hotspot_boost_is_part_of_query_cache_key(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = _count_pagerank_calls(monkeypatch)
    base = {"repo_path": str(repo), "task": "login billing", "hotspot_boost": False}

    call_tool("cortex_query", base)
    call_tool("cortex_query", {**base, "hotspot_boost": True})
    assert calls["n"] == 2, "hotspot_boost changes ranking policy and must miss the off-mode cache entry"

    call_tool("cortex_query", {**base, "hotspot_boost": True})
    assert calls["n"] == 2, "repeating the same hotspot_boost mode must hit its own cache entry"

    store = CortexStore(default_db_path(repo))
    rows = store.connection.execute(
        "SELECT COUNT(*) AS n FROM query_cache WHERE repo_path = ?",
        (str(repo.resolve()),),
    ).fetchone()
    assert rows["n"] == 2
    assert mcp_tools._cache_key("fingerprint", "cortex_query", base) != mcp_tools._cache_key(
        "fingerprint", "cortex_query", {**base, "hotspot_boost": True}
    )


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
    original = mcp_tools.build_report_data

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "build_report_data", _counting)
    args = {"repo_path": str(repo)}

    call_tool("cortex_overview", args)
    # Overview now caches analysis separately from presentation. The ledger's
    # detailed baseline re-render reuses that analysis rather than rebuilding
    # it, even though it has a different response format.
    after_first = calls["n"]
    assert after_first == 1

    call_tool("cortex_overview", args)
    assert calls["n"] == after_first, (
        "second identical cortex_overview call must reuse cached analysis"
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


# --- (c) a cache hit's DATA is byte-identical to a fresh computation ---
#
# P1-5 changed what "byte-identical" means here. `_meta` (index age,
# `cached` flag, folded-in `saved_tokens`) is now rebuilt fresh on every
# call -- including a cache hit -- specifically so a hit's `_meta` is never
# a stale replay of whatever was true when the entry was written (the P1-3
# caveat P1-5 closes; see mcp/tools._cache_get's docstring). That means a
# miss and a hit are expected to differ in `_meta` (at minimum, `cached`),
# so these tests now compare the payload with `_meta` excluded via
# `mcp_tools._without_meta`, and separately assert the `_meta.cached` flag
# itself on each side.


def test_non_object_query_cache_payload_degrades_to_recompute(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    args = {"repo_path": str(repo), "task": "login billing"}
    cache_key = mcp_tools._cache_key(
        store.get_repo_fingerprint(repo),
        "cortex_query",
        args,
    )
    store.set_query_cache(repo, cache_key, "[]")

    result = call_tool("cortex_query", args)
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]
    assert "token_stats" in payload


def test_legacy_query_cache_payload_backfills_token_stats(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    for response_format, why in (
        ("concise", "keyword: login"),
        ("detailed", [{"type": "keyword", "terms": ["login"]}]),
    ):
        args = {
            "repo_path": str(repo),
            "task": "legacy cached login",
            "response_format": response_format,
        }
        cache_key = mcp_tools._cache_key(
            store.get_repo_fingerprint(repo),
            "cortex_query",
            args,
        )
        legacy_payload = {
            "task": args["task"],
            "repo_path": str(repo),
            "budget": 4000,
            "total_tokens": 12,
            "confidence_notes": [],
            "open_questions": [],
            "items": [
                {
                    "path": "auth.py",
                    "kind": "code",
                    "token_count": 12,
                    "content": "def login(): pass",
                    "why": why,
                }
            ],
        }
        store.set_query_cache(repo, cache_key, json.dumps(legacy_payload))

        payload = _payload(call_tool("cortex_query", args))

        assert payload["token_stats"] == {
            "budget": 4000,
            "returned_tokens": 12,
            "matched_tokens": 12,
            "matched_ratio": 1.0,
        }
        cached = store.get_query_cache(repo, cache_key)
        assert cached is not None
        assert json.loads(cached)["token_stats"] == payload["token_stats"]


def test_cached_payload_is_byte_identical_to_fresh_recompute(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing"}

    first = _payload(call_tool("cortex_query", args))  # cache miss -> fresh compute, writes cache
    second = _payload(call_tool("cortex_query", args))  # cache hit

    assert mcp_tools._without_meta(first) == mcp_tools._without_meta(second)
    assert first.get("_meta", {}).get("cached") is not True
    assert second["_meta"]["cached"] is True

    # Confirm the hit matches a genuinely independent fresh computation too,
    # not just the entry it happened to write.
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "0")
    third = _payload(call_tool("cortex_query", args))  # caching disabled -> guaranteed fresh
    assert mcp_tools._without_meta(third) == mcp_tools._without_meta(second)
    assert third.get("_meta", {}).get("cached") is not True


def test_cached_impact_and_overview_are_byte_identical(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    impact_args = {"repo_path": str(repo), "path": "auth.py"}
    first_impact = _payload(call_tool("cortex_impact", impact_args))
    second_impact = _payload(call_tool("cortex_impact", impact_args))
    assert mcp_tools._without_meta(first_impact) == mcp_tools._without_meta(second_impact)
    assert second_impact["_meta"]["cached"] is True

    overview_args = {"repo_path": str(repo)}
    first_overview = _payload(call_tool("cortex_overview", overview_args))
    second_overview = _payload(call_tool("cortex_overview", overview_args))
    assert mcp_tools._without_meta(first_overview) == mcp_tools._without_meta(second_overview)
    assert second_overview["_meta"]["cached"] is True


# --- (c2) P1-5 fix: a cache hit shows the CURRENT index age, never a
# frozen write-time snapshot or a replayed auto_refreshed block ---


def test_cache_hit_shows_current_index_age_not_frozen_write_time_meta(tmp_path, monkeypatch):
    """This is the P1-3 caveat P1-5 was written to close: previously the
    cache stored the fully status-merged result, so a hit echoed whatever
    `auto_refreshed`/index-age snapshot was true when the entry was first
    written. `_meta` must now be assembled fresh after every cache lookup,
    so a hit's `index_age_seconds` reflects the CURRENT wall clock (even
    though the underlying repo/index content -- and therefore the cache
    key -- hasn't changed) and never carries a stale `auto_refreshed`.
    """
    repo = _repo_with_index(tmp_path, monkeypatch)
    args = {"repo_path": str(repo), "task": "login billing", "response_format": "detailed"}

    fake_now = {"t": time.time()}
    monkeypatch.setattr(time, "time", lambda: fake_now["t"])

    first = _payload(call_tool("cortex_query", args))  # cache miss
    assert first["_meta"]["cached"] is not True
    assert "auto_refreshed" not in first["_meta"]
    first_age = first["_meta"]["index_age_seconds"]

    fake_now["t"] += 120  # advance the clock; nothing about the repo/index changes

    second = _payload(call_tool("cortex_query", args))  # cache hit, same fingerprint
    assert second["_meta"]["cached"] is True
    assert "auto_refreshed" not in second["_meta"]
    assert second["_meta"]["index_age_seconds"] == first_age + 120, (
        "a cache hit must report the CURRENT index age, not the age frozen "
        "at write time"
    )


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
