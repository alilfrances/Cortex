from __future__ import annotations

import json
import threading
import time

import pytest

from cortex.deadline import DeadlineExceeded
from cortex.mcp.status import REGISTRY, RequestStatus, StatusRegistry


def test_registry_import_has_zero_side_effects() -> None:
    """REGISTRY exists at import time but starts with no in-flight work and
    no history -- importing the module must not itself register anything."""
    fresh = StatusRegistry()
    snapshot = fresh.snapshot()
    assert snapshot["in_flight_count"] == 0
    assert snapshot["in_flight"] == []
    assert snapshot["total_completed"] == 0
    assert snapshot["total_failed"] == 0
    assert snapshot["total_deadline_exceeded"] == 0
    assert snapshot["per_tool"] == {}
    assert snapshot["uptime_seconds"] >= 0


def test_module_level_registry_is_a_status_registry() -> None:
    assert isinstance(REGISTRY, StatusRegistry)


# ---------------------------------------------------------------------------
# track(): success path
# ---------------------------------------------------------------------------


def test_track_registers_while_in_flight_and_removes_on_success() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_query", "req-1") as status:
        assert isinstance(status, RequestStatus)
        assert status.tool_name == "cortex_query"
        assert status.request_id == "req-1"
        snap = registry.snapshot()
        assert snap["in_flight_count"] == 1
        assert snap["in_flight"][0]["tool_name"] == "cortex_query"
        assert snap["in_flight"][0]["request_id"] == "req-1"
        assert snap["in_flight"][0]["elapsed_seconds"] >= 0

    snap = registry.snapshot()
    assert snap["in_flight_count"] == 0
    assert snap["in_flight"] == []
    assert snap["total_completed"] == 1
    assert snap["total_failed"] == 0
    assert snap["per_tool"]["cortex_query"]["call_count"] == 1
    assert snap["per_tool"]["cortex_query"]["failure_count"] == 0


def test_request_status_elapsed_seconds_grows() -> None:
    status = RequestStatus("cortex_query", "req-x")
    first = status.elapsed_seconds
    time.sleep(0.01)
    second = status.elapsed_seconds
    assert second > first


# ---------------------------------------------------------------------------
# track(): exception path
# ---------------------------------------------------------------------------


def test_track_removes_in_flight_entry_on_exception() -> None:
    registry = StatusRegistry()
    with pytest.raises(RuntimeError):
        with registry.track("cortex_dead_code", "req-2"):
            assert registry.snapshot()["in_flight_count"] == 1
            raise RuntimeError("boom")

    snap = registry.snapshot()
    assert snap["in_flight_count"] == 0
    assert snap["total_completed"] == 1
    assert snap["total_failed"] == 1
    assert snap["per_tool"]["cortex_dead_code"]["failure_count"] == 1
    assert snap["total_deadline_exceeded"] == 0


def test_track_records_deadline_exceeded_distinctly() -> None:
    registry = StatusRegistry()
    with pytest.raises(DeadlineExceeded):
        with registry.track("cortex_overview", "req-3"):
            raise DeadlineExceeded(budget_seconds=1.0, elapsed_seconds=2.0, tool_name="cortex_overview")

    snap = registry.snapshot()
    assert snap["total_failed"] == 1
    assert snap["total_deadline_exceeded"] == 1
    assert snap["per_tool"]["cortex_overview"]["deadline_exceeded_count"] == 1
    assert snap["per_tool"]["cortex_overview"]["failure_count"] == 1


def test_track_multiple_requests_same_tool_track_independently() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_query", "a"):
        with registry.track("cortex_query", "b"):
            snap = registry.snapshot()
            assert snap["in_flight_count"] == 2
            ids = {entry["request_id"] for entry in snap["in_flight"]}
            assert ids == {"a", "b"}
        assert registry.snapshot()["in_flight_count"] == 1
    assert registry.snapshot()["in_flight_count"] == 0
    assert registry.snapshot()["per_tool"]["cortex_query"]["call_count"] == 2


# ---------------------------------------------------------------------------
# record_completed()
# ---------------------------------------------------------------------------


def test_record_completed_updates_counters_directly() -> None:
    registry = StatusRegistry()
    registry.record_completed("cortex_impact", 0.5, failed=False)
    registry.record_completed("cortex_impact", 1.5, failed=True)
    snap = registry.snapshot()
    assert snap["total_completed"] == 2
    assert snap["total_failed"] == 1
    aggregate = snap["per_tool"]["cortex_impact"]
    assert aggregate["call_count"] == 2
    assert aggregate["failure_count"] == 1
    assert aggregate["slowest_seconds"] == 1.5


