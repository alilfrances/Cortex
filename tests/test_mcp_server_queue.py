from __future__ import annotations

import contextlib
import json
import os
import queue
import stat
import subprocess
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from cortex.deadline import Deadline, deadline_scope
from cortex.ingest import ingest_repository
from cortex.mcp import server
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import data_root

TIMEOUT = 10


class ControlledInput:
    """Line source the test feeds by hand.

    ``wait_consumed`` blocks until the reader has handled every pushed line and
    come back asking for the next one, which proves the previous line was fully
    processed without sleeping.
    """

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._cond = threading.Condition()
        self._pushed = 0
        self._fetches = 0

    def push(self, frame: dict[str, Any]) -> None:
        self.push_raw(json.dumps(frame))

    def push_raw(self, text: str) -> None:
        with self._cond:
            self._pushed += 1
        self._lines.put(text + "\n")

    def close(self) -> None:
        self._lines.put(None)

    def wait_consumed(self) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: self._fetches > self._pushed, TIMEOUT)

    def __iter__(self) -> ControlledInput:
        return self

    def __next__(self) -> str:
        with self._cond:
            self._fetches += 1
            self._cond.notify_all()
        line = self._lines.get(timeout=TIMEOUT)
        if line is None:
            raise StopIteration
        return line


class MemoryOutput:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._lines: list[str] = []

    def write(self, text: str) -> int:
        with self._cond:
            assert text.endswith("\n") and text.count("\n") == 1, "one full line per write"
            self._lines.append(text)
            self._cond.notify_all()
        return len(text)

    def flush(self) -> None:
        pass

    def responses(self) -> list[dict[str, Any]]:
        with self._cond:
            return [json.loads(line) for line in self._lines]

    def wait_for(self, count: int) -> list[dict[str, Any]]:
        with self._cond:
            assert self._cond.wait_for(lambda: len(self._lines) >= count, TIMEOUT)
            return [json.loads(line) for line in self._lines]


class FakeTools:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cancel_events: dict[str, threading.Event | None] = {}
        self.request_ids: dict[str, Any] = {}
        self.started: defaultdict[str, threading.Event] = defaultdict(threading.Event)
        self.gates: defaultdict[str, threading.Event] = defaultdict(threading.Event)
        self.blocking: set[str] = set()
        self.cancel_aware: set[str] = set()

    def __call__(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        cancel: threading.Event | None = None,
        request_id: Any = None,
    ) -> dict[str, Any]:
        self.calls.append(name)
        self.cancel_events[name] = cancel
        self.request_ids[name] = request_id
        self.started[name].set()
        if name in self.blocking:
            assert self.gates[name].wait(TIMEOUT)
        if name in self.cancel_aware:
            assert cancel is not None and cancel.wait(TIMEOUT)
        if name == "boom":
            raise RuntimeError("boom")
        return {"content": [{"type": "text", "text": name}]}


class RunningServer:
    def __init__(self) -> None:
        self.input = ControlledInput()
        self.output = MemoryOutput()
        self.exit_code: int | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.exit_code = server.serve(self.input, self.output)

    def call(self, request_id: Any, tool: str, arguments: dict[str, Any] | None = None) -> None:
        self.input.push(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        )

    def cancel(self, request_id: Any) -> None:
        self.input.push(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}
        )

    def finish(self) -> list[dict[str, Any]]:
        self.input.close()
        self._thread.join(TIMEOUT)
        assert not self._thread.is_alive()
        assert self.exit_code == 0
        return self.output.responses()


@pytest.fixture
def fake_tools(monkeypatch) -> FakeTools:
    fake = FakeTools()
    monkeypatch.setattr(server, "call_tool", fake)
    return fake


def _ids(responses: list[dict[str, Any]]) -> list[Any]:
    return [response["id"] for response in responses]


