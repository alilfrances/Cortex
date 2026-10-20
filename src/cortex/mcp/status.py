"""In-process registry of in-flight MCP requests.

This is Phase 4 of ``docs/fix-plan-mcp-concurrency-wedge.md``: the
observability the original bug report explicitly asked for -- a way to tell
"still working" from "wedged" without ptrace (which was sandboxed in the
reporter's environment). It backs a future ``cortex_status`` tool and is
useful on its own for logging/diagnostics.

The registry is a single process-wide object (:data:`REGISTRY`). It is
thread-safe: reads (``snapshot``) happen from whatever thread handles a
``cortex_status`` call while writes (``track``) happen from worker threads
handling other tool calls, once Phase 3's worker pool lands. A
``threading.Lock`` guards every mutation and read of shared state.

Memory stays bounded regardless of how long the process runs or how many
calls it has served: in-flight entries are removed on completion, and
completed-call history is folded into small per-tool aggregates (call count
and slowest observed duration) rather than kept as an ever-growing list.

Importing this module has zero side effects: :data:`REGISTRY`'s start time
is recorded at *construction*, and the module constructs it at import time
only because that is what makes ``uptime`` measure "since the server
started" -- no I/O, no threads, no global state beyond the one object.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
from collections.abc import Iterator
from typing import Any

__all__ = ["RequestStatus", "StatusRegistry", "REGISTRY"]


class RequestStatus:
    """A snapshot-friendly handle for one in-flight request.

    Returned by :meth:`StatusRegistry.track` for callers that want to read
    back their own elapsed time; the registry itself only stores plain data
    (see ``_InFlight`` below) so ``snapshot()`` never has to worry about
    concurrent mutation of a live object it doesn't own.
    """

    __slots__ = ("tool_name", "request_id", "_start", "soft_deadline", "stage", "done", "total")

    def __init__(self, tool_name: str, request_id: str) -> None:
        self.tool_name = tool_name
        self.request_id = request_id
        self._start = time.monotonic()
        # Set when an analysis stopped softly at its time budget and returned a
        # partial result (counted separately from a hard deadline_exceeded).
        self.soft_deadline = False
        # Coarse progress for long calls (cortex_refresh): the stage name and
        # done/total counts within it. Plain attribute writes; readers tolerate
        # a momentarily inconsistent triple.
        self.stage = ""
        self.done = 0
        self.total = 0

    def set_progress(self, stage: str, done: int, total: int) -> None:
        self.stage = stage
        self.done = done
        self.total = total

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._start


class _ToolAggregate:
    """Bounded per-tool lifetime stats: no unbounded history is ever kept."""

    __slots__ = ("call_count", "failure_count", "deadline_exceeded_count", "soft_deadline_count", "slowest_seconds")

    def __init__(self) -> None:
        self.call_count = 0
        self.failure_count = 0
        self.deadline_exceeded_count = 0
        self.soft_deadline_count = 0
        self.slowest_seconds = 0.0

    def record(self, duration_seconds: float, *, failed: bool, deadline_exceeded: bool, soft_deadline: bool = False) -> None:
        self.call_count += 1
        if failed:
            self.failure_count += 1
        if deadline_exceeded:
            self.deadline_exceeded_count += 1
        if soft_deadline:
            self.soft_deadline_count += 1
        if duration_seconds > self.slowest_seconds:
            self.slowest_seconds = duration_seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_count": self.call_count,
            "failure_count": self.failure_count,
            "deadline_exceeded_count": self.deadline_exceeded_count,
            "soft_deadline_count": self.soft_deadline_count,
            "slowest_seconds": round(self.slowest_seconds, 3),
        }


class StatusRegistry:
    """Thread-safe registry of in-flight requests plus lifetime counters.

    All public methods acquire an internal lock for the (small, O(1)) time
    they touch shared state; none of them hold the lock while doing I/O or
    calling into user code, so contention is limited to bookkeeping.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._started_at_wall = time.time()
        self._in_flight: dict[int, RequestStatus] = {}
        self._id_counter = itertools.count(1)
        self._total_completed = 0
        self._total_failed = 0
        self._total_deadline_exceeded = 0
        self._total_soft_deadline = 0
        self._per_tool: dict[str, _ToolAggregate] = {}

    @contextlib.contextmanager
    def track(self, tool_name: str, request_id: str) -> Iterator[RequestStatus]:
        """Register ``(tool_name, request_id)`` as in-flight for the
        duration of the ``with`` block, and record its outcome on exit.

        Removal (and outcome recording) happens in a ``finally``, so a slot
        is freed and the call is accounted for whether the block returns
        normally, raises :class:`cortex.deadline.DeadlineExceeded`, or
        raises anything else. This is the only intended way to mutate
        in-flight state; :meth:`record_completed` exists separately for
        callers that already have their own timing and only want the
        lifetime counters updated.
        """
        status = RequestStatus(tool_name, request_id)
        slot = next(self._id_counter)
        with self._lock:
            self._in_flight[slot] = status
        failed = False
        deadline_exceeded = False
        try:
            yield status
        except BaseException as exc:
            failed = True
            # Import locally to avoid a hard dependency edge at module import
            # time between mcp/status.py and deadline.py; both are meant to
            # be independently importable standalone modules.
            from ..deadline import DeadlineExceeded

            deadline_exceeded = isinstance(exc, DeadlineExceeded)
            raise
        finally:
            with self._lock:
                self._in_flight.pop(slot, None)
            self.record_completed(
                tool_name,
                status.elapsed_seconds,
                failed=failed,
                deadline_exceeded=deadline_exceeded,
                soft_deadline=status.soft_deadline,
            )

    def record_completed(
        self,
        tool_name: str,
        duration_seconds: float,
        *,
        failed: bool = False,
        deadline_exceeded: bool = False,
        soft_deadline: bool = False,
    ) -> None:
        """Fold one completed call's outcome into the lifetime counters and
        the per-tool aggregate, without keeping any per-call history.

        Exposed separately from :meth:`track` for callers that measure
        duration themselves (e.g. a caller that wraps a call in its own
        timer) and only need the counters updated.
        """
        with self._lock:
            self._total_completed += 1
            if failed:
                self._total_failed += 1
            if deadline_exceeded:
                self._total_deadline_exceeded += 1
            if soft_deadline:
                self._total_soft_deadline += 1
            aggregate = self._per_tool.setdefault(tool_name, _ToolAggregate())
            aggregate.record(
                duration_seconds, failed=failed, deadline_exceeded=deadline_exceeded, soft_deadline=soft_deadline
            )

    def snapshot(self) -> dict[str, Any]:
        """A JSON-serializable snapshot of current and lifetime state.

        Safe to call from any thread, including while other threads are
        concurrently entering/exiting :meth:`track`; the lock is held only
        long enough to copy out plain data.
        """
        now_monotonic = time.monotonic()
        with self._lock:
            in_flight = []
            for status in self._in_flight.values():
                entry = {
                    "tool_name": status.tool_name,
                    "request_id": status.request_id,
                    "elapsed_seconds": round(now_monotonic - status._start, 3),
                }
                if status.stage:
                    entry["progress"] = {"stage": status.stage, "done": status.done, "total": status.total}
                in_flight.append(entry)
            per_tool = {name: aggregate.as_dict() for name, aggregate in self._per_tool.items()}
            total_completed = self._total_completed
            total_failed = self._total_failed
            total_deadline_exceeded = self._total_deadline_exceeded
            total_soft_deadline = self._total_soft_deadline
            started_at_wall = self._started_at_wall
            uptime_seconds = now_monotonic - self._started_at

        in_flight.sort(key=lambda entry: (-entry["elapsed_seconds"], entry["tool_name"], entry["request_id"]))
        return {
            "started_at": started_at_wall,
            "uptime_seconds": round(uptime_seconds, 3),
            "in_flight_count": len(in_flight),
            "in_flight": in_flight,
            "total_completed": total_completed,
            "total_failed": total_failed,
            "total_deadline_exceeded": total_deadline_exceeded,
            "total_soft_deadline": total_soft_deadline,
            "per_tool": per_tool,
        }


# A single process-wide registry. Constructing it has no side effects beyond
# recording "now" as the start time -- no I/O, no threads, no env reads.
REGISTRY = StatusRegistry()