def test_slowest_seconds_tracks_max_not_last() -> None:
    registry = StatusRegistry()
    registry.record_completed("cortex_relations", 3.0)
    registry.record_completed("cortex_relations", 1.0)
    registry.record_completed("cortex_relations", 2.0)
    aggregate = registry.snapshot()["per_tool"]["cortex_relations"]
    assert aggregate["slowest_seconds"] == 3.0
    assert aggregate["call_count"] == 3


def test_per_tool_aggregates_stay_bounded_regardless_of_call_volume() -> None:
    """Memory must stay O(distinct tool names), never O(number of calls)."""
    registry = StatusRegistry()
    for i in range(500):
        registry.record_completed("cortex_search_text", float(i) / 100)
    snap = registry.snapshot()
    assert snap["per_tool"]["cortex_search_text"]["call_count"] == 500
    # Exactly one aggregate dict for the one tool name used, no per-call list.
    assert len(snap["per_tool"]) == 1
    assert set(snap["per_tool"]["cortex_search_text"].keys()) == {
        "call_count",
        "failure_count",
        "deadline_exceeded_count",
        "slowest_seconds",
    }


# ---------------------------------------------------------------------------
# snapshot(): shape and serializability
# ---------------------------------------------------------------------------


def test_snapshot_is_json_serializable() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_query", "req-json"):
        snapshot = registry.snapshot()
        serialized = json.dumps(snapshot)
        assert "cortex_query" in serialized
        # Round-trips cleanly.
        restored = json.loads(serialized)
        assert restored["in_flight_count"] == 1


def test_snapshot_has_expected_top_level_keys() -> None:
    registry = StatusRegistry()
    snapshot = registry.snapshot()
    assert set(snapshot.keys()) == {
        "started_at",
        "uptime_seconds",
        "in_flight_count",
        "in_flight",
        "total_completed",
        "total_failed",
        "total_deadline_exceeded",
        "per_tool",
    }


def test_snapshot_uptime_increases_over_time() -> None:
    registry = StatusRegistry()
    first = registry.snapshot()["uptime_seconds"]
    time.sleep(0.02)
    second = registry.snapshot()["uptime_seconds"]
    assert second > first


def test_snapshot_in_flight_sorted_by_longest_elapsed_first() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_a", "first"):
        time.sleep(0.02)
        with registry.track("cortex_b", "second"):
            snap = registry.snapshot()
            assert [entry["request_id"] for entry in snap["in_flight"]] == ["first", "second"]


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_track_from_multiple_threads_is_safe() -> None:
    registry = StatusRegistry()
    iterations = 200
    thread_count = 8
    errors: list[BaseException] = []

    def worker(thread_index: int) -> None:
        try:
            for i in range(iterations):
                request_id = f"t{thread_index}-{i}"
                if i % 10 == 0:
                    try:
                        with registry.track("cortex_query", request_id):
                            raise ValueError("synthetic failure")
                    except ValueError:
                        pass
                else:
                    with registry.track("cortex_query", request_id):
                        pass
        except BaseException as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    snapshot = registry.snapshot()
    assert snapshot["in_flight_count"] == 0
    assert snapshot["total_completed"] == iterations * thread_count
    expected_failures = sum(1 for i in range(iterations) if i % 10 == 0) * thread_count
    assert snapshot["total_failed"] == expected_failures
    assert snapshot["per_tool"]["cortex_query"]["call_count"] == iterations * thread_count
    # Still just one aggregate bucket for the one tool name used.
    assert len(snapshot["per_tool"]) == 1
    # JSON-serializable even after heavy concurrent mutation.
    json.dumps(snapshot)


def test_concurrent_snapshot_reads_do_not_crash_while_writers_mutate() -> None:
    registry = StatusRegistry()
    stop = threading.Event()
    read_errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                snapshot = registry.snapshot()
                json.dumps(snapshot)
        except BaseException as exc:  # pragma: no cover - defensive
            read_errors.append(exc)

    def writer() -> None:
        for i in range(300):
            with registry.track("cortex_context", f"w-{i}"):
                pass

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    writer_threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in writer_threads:
        thread.start()
    for thread in writer_threads:
        thread.join(timeout=30)
    stop.set()
    reader_thread.join(timeout=10)

    assert not read_errors
    snapshot = registry.snapshot()
    assert snapshot["total_completed"] == 300 * 4