def test_request_cancelled_before_start_is_dropped_without_response(fake_tools):
    fake_tools.blocking.add("first")
    srv = RunningServer()
    srv.call(1, "first")
    assert fake_tools.started["first"].wait(TIMEOUT)
    srv.call(2, "doomed")
    srv.cancel(2)
    srv.input.wait_consumed()
    fake_tools.gates["first"].set()
    srv.call(3, "last")

    responses = srv.finish()

    assert _ids(responses) == [1, 3]
    assert fake_tools.calls == ["first", "last"]


def test_cancelling_running_request_sets_event_and_sends_no_response(fake_tools):
    fake_tools.cancel_aware.add("slow")
    srv = RunningServer()
    srv.call(1, "slow")
    assert fake_tools.started["slow"].wait(TIMEOUT)
    event = fake_tools.cancel_events["slow"]
    assert event is not None and not event.is_set()
    srv.cancel(1)
    srv.call(2, "next")

    responses = srv.finish()

    assert event.is_set()
    assert _ids(responses) == [2]


def test_cancel_key_distinguishes_int_and_string_ids(fake_tools):
    fake_tools.blocking.add("first")
    srv = RunningServer()
    srv.call(1, "first")
    assert fake_tools.started["first"].wait(TIMEOUT)
    srv.call("2", "string_id")
    srv.cancel(2)
    srv.input.wait_consumed()
    fake_tools.gates["first"].set()

    responses = srv.finish()

    assert _ids(responses) == [1, "2"]


def test_ping_is_answered_while_tool_call_is_blocked(fake_tools):
    fake_tools.blocking.add("slow")
    srv = RunningServer()
    srv.call(1, "slow")
    assert fake_tools.started["slow"].wait(TIMEOUT)
    srv.input.push({"jsonrpc": "2.0", "id": 9, "method": "ping"})

    responses = srv.output.wait_for(1)

    assert responses == [{"jsonrpc": "2.0", "id": 9, "result": {}}]
    assert not fake_tools.gates["slow"].is_set()
    fake_tools.gates["slow"].set()
    assert _ids(srv.finish()) == [9, 1]


def test_handler_exception_returns_internal_error_with_request_id(fake_tools):
    srv = RunningServer()
    srv.call(7, "boom")
    srv.call(8, "fine")

    responses = srv.finish()

    assert responses[0]["id"] == 7
    assert responses[0]["error"]["code"] == -32603
    assert _ids(responses) == [7, 8]
    assert "result" in responses[1]


def test_invalid_json_returns_parse_error_and_loop_continues(fake_tools):
    srv = RunningServer()
    srv.input.push_raw("{not json")
    srv.call(1, "fine")

    responses = srv.finish()

    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] == -32700
    assert _ids(responses) == [None, 1]


def test_end_of_input_still_yields_every_response_in_order(fake_tools):
    srv = RunningServer()
    for request_id in (1, 2, 3, 4):
        srv.call(request_id, f"tool{request_id}")

    assert _ids(srv.finish()) == [1, 2, 3, 4]
    assert fake_tools.calls == ["tool1", "tool2", "tool3", "tool4"]


def _activity_path() -> Path:
    return data_root() / "mcp-activity" / f"{os.getpid()}.json"


def test_activity_file_shows_running_tool_and_queue_depth_then_is_removed(fake_tools):
    fake_tools.blocking.add("slow")
    srv = RunningServer()
    srv.call(1, "slow", {"secret": "do-not-leak"})
    assert fake_tools.started["slow"].wait(TIMEOUT)
    srv.call(2, "queued_a")
    srv.call(3, "queued_b")
    srv.input.wait_consumed()

    path = _activity_path()
    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)
    assert document["pid"] == os.getpid()
    assert document["current"]["tool"] == "slow"
    assert document["current"]["request_id"] == 1
    assert document["queue_depth"] == 2
    assert set(document) >= {"started_at", "updated_at", "completed", "cancelled"}
    assert "do-not-leak" not in raw
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    fake_tools.gates["slow"].set()
    srv.finish()
    assert not path.exists()


