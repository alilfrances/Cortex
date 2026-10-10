"""The deadline and status registry as wired into MCP dispatch.

The unit tests in test_deadline.py / test_mcp_status.py cover the modules in
isolation; these cover the thing the bug report actually cares about -- that a
tool call which runs long is stopped and reported rather than left to burn CPU
while the rest of the batch queues behind it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cortex.deadline import DeadlineExceeded, check_deadline  # noqa: E402
from cortex.mcp import tools as mcp_tools  # noqa: E402


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_overrunning_tool_returns_structured_error_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call past its budget fails fast with an actionable payload."""
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0.05")

    def _slow(_args: dict) -> dict:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            check_deadline()
        raise AssertionError("check_deadline() should have stopped this call")

    monkeypatch.setattr(mcp_tools, "_call_overview", _slow)
    result = mcp_tools.call_tool("cortex_overview", {})

    assert result.get("isError") is True
    payload = _payload(result)
    assert payload["error"] == "deadline_exceeded"
    assert payload["tool"] == "cortex_overview"
    assert payload["budget_seconds"] == 0.05
    assert payload["elapsed_seconds"] >= 0.05
    assert "CORTEX_TOOL_TIMEOUT_SECONDS" in payload["hint"]


def test_deadline_exceeded_is_counted_and_does_not_wedge_later_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server stays usable after one call blows its budget."""
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0.05")

    def _slow(_args: dict) -> dict:
        import time

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            check_deadline()
        raise AssertionError("unreachable")

    monkeypatch.setattr(mcp_tools, "_call_overview", _slow)
    before = _payload(mcp_tools.call_tool("cortex_status", {}))["total_deadline_exceeded"]
    mcp_tools.call_tool("cortex_overview", {})
    after_payload = _payload(mcp_tools.call_tool("cortex_status", {}))

    assert after_payload["total_deadline_exceeded"] == before + 1
    assert after_payload["per_tool"]["cortex_overview"]["deadline_exceeded_count"] >= 1
    # The very fact this returned proves dispatch still works afterwards.
    assert after_payload["in_flight_count"] == 1


def test_status_reports_itself_in_flight_and_is_json_serializable() -> None:
    payload = _payload(mcp_tools.call_tool("cortex_status", {}))
    json.dumps(payload)
    assert payload["in_flight_count"] == 1
    assert payload["in_flight"][0]["tool_name"] == "cortex_status"
    assert payload["deadline_budget_seconds"] > 0


def test_request_id_reaches_the_in_flight_registry_without_entering_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def _dispatch(name: str, args: dict) -> dict:
        seen["args"] = dict(args)
        seen["in_flight"] = mcp_tools.REGISTRY.snapshot()["in_flight"]
        return mcp_tools._content({"ok": True})

    monkeypatch.setattr(mcp_tools, "_dispatch_tool", _dispatch)
    mcp_tools.call_tool("cortex_refresh", {"a": 1}, request_id=41)

    assert seen["args"] == {"a": 1}
    assert [(e["tool_name"], e["request_id"]) for e in seen["in_flight"]] == [("cortex_refresh", "41")]


def test_status_is_registered_as_a_tool() -> None:
    names = [tool["name"] for tool in mcp_tools.TOOL_DEFINITIONS]
    assert "cortex_status" in names
    definition = next(t for t in mcp_tools.TOOL_DEFINITIONS if t["name"] == "cortex_status")
    assert definition["inputSchema"]["type"] == "object"


def test_disabled_budget_lets_a_long_call_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """CORTEX_TOOL_TIMEOUT_SECONDS=0 restores the old unbounded behaviour."""
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")
    calls: list[int] = []

    def _fast(_args: dict) -> dict:
        calls.append(1)
        check_deadline()  # must be a no-op with no budget
        return mcp_tools._content({"ok": True})

    monkeypatch.setattr(mcp_tools, "_call_overview", _fast)
    result = mcp_tools.call_tool("cortex_overview", {})
    assert not result.get("isError")
    assert calls == [1]


def test_unknown_tool_still_reports_cleanly() -> None:
    result = mcp_tools.call_tool("cortex_nope", {})
    assert result.get("isError") is True
    assert _payload(result)["error"] == "unknown_tool"


def test_handler_exception_is_still_a_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_args: dict) -> dict:
        raise ValueError("kaboom")

    monkeypatch.setattr(mcp_tools, "_call_overview", _boom)
    result = mcp_tools.call_tool("cortex_overview", {})
    assert result.get("isError") is True
    payload = _payload(result)
    assert payload["error"] == "ValueError"
    assert payload["message"] == "kaboom"
