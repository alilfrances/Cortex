from __future__ import annotations

import json
import subprocess
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
        "soft_deadline_count",
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
        "total_soft_deadline",
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


def test_soft_deadline_is_counted_separately_from_a_hard_one() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_dead_code", "1") as status:
        status.soft_deadline = True
    with registry.track("cortex_dead_code", "2"):
        pass
    snapshot = registry.snapshot()
    assert snapshot["total_soft_deadline"] == 1
    assert snapshot["total_deadline_exceeded"] == 0
    assert snapshot["per_tool"]["cortex_dead_code"]["soft_deadline_count"] == 1
    assert snapshot["per_tool"]["cortex_dead_code"]["call_count"] == 2


def test_in_flight_progress_is_reported_only_once_set() -> None:
    registry = StatusRegistry()
    with registry.track("cortex_refresh", "7") as status:
        assert "progress" not in registry.snapshot()["in_flight"][0]
        status.set_progress("parse", 50, 200)
        assert registry.snapshot()["in_flight"][0]["progress"] == {"stage": "parse", "done": 50, "total": 200}


def test_status_and_server_info_report_the_package_version() -> None:
    import cortex
    from cortex.mcp.server import SERVER_INFO
    from cortex.mcp.tools import call_tool

    payload = json.loads(call_tool("cortex_status", {})["content"][0]["text"])

    assert payload["version"] == cortex.__version__ == SERVER_INFO["version"]


def _count_edge(index: int, relation: str, target: str, source_file: str = "a.cpp", **metadata):
    from cortex.models import GraphEdge

    return GraphEdge(
        edge_id=f"e{index}", source="file:a.cpp", target=target, relation=relation, layer="STRUCTURAL",
        confidence="EXTRACTED", metadata={"lineno": 1, "source_file": source_file, **metadata},
    )


def _resolution_of(tmp_path, monkeypatch, edges, symbols=()):
    from cortex.mcp.tools import call_tool
    from cortex.models import GraphNode
    from cortex.store import CortexStore, default_db_path

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    repo = repo.resolve()
    store = CortexStore(default_db_path(repo))
    node = GraphNode(node_id="file:a.cpp", kind="file", label="a.cpp", source_ref="a.cpp", granularity="file")
    nodes = [node] + [
        GraphNode(node_id=f"symbol:a.cpp:{label}", kind=kind, label=label, source_ref="a.cpp", granularity="symbol")
        for kind, label in symbols
    ]
    store.replace_graph(repo, nodes, edges)
    store.connection.close()
    result = call_tool("cortex_status", {"repo_path": str(repo), "response_format": "detailed"})
    return json.loads(result["content"][0]["text"])["resolution"]


def test_resolution_counts_split_external_names_and_report_the_in_repo_share(tmp_path, monkeypatch) -> None:
    edges = [_count_edge(0, "inherits", "symbol:a.cpp:Base"), _count_edge(1, "inherits", "symbol:a.cpp:Base2")]
    edges += [_count_edge(2, "inherits", "name:Missing"), _count_edge(3, "inherits", "module:Mod")]
    edges += [_count_edge(4, "inherits", "name:QObject", external="qt"), _count_edge(5, "inherits", "module:Q", external="qml_import")]
    edges += [_count_edge(6, "calls", "symbol:a.cpp:f"), _count_edge(7, "calls", "name:g"), _count_edge(8, "calls", "name:h")]
    edges += [_count_edge(9, "calls", "name:qWarning", external="qt_macro"), _count_edge(10, "calls", "external:qml:X")]

    resolution = _resolution_of(tmp_path, monkeypatch, edges)

    assert resolution["inherits"] == {
        "symbol": 2, "name": 2, "module": 2, "name_external": 2, "in_repo_symbol_share": 0.5,
    }
    calls = resolution["calls"]
    assert {key: calls[key] for key in ("symbol", "name", "external", "name_external")} == {
        "symbol": 1, "name": 3, "external": 1, "name_external": 1,
    }
    assert calls["in_repo_symbol_share"] == round(1 / 3, 4)


def test_resolution_share_is_null_when_nothing_is_in_repo(tmp_path, monkeypatch) -> None:
    edges = [_count_edge(0, "inherits", "name:QObject", external="qt"), _count_edge(1, "calls", "name:qWarning", external="qt_macro")]

    resolution = _resolution_of(tmp_path, monkeypatch, edges)

    assert resolution["inherits"]["in_repo_symbol_share"] is None
    assert resolution["calls"]["in_repo_symbol_share"] is None
    assert resolution["inherits"]["name_external"] == resolution["calls"]["name_external"] == 1


def test_resolution_share_is_null_and_counts_are_zero_without_edges(tmp_path, monkeypatch) -> None:
    resolution = _resolution_of(tmp_path, monkeypatch, [])

    assert resolution["calls"]["in_repo_symbol_share"] is None and resolution["inherits"]["in_repo_symbol_share"] is None
    assert resolution["calls"]["name_external"] == resolution["inherits"]["name_external"] == 0


def test_resolution_counts_unresolved_calls_per_language_family(tmp_path, monkeypatch) -> None:
    edges = [
        _count_edge(0, "calls", "name:a", "x.cpp"), _count_edge(1, "calls", "name:b", "x.h"),
        _count_edge(2, "calls", "name:c", "y.ts"), _count_edge(3, "calls", "name:d", "z.py"),
        _count_edge(4, "calls", "name:e", "Z.java"), _count_edge(5, "calls", "name:f", "q.rs"),
        _count_edge(6, "calls", "symbol:x.cpp:g", "x.cpp"),
    ]

    by_family = _resolution_of(tmp_path, monkeypatch, edges)["calls"]["name_by_family"]

    assert by_family == {"c": 2, "js": 1, "java": 1, "py": 1, "rs": 1}