def test_activity_counters_track_completed_and_cancelled(fake_tools):
    fake_tools.blocking.add("first")
    srv = RunningServer()
    srv.call(1, "first")
    assert fake_tools.started["first"].wait(TIMEOUT)
    srv.call(2, "doomed")
    srv.cancel(2)
    srv.input.wait_consumed()
    fake_tools.gates["first"].set()
    srv.call(3, "last")
    srv.output.wait_for(2)
    srv.input.wait_consumed()

    document = json.loads(_activity_path().read_text(encoding="utf-8"))

    assert document["completed"] == 2
    assert document["cancelled"] == 1
    assert document["current"] is None
    srv.finish()


def test_stale_activity_file_for_dead_pid_is_pruned_at_startup(fake_tools):
    directory = data_root() / "mcp-activity"
    directory.mkdir(parents=True)
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    stale = directory / f"{finished.pid}.json"
    stale.write_text("{}", encoding="utf-8")
    alive = directory / f"{os.getppid()}.json"
    alive.write_text("{}", encoding="utf-8")

    srv = RunningServer()
    srv.call(1, "fine")
    srv.finish()

    assert not stale.exists()
    assert alive.exists()


def test_activity_can_be_disabled(fake_tools, monkeypatch):
    monkeypatch.setenv("CORTEX_MCP_ACTIVITY", "0")
    srv = RunningServer()
    srv.call(1, "fine")
    srv.finish()

    assert not (data_root() / "mcp-activity").exists()


def test_activity_failure_never_breaks_the_protocol(fake_tools, monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CORTEX_DATA_DIR", str(blocker))
    srv = RunningServer()
    srv.call(1, "fine")

    assert _ids(srv.finish()) == [1]


def test_handle_without_cancel_calls_tool_with_two_arguments(monkeypatch):
    seen: list[tuple[Any, ...]] = []
    monkeypatch.setattr(server, "call_tool", lambda *args, **kwargs: seen.append((args, kwargs)) or {})
    server._handle({"id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}})

    assert seen == [(("x", {}), {})]


def test_cancel_notification_for_unknown_id_is_harmless(fake_tools):
    srv = RunningServer()
    srv.cancel(99)
    srv.call(1, "fine")

    assert _ids(srv.finish()) == [1]


def test_server_passes_request_id_to_call_tool(fake_tools):
    srv = RunningServer()
    srv.call("req-5", "fine")
    srv.finish()

    assert fake_tools.request_ids["fine"] == "req-5"


def test_cortex_status_is_answered_by_reader_while_tool_call_is_blocked(monkeypatch):
    started = threading.Event()
    gate = threading.Event()

    def blocking_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        started.set()
        assert gate.wait(TIMEOUT)
        return mcp_tools._content({"ok": name})

    monkeypatch.setattr(mcp_tools, "_dispatch_tool", blocking_dispatch)
    srv = RunningServer()
    srv.call("req-7", "cortex_refresh")
    assert started.wait(TIMEOUT)
    srv.call(8, "cortex_refresh")
    srv.call(9, "cortex_status")

    responses = srv.output.wait_for(1)

    assert not gate.is_set()
    assert [r["id"] for r in responses] == [9]
    status = _payload(responses[0]["result"])
    assert status["queue_depth"] == 1
    assert status["deadline_budget_seconds"] > 0
    running = [entry for entry in status["in_flight"] if entry["request_id"] == "req-7"]
    assert [entry["tool_name"] for entry in running] == ["cortex_refresh"]
    gate.set()
    assert _ids(srv.finish()) == [9, "req-7", 8]


# --- tools-level: soft stop at the call deadline ---


def _indexed_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Cortex Test"], cwd=repo, check=True, capture_output=True)
    (repo / "app.py").write_text("def app():\n    return 1\n\n\ndef helper():\n    return 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


@contextlib.contextmanager
def _deadline_expired(monkeypatch):
    """Make every enabled Deadline report `.expired` immediately. The hard
    path (`Deadline.check`) does not read `.expired`, so only soft stops
    (the dead-code analysis predicate) are affected."""
    with monkeypatch.context() as patch:
        patch.setattr(Deadline, "expired", property(lambda self: self.enabled))
        yield


def _count_report_builds(monkeypatch) -> dict[str, int]:
    calls = {"n": 0}
    original = mcp_tools.build_report_data

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "build_report_data", counting)
    return calls


def test_analysis_should_stop_follows_cancel_and_active_deadline(monkeypatch):
    assert mcp_tools._analysis_should_stop()() is False

    cancel = threading.Event()
    monkeypatch.setattr(mcp_tools, "_current_cancel", cancel)
    should_stop = mcp_tools._analysis_should_stop()
    assert should_stop() is False
    cancel.set()
    assert should_stop() is True
    monkeypatch.setattr(mcp_tools, "_current_cancel", None)

    with deadline_scope(30.0):
        live = mcp_tools._analysis_should_stop()
    assert live() is False
    with deadline_scope(30.0), _deadline_expired(monkeypatch):
        assert mcp_tools._analysis_should_stop()() is True
    with deadline_scope(0.0), _deadline_expired(monkeypatch):
        assert mcp_tools._analysis_should_stop()() is False


def test_dead_code_past_deadline_is_partial_with_note(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)

    with _deadline_expired(monkeypatch):
        result = call_tool("cortex_dead_code", {"repo_path": str(repo)})

    assert not result.get("isError")
    payload = _payload(result)
    assert payload["partial"] is True
    assert "CORTEX_TOOL_TIMEOUT_SECONDS" in payload["note"]


def test_complete_dead_code_payload_has_no_partial_key(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)

    payload = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo)}))

    assert "partial" not in payload
    assert "note" not in payload


