"""How often an MCP tool call computes ``compute_repo_fingerprint``.

``cortex.mcp.tools._staleness`` needs the working-tree fingerprint (``git ls-files``
plus a ``stat()`` of every candidate file) on every read-tool call, cache hits
included.

History of the counts pinned here:

* **7b0e6e0 (Task 0 baseline).**  Fresh index: ``cortex_query``, ``cortex_read_file``,
  ``cortex_impact`` (and the other tools) computed exactly once, but
  ``cortex_search_symbols``, ``cortex_overview``, ``cortex_relations`` and
  ``cortex_path`` computed **twice** -- the token-savings ledger
  (``_record_tool_usage`` -> ``_estimate_baseline``) re-enters
  ``_call_search``/``_call_relations``/``_call_overview``, each of which runs its own
  ``_ensure_fresh``.  A repeated (cache-hit) call computed once again.  Stale index
  with auto-refresh: 2 (before + after the ingest), 3 for ``cortex_search_symbols``.
* **Task 1 (ingest lock).**  The post-lock re-check added one computation to the
  stale path (3, and 4 for ``cortex_search_symbols``).
* **Task 2 (TTL memo, this file's current numbers).**  ``_staleness`` memoizes the
  working-tree fingerprint per repo for ``CORTEX_FINGERPRINT_TTL_SECONDS`` (default
  2.0; ``0`` disables).  Fresh index: exactly **1** per call for every tool (the
  ledger re-entry hits the memo); a repeat within the TTL computes **0**.  Stale index:
  **2** (the first look, plus the post-lock re-check, which always bypasses the memo);
  the post-ingest status reuses the fingerprint the ingest just stored.
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

# Fresh index, first call (empty memo): fingerprint computations per call_tool.
# At 7b0e6e0 overview/path/relations/search_symbols were 2 (ledger re-entry).
FRESH_COUNTS = {name: 1 for name in CALLS}

# Tools that never touch the fingerprint.
NO_FINGERPRINT = ("cortex_status", "cortex_refresh")

# Stale index + auto-refresh: the first look (may be memoized) plus the post-lock
# re-check (never memoized).  7b0e6e0: 2/2/2/3; Task 1: 3/3/3/4.
STALE_AUTO_REFRESH_COUNTS = {
    "cortex_query": 2,
    "cortex_impact": 2,
    "cortex_read_file": 2,
    "cortex_search_symbols": 2,
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
    # A long TTL keeps the count assertions independent of machine speed; the
    # clock-driven tests below pin the 2 s default explicitly.
    monkeypatch.setenv("CORTEX_FINGERPRINT_TTL_SECONDS", "60")
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
def test_repeat_within_ttl_reuses_the_memoized_fingerprint(counter: dict[str, int], qt_repo: Path, name: str) -> None:
    """7b0e6e0: (1, 1).  Task 2: the repeat inside the TTL computes nothing."""
    first_count, first = _call(counter, qt_repo, name)
    second_count, second = _call(counter, qt_repo, name)

    assert (first_count, second_count) == (1, 0)
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
    # 7b0e6e0: 2 then 1.  Task 1: 3 then 1.  Task 2: the ingest seeds the memo.
    assert stale_count == 2
    assert "auto_refreshed" not in fresh["_meta"]
    assert fresh["stale"] is False and fresh_count == 0


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


# --------------------------------------------------------------------------- #
# Task 2: the TTL memo itself
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch, qt_repo: Path) -> _Clock:
    monkeypatch.setenv("CORTEX_FINGERPRINT_TTL_SECONDS", "2")  # the shipped default
    fake = _Clock()
    monkeypatch.setattr(tools.time, "monotonic", fake)
    return fake


def _store(repo: Path) -> Any:
    return tools.CortexStore(tools.default_db_path(repo))


def test_ttl_default_env_parsing_and_status_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORTEX_FINGERPRINT_TTL_SECONDS", raising=False)
    assert tools.fingerprint_ttl_seconds() == 2.0
    for raw, expected in (("0", 0.0), ("0.5", 0.5), ("-3", 0.0), ("bogus", 2.0), ("", 2.0), ("nan", 2.0), ("inf", 2.0)):
        assert tools.fingerprint_ttl_seconds({"CORTEX_FINGERPRINT_TTL_SECONDS": raw}) == expected, raw
    monkeypatch.setenv("CORTEX_FINGERPRINT_TTL_SECONDS", "7")
    status = json.loads(tools.call_tool("cortex_status", {})["content"][0]["text"])
    assert status["fingerprint_ttl_seconds"] == 7.0


def test_back_to_back_staleness_computes_once_then_again_after_ttl(
    counter: dict[str, int], qt_repo: Path, clock: _Clock
) -> None:
    store, root = _store(qt_repo), qt_repo.resolve()
    counter["n"] = 0

    tools._staleness(store, root)
    tools._staleness(store, root)
    assert counter["n"] == 1

    clock.now += 1.99
    tools._staleness(store, root)
    assert counter["n"] == 1

    clock.now += 0.02  # 2.01 s after the computation: expired
    tools._staleness(store, root)
    assert counter["n"] == 2


def test_edit_inside_ttl_is_seen_after_expiry(counter: dict[str, int], qt_repo: Path, clock: _Clock) -> None:
    """The documented tradeoff: an edit inside the window shows up one TTL later."""
    store, root = _store(qt_repo), qt_repo.resolve()
    assert tools._staleness(store, root)["stale"] is False
    path = qt_repo / "src/DeviceModel.cpp"
    path.write_text(path.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")

    assert tools._staleness(store, root)["stale"] is False  # memoized
    clock.now += 2.5
    assert tools._staleness(store, root)["stale"] is True


def test_bypass_recomputes_and_refreshes_the_memo(counter: dict[str, int], qt_repo: Path, clock: _Clock) -> None:
    store, root = _store(qt_repo), qt_repo.resolve()
    tools._staleness(store, root)
    counter["n"] = 0

    tools._staleness(store, root, bypass_ttl=True)
    tools._staleness(store, root)
    assert counter["n"] == 1


def test_refresh_reseeds_the_memo_with_the_new_fingerprint(
    counter: dict[str, int], qt_repo: Path, clock: _Clock
) -> None:
    store, root = _store(qt_repo), qt_repo.resolve()
    before = tools._staleness(store, root)["current_fingerprint"]
    path = qt_repo / "src/DeviceModel.cpp"
    path.write_text(path.read_text(encoding="utf-8") + "\n// edited\n", encoding="utf-8")

    _call(counter, qt_repo, "cortex_refresh")
    counter["n"] = 0
    after = tools._staleness(store, root)

    assert counter["n"] == 0  # served from the memo seeded by the refresh
    assert after["current_fingerprint"] == after["fingerprint"] != before
    assert after["stale"] is False


def test_ttl_zero_recomputes_every_call(
    counter: dict[str, int], qt_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORTEX_FINGERPRINT_TTL_SECONDS", "0")

    counts = [_call(counter, qt_repo, "cortex_query")[0] for _ in range(3)]

    # cortex_search_symbols-style ledger re-entry is back to 2 with the memo off.
    assert counts == [1, 1, 1]
    assert _call(counter, qt_repo, "cortex_search_symbols")[0] == 2
    assert tools._FINGERPRINT_MEMO.get(str(qt_repo.resolve())) is None


def test_memo_is_keyed_per_repo(
    counter: dict[str, int], qt_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = _build_qt_app(tmp_path / "other")
    ingest(other)

    counts = [_call(counter, repo, "cortex_query")[0] for repo in (qt_repo, other, qt_repo, other)]

    assert counts == [1, 1, 0, 0]
    assert {str(qt_repo.resolve()), str(other.resolve())} <= set(tools._FINGERPRINT_MEMO)
