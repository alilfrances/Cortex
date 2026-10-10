from __future__ import annotations

import contextvars
import json
import threading
import time

import pytest

from cortex.deadline import (
    DEFAULT_BUDGET_SECONDS,
    Deadline,
    DeadlineExceeded,
    check_deadline,
    current_deadline,
    deadline_scope,
    resolve_budget_seconds,
)


# ---------------------------------------------------------------------------
# Deadline object
# ---------------------------------------------------------------------------


def test_deadline_not_expired_within_budget() -> None:
    deadline = Deadline(10.0)
    assert deadline.enabled is True
    assert deadline.expired is False
    assert deadline.remaining > 0
    assert deadline.remaining <= 10.0
    deadline.check()  # must not raise


def test_deadline_expires_past_budget() -> None:
    deadline = Deadline(0.01)
    time.sleep(0.03)
    assert deadline.expired is True
    assert deadline.remaining < 0
    with pytest.raises(DeadlineExceeded) as excinfo:
        deadline.check()
    err = excinfo.value
    assert err.budget_seconds == 0.01
    assert err.elapsed_seconds >= 0.01
    assert err.tool_name is None


def test_deadline_exceeded_carries_tool_name() -> None:
    deadline = Deadline(0.01, tool_name="cortex_overview")
    time.sleep(0.03)
    with pytest.raises(DeadlineExceeded) as excinfo:
        deadline.check()
    assert excinfo.value.tool_name == "cortex_overview"
    assert "cortex_overview" in str(excinfo.value)


@pytest.mark.parametrize("budget", [0, -1, -5.5])
def test_deadline_disabled_for_non_positive_budget(budget: float) -> None:
    deadline = Deadline(budget)
    assert deadline.enabled is False
    assert deadline.expired is False
    assert deadline.remaining == float("inf")
    time.sleep(0.01)
    deadline.check()  # never raises when disabled


def test_deadline_elapsed_grows_monotonically() -> None:
    deadline = Deadline(100.0)
    first = deadline.elapsed
    time.sleep(0.01)
    second = deadline.elapsed
    assert second > first


# ---------------------------------------------------------------------------
# resolve_budget_seconds
# ---------------------------------------------------------------------------


def test_resolve_budget_defaults_when_unset() -> None:
    assert resolve_budget_seconds(env={}) == DEFAULT_BUDGET_SECONDS


def test_resolve_budget_defaults_when_blank() -> None:
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "   "}) == DEFAULT_BUDGET_SECONDS


def test_resolve_budget_zero_disables() -> None:
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "0"}) == 0.0


def test_resolve_budget_negative_disables() -> None:
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "-5"}) == 0.0


def test_resolve_budget_garbage_falls_back_to_default() -> None:
    """A typo must not silently remove the timeout.

    Only an explicit "0"/negative disables the deadline. An unparseable
    value falls back to the default budget, because this module exists
    precisely to stop an unbounded call from wedging the server -- failing
    open to "no deadline" on a typo would reintroduce the reported bug.
    """
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "not-a-number"}) == DEFAULT_BUDGET_SECONDS


def test_resolve_budget_garbage_variants_fall_back_to_default() -> None:
    for raw in ["nan-ish", "1e", "infinity-plus-one", "12abc", "30s", "thirty"]:
        result = resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": raw})
        assert result == DEFAULT_BUDGET_SECONDS, f"{raw!r} should fall back to the default budget"


def test_resolve_budget_blank_or_missing_uses_default() -> None:
    assert resolve_budget_seconds(env={}) == DEFAULT_BUDGET_SECONDS
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "  "}) == DEFAULT_BUDGET_SECONDS


def test_resolve_budget_non_finite_falls_back_to_default() -> None:
    """float() accepts these, and both would defeat the budget silently:
    a NaN budget makes Deadline.enabled false, an infinite one never expires."""
    for raw in ["nan", "NaN", "inf", "-inf", "Infinity"]:
        result = resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": raw})
        assert result == DEFAULT_BUDGET_SECONDS, f"{raw!r} should fall back to the default budget"


def test_resolve_budget_parses_valid_float() -> None:
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "45"}) == 45.0
    assert resolve_budget_seconds(env={"CORTEX_TOOL_TIMEOUT_SECONDS": "12.5"}) == 12.5


def test_resolve_budget_reads_actual_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "7")
    assert resolve_budget_seconds() == 7.0
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "0")
    assert resolve_budget_seconds() == 0.0
    monkeypatch.delenv("CORTEX_TOOL_TIMEOUT_SECONDS")
    assert resolve_budget_seconds() == DEFAULT_BUDGET_SECONDS