def test_disabled_budget_never_stops_dead_code_early(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")

    with _deadline_expired(monkeypatch):
        payload = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo)}))

    assert "partial" not in payload


def test_dead_code_stops_when_cancel_event_is_set(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    cancel = threading.Event()
    cancel.set()

    payload = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo)}, cancel=cancel))

    assert payload["partial"] is True


def test_partial_overview_is_persisted_and_the_next_call_continues_it(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    calls = _count_report_builds(monkeypatch)
    args = {"repo_path": str(repo)}

    with _deadline_expired(monkeypatch):
        partial = _payload(call_tool("cortex_overview", {**args, "response_format": "detailed"}))

    assert partial["dead_code_partial"] is True
    assert "CORTEX_TOOL_TIMEOUT_SECONDS" in partial["note"]
    assert partial["_meta"]["cached"] is False
    assert calls["n"] == 1

    # The stopped analysis was kept: the next call continues its dead-code scan instead of
    # rebuilding communities, hotspots and the rest (report 0.11 #7).
    complete = _payload(call_tool("cortex_overview", args))

    assert calls["n"] == 1
    assert "dead_code_partial" not in complete
    assert "note" not in complete

    call_tool("cortex_overview", args)
    assert calls["n"] == 1, "the complete analysis is persisted and reused"


def test_overview_with_disabled_budget_is_complete(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")

    with _deadline_expired(monkeypatch):
        payload = _payload(call_tool("cortex_overview", {"repo_path": str(repo)}))

    assert "dead_code_partial" not in payload
    assert "note" not in payload


def test_overview_computes_once_per_call_even_with_cache_disabled(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "0")
    calls = _count_report_builds(monkeypatch)

    call_tool("cortex_overview", {"repo_path": str(repo)})
    assert calls["n"] == 1

    call_tool("cortex_overview", {"repo_path": str(repo)})
    assert calls["n"] == 2, "the memo must not outlive one outer tool call"
