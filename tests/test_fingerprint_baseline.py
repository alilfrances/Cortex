"""How often an MCP tool call computes ``compute_repo_fingerprint`` at 7b0e6e0.

``cortex.mcp.tools._staleness`` calls ``compute_repo_fingerprint`` (``git ls-files`` plus a
``stat()`` of every candidate file) on every read-tool call, cache hits included.
Task 2 memoises it with a short TTL and **must update the counts below** (keep the
old numbers in its docstring).

Counts below are observed values, not the plan's assumption.  The plan said one call
per ``call_tool`` for ``cortex_query``/``cortex_search_symbols``/``cortex_read_file``/
``cortex_impact``.  Measured: ``cortex_query``, ``cortex_read_file`` and
``cortex_impact`` do compute exactly once, but ``cortex_search_symbols`` computes
**twice** -- as do ``cortex_overview``, ``cortex_relations`` and ``cortex_path``.  The
second call comes from the token-savings ledger: ``_record_tool_usage`` →
``_estimate_baseline`` re-enters ``_call_search``/``_call_relations``/``_call_overview``
directly, and each of those runs its own ``_ensure_fresh``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import cortex.mcp.tools as tools
from evals.run_evals import _build_qt_app
from tests.baseline_helpers import fixed_git_env, ingest
from tests.golden_utils import force_stdlib_tokenizer

DEVICE_MANAGER = "src/DeviceManager.cpp"

CALLS: dict[str, dict[str, Any]] = {
    "cortex_query": {"task": "deviceConnected signal"},
    "cortex_overview": {},
    "cortex_context": {"targets": [DEVICE_MANAGER]},
    "cortex_impact": {"path": DEVICE_MANAGER},
    "cortex_risk": {},
    "cortex_dead_code": {},
    "cortex_search_symbols": {"query": "DeviceManager"},
    "cortex_read_symbol": {"symbol": "DeviceManager"},
    "cortex_read_file": {"path": DEVICE_MANAGER},
    "cortex_relations": {"relation": "connects"},
    "cortex_references": {"symbol": "deviceConnected"},
    "cortex_search_text": {"query": "deviceConnected"},
    "cortex_path": {
        "symbol_a": "symbol:include/DeviceManager.hpp:deviceConnected",
        "symbol_b": "symbol:include/DeviceModel.hpp:onDeviceConnected",
    },
}

# Fresh index: fingerprint computations per call_tool (identical for cache hits).
FRESH_COUNTS = {
    "cortex_query": 1,
    "cortex_overview": 2,
    "cortex_context": 1,
    "cortex_impact": 1,
    "cortex_risk": 1,
    "cortex_dead_code": 1,
    "cortex_search_symbols": 2,
    "cortex_read_symbol": 1,
    "cortex_read_file": 1,
    "cortex_relations": 2,
    "cortex_references": 1,
    "cortex_search_text": 1,
    "cortex_path": 2,
}

# Tools that never touch the fingerprint.
NO_FINGERPRINT = ("cortex_status", "cortex_refresh")

# Stale index + auto-refresh: one computation before the ingest, one re-check after
# taking the cross-process ingest lock (Task 1), one after the ingest, plus whatever
# the fresh path costs for the ledger shadow call (observed).
# At 7b0e6e0 (before the Task 1 lock re-check) these were 2/2/2/3.
STALE_AUTO_REFRESH_COUNTS = {
    "cortex_query": 3,
    "cortex_impact": 3,
    "cortex_read_file": 3,
    "cortex_search_symbols": 4,
}


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state = {"n": 0}
    original = tools.compute_repo_fingerprint

    def counting(*args: Any, **kwargs: Any) -> str:
        state["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(tools, "compute_repo_fingerprint", counting)
    return state


@pytest.fixture
def qt_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    force_stdlib_tokenizer(monkeypatch)
    fixed_git_env(monkeypatch)
    repo = _build_qt_app(tmp_path)
    ingest(repo)
    return repo


def _call(counter: dict[str, int], repo: Path, name: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    counter["n"] = 0
    arguments = {"repo_path": str(repo), **CALLS.get(name, {}), **extra}
    result = tools.call_tool(name, arguments)
    assert not result["isError"], result["content"][0]["text"]
    return counter["n"], json.loads(result["content"][0]["text"])


def test_every_read_tool_is_covered() -> None:
    names = {d["name"] for d in tools.TOOL_DEFINITIONS}
    assert names == set(CALLS) | set(NO_FINGERPRINT)
    assert set(FRESH_COUNTS) == set(CALLS)


@pytest.mark.parametrize("response_format", ["concise", "detailed"])
@pytest.mark.parametrize("name", sorted(FRESH_COUNTS))
def test_fresh_index_fingerprint_calls_per_tool_call(
    counter: dict[str, int], qt_repo: Path, name: str, response_format: str
) -> None:
    count, payload = _call(counter, qt_repo, name, response_format=response_format)

    assert count == FRESH_COUNTS[name]
    assert payload.get("stale") is False


@pytest.mark.parametrize("name", ["cortex_query", "cortex_impact"])
def test_cache_hit_still_computes_fingerprint_once(counter: dict[str, int], qt_repo: Path, name: str) -> None:
    first_count, first = _call(counter, qt_repo, name)
    second_count, second = _call(counter, qt_repo, name)

    assert (first_count, second_count) == (1, 1)
    assert "_meta" not in first or first["_meta"].get("cached") is False
    assert second["_meta"]["cached"] is True  # proves the second call was served from cache


@pytest.mark.parametrize("name", sorted(NO_FINGERPRINT))
def test_status_and_refresh_do_not_compute_the_fingerprint(counter: dict[str, int], qt_repo: Path, name: str) -> None:
    count, _ = _call(counter, qt_repo, name)

    assert count == 0


@pytest.mark.parametrize("name", sorted(STALE_AUTO_REFRESH_COUNTS))
def test_stale_index_auto_refreshes_with_a_second_computation(
    counter: dict[str, int], qt_repo: Path, name: str
) -> None:
    (qt_repo / "src/DeviceModel.cpp").write_text(
        (qt_repo / "src/DeviceModel.cpp").read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8"
    )

    count, payload = _call(counter, qt_repo, name)

    assert payload["stale"] is False
    assert payload["_meta"]["auto_refreshed"] == {
        "deleted_files": 0, "new_files": 0, "unchanged_files": 7, "updated_files": 1,
    }
    assert count == STALE_AUTO_REFRESH_COUNTS[name]


def test_stale_then_fresh_sequence(counter: dict[str, int], qt_repo: Path) -> None:
    """First call after an edit refreshes; the very next call sees a fresh index.

    The plan lists a "stale-then-fresh race" hole; the cross-process race itself belongs
    to Task 1 (ingest lock).  This pins the sequential contract it must preserve.
    """
    path = qt_repo / "src/DeviceModel.cpp"
    path.write_text(path.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")

    stale_count, stale = _call(counter, qt_repo, "cortex_impact")
    fresh_count, fresh = _call(counter, qt_repo, "cortex_impact")

    assert stale["_meta"]["auto_refreshed"]["updated_files"] == 1
    assert stale_count == 3  # 2 at 7b0e6e0; +1 post-lock re-check (Task 1)
    assert "auto_refreshed" not in fresh["_meta"]
    assert fresh["stale"] is False and fresh_count == 1


def test_auto_refresh_disabled_reports_stale_with_a_single_computation(
    counter: dict[str, int], qt_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    path = qt_repo / "src/DeviceModel.cpp"
    path.write_text(path.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")

    count, payload = _call(counter, qt_repo, "cortex_query")

    assert count == 1
    assert payload["stale"] is True
    assert payload["refresh_hint"] == "Call cortex_refresh to update the index."
    assert payload["_meta"]["fingerprint_fresh"] is False
    assert "auto_refreshed" not in payload["_meta"]