def test_resolve_budget_read_at_call_time_not_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module must not snapshot the env var at import time."""
    monkeypatch.delenv("CORTEX_TOOL_TIMEOUT_SECONDS", raising=False)
    assert resolve_budget_seconds() == DEFAULT_BUDGET_SECONDS
    monkeypatch.setenv("CORTEX_TOOL_TIMEOUT_SECONDS", "3.5")
    assert resolve_budget_seconds() == 3.5


# ---------------------------------------------------------------------------
# check_deadline / deadline_scope / contextvar behavior
# ---------------------------------------------------------------------------


def test_check_deadline_is_noop_with_no_active_scope() -> None:
    assert current_deadline() is None
    check_deadline()  # must not raise, must not error


def test_deadline_scope_sets_and_restores_current_deadline() -> None:
    assert current_deadline() is None
    with deadline_scope(10.0, tool_name="cortex_query") as deadline:
        assert current_deadline() is deadline
        assert deadline.tool_name == "cortex_query"
    assert current_deadline() is None


def test_check_deadline_raises_within_active_expired_scope() -> None:
    with deadline_scope(0.01, tool_name="cortex_dead_code"):
        time.sleep(0.03)
        with pytest.raises(DeadlineExceeded) as excinfo:
            check_deadline()
        assert excinfo.value.tool_name == "cortex_dead_code"


def test_check_deadline_ok_within_active_unexpired_scope() -> None:
    with deadline_scope(10.0):
        check_deadline()  # must not raise


def test_deadline_scope_disabled_budget_never_raises() -> None:
    with deadline_scope(0.0):
        time.sleep(0.01)
        check_deadline()  # never raises: 0 means "no deadline"


def test_deadline_scope_nesting_restores_outer_scope() -> None:
    with deadline_scope(10.0, tool_name="outer") as outer:
        assert current_deadline() is outer
        with deadline_scope(5.0, tool_name="inner") as inner:
            assert current_deadline() is inner
            assert inner is not outer
        assert current_deadline() is outer
    assert current_deadline() is None


def test_deadline_scope_restores_on_exception() -> None:
    with deadline_scope(10.0, tool_name="outer") as outer:
        with pytest.raises(RuntimeError):
            with deadline_scope(5.0, tool_name="inner"):
                raise RuntimeError("boom")
        assert current_deadline() is outer
    assert current_deadline() is None


def test_deadline_scope_isolated_across_contextvar_context() -> None:
    """A contextvars.Context started fresh must not see an active scope
    from the context that spawned it once the value is copied at spawn
    time -- this is the property that keeps per-thread/per-task isolation
    correct once a worker pool exists (Phase 3)."""
    results: dict[str, Deadline | None] = {}

    def other_context_reader() -> None:
        results["seen"] = current_deadline()

    with deadline_scope(10.0, tool_name="outer"):
        ctx = contextvars.copy_context()
        ctx.run(other_context_reader)
        # A copied context inherits the current value at copy time.
        assert results["seen"] is not None
        assert results["seen"].tool_name == "outer"

    # After the scope exits in *this* context, a brand new context (no
    # copy) never sees it.
    results.clear()
    fresh_ctx = contextvars.Context()
    fresh_ctx.run(other_context_reader)
    assert results["seen"] is None


def test_deadline_scope_thread_isolation() -> None:
    """Each thread must see only its own deadline scope: this is exactly
    why a ContextVar is used instead of a plain module global."""
    seen: dict[str, Deadline | None] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, budget: float) -> None:
        with deadline_scope(budget, tool_name=name):
            barrier.wait(timeout=5)
            # Give the other thread a chance to have set its own scope too.
            time.sleep(0.02)
            seen[name] = current_deadline()

    threads = [
        threading.Thread(target=worker, args=("thread_a", 10.0)),
        threading.Thread(target=worker, args=("thread_b", 20.0)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert seen["thread_a"] is not None and seen["thread_a"].tool_name == "thread_a"
    assert seen["thread_b"] is not None and seen["thread_b"].tool_name == "thread_b"
    assert current_deadline() is None


def test_deadline_scope_yields_the_deadline_object() -> None:
    with deadline_scope(1.0, tool_name="t") as deadline:
        assert isinstance(deadline, Deadline)
        assert deadline.budget_seconds == 1.0


def test_deadline_error_is_json_describable() -> None:
    deadline = Deadline(0.01, tool_name="cortex_overview")
    time.sleep(0.02)
    try:
        deadline.check()
    except DeadlineExceeded as exc:
        payload = {
            "error": "deadline_exceeded",
            "tool": exc.tool_name,
            "elapsed_seconds": exc.elapsed_seconds,
            "budget_seconds": exc.budget_seconds,
        }
        serialized = json.dumps(payload)
        assert "deadline_exceeded" in serialized
    else:
        pytest.fail("expected DeadlineExceeded")