def _call_edge(index: int, callee: str, source_file: str = "a.cpp", **metadata):
    metadata = {"callee": callee, "receiver_kind": "none", "scope": None, "receiver_type": None, **metadata}
    return _count_edge(index, "calls", f"name:{callee}", source_file, **metadata)


def test_calls_to_names_no_repository_symbol_has_count_as_not_in_repo(tmp_path, monkeypatch) -> None:
    symbols = [("func", "helper"), ("func", "update"), ("class", "Foo")]
    edges = [
        _call_edge(0, "qDebug"),                                           # nothing defines it: not in repo
        _call_edge(1, "qMax"),                                             # likewise
        _call_edge(2, "helper"),                                           # a repository function: unresolved but resolvable
        _call_edge(3, "update"),                                           # named like a repository method
        _call_edge(4, "qDebug", receiver_kind="var", receiver="obj"),      # has a receiver
        _call_edge(5, "qDebug", receiver_kind="scoped", scope="Ns"),       # has a scope
        _call_edge(6, "qDebug", receiver_type="Foo"),                      # has a receiver type
        _call_edge(7, "qDebug", ambiguous=True),                           # kept ambiguous on purpose
        _call_edge(8, "qDebug", external="qt_macro"),                      # already external
        _count_edge(9, "calls", "symbol:a.cpp:helper", callee="helper", receiver_kind="none"),
    ]

    calls = _resolution_of(tmp_path, monkeypatch, edges, symbols)["calls"]

    assert calls["name"] == 9 and calls["symbol"] == 1 and calls["name_external"] == 1
    assert calls["name_not_in_repo"] == 2


def test_a_call_without_receiver_kind_or_callee_metadata_reads_the_target_label(tmp_path, monkeypatch) -> None:
    edges = [_count_edge(0, "calls", "name:qDebug"), _count_edge(1, "calls", "name:helper")]

    calls = _resolution_of(tmp_path, monkeypatch, edges, [("func", "helper")])["calls"]

    assert calls["name_not_in_repo"] == 1


def test_resolvable_share_leaves_out_external_and_not_in_repo_calls(tmp_path, monkeypatch) -> None:
    edges = [_count_edge(0, "calls", "symbol:a.cpp:helper", callee="helper", receiver_kind="none")]
    edges += [_call_edge(1, "helper"), _call_edge(2, "qDebug"), _call_edge(3, "qMax"), _call_edge(4, "qWarning", external="qt_macro")]
    edges += [_call_edge(5, "helper", receiver_kind="var", receiver="o")]

    calls = _resolution_of(tmp_path, monkeypatch, edges, [("func", "helper")])["calls"]

    # symbol 1, name 5, external-marked 1, not in repo 2: 1 / (1 + 5 - 1 - 2)
    assert (calls["symbol"], calls["name"], calls["name_external"], calls["name_not_in_repo"]) == (1, 5, 1, 2)
    assert calls["resolvable_share"] == round(1 / 3, 4)
    assert calls["in_repo_symbol_share"] == round(1 / 5, 4)  # unchanged: symbol / (symbol + name - name_external)


def test_resolvable_share_is_null_when_nothing_can_resolve(tmp_path, monkeypatch) -> None:
    resolution = _resolution_of(tmp_path, monkeypatch, [_call_edge(0, "qDebug")])

    assert resolution["calls"]["name_not_in_repo"] == 1
    assert resolution["calls"]["resolvable_share"] is None
    assert "name_not_in_repo" not in resolution["inherits"]


def test_resolution_counts_the_calls_share_per_language_family(tmp_path, monkeypatch) -> None:
    symbols = [("func", "helper"), ("func", "render")]
    edges = [
        _count_edge(0, "calls", "symbol:a.cpp:helper", "x.cpp", callee="helper", receiver_kind="none"),
        _count_edge(1, "calls", "symbol:a.cpp:helper", "x.cpp", callee="helper", receiver_kind="none"),
        _call_edge(2, "helper", "x.cpp"),                          # resolvable
        _call_edge(3, "qDebug", "x.cpp"),                          # not in repo
        _call_edge(4, "qWarning", "y.h", external="qt_macro"),     # external
        _count_edge(5, "calls", "symbol:a.cpp:render", "app.js", callee="render", receiver_kind="none"),
        _call_edge(6, "fetch", "app.js"),                          # not in repo
        _call_edge(7, "fetch", "app.js"),
        _call_edge(8, "render", "app.js", receiver_kind="var", receiver="o"),
        _call_edge(9, "log", "Main.java", receiver_kind="var", receiver="out"),
    ]

    calls = _resolution_of(tmp_path, monkeypatch, edges, symbols)["calls"]
    by_family = calls["by_family"]

    assert by_family == {
        "c": {"symbol": 2, "name": 3, "name_external": 1, "name_not_in_repo": 1, "resolvable_share": round(2 / 3, 4)},
        "java": {"symbol": 0, "name": 1, "name_external": 0, "name_not_in_repo": 0, "resolvable_share": 0.0},
        "js": {"symbol": 1, "name": 3, "name_external": 0, "name_not_in_repo": 2, "resolvable_share": 0.5},
    }
    for key in ("symbol", "name", "name_external", "name_not_in_repo"):
        assert sum(family[key] for family in by_family.values()) == calls[key]
    assert calls["resolvable_share"] == round(3 / (3 + 7 - 1 - 3), 4)
    assert calls["name_by_family"] == {"c": 3, "js": 3, "java": 1, "py": 0}
