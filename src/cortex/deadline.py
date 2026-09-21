"""Cooperative wall-clock deadlines for MCP tool calls.

This is Phase 2 of ``docs/fix-plan-mcp-concurrency-wedge.md``: a per-request
soft budget that hot loops (``analyze_dead_code``'s candidate loop,
``detect_communities``'s iteration loop, the reference scans, ...) can check
cheaply and bail out of, rather than the server discovering minutes later
that a single call burned through the whole batch.

Why not ``signal.alarm`` / ``SIGALRM``
---------------------------------------
``signal.alarm`` only works on the main thread of the main interpreter, and
delivering a signal mid-computation is not safe to interleave with arbitrary
library code (partial writes, non-reentrant C extensions, etc). Phase 3 of
the fix plan introduces a bounded worker pool, at which point most tool
calls would not even run on the main thread, so ``signal.alarm`` would
silently stop working for exactly the calls that need it. A cooperative
deadline -- checked at existing loop boundaries -- works identically on the
main thread today and on a worker thread after Phase 3 ships, with no
signal-safety concerns at all.

Usage
-----
A dispatch layer (``call_tool``) wraps each call::

    budget = resolve_budget_seconds()
    with deadline_scope(budget, tool_name="cortex_overview") as deadline:
        ...  # existing call, unmodified

Deep call sites that cannot easily thread a ``Deadline`` parameter through
every intermediate function call the module-level helper instead::

    from cortex.deadline import check_deadline

    for candidate in candidates:
        check_deadline()
        ...

``check_deadline()`` is a no-op (a single ``contextvar`` lookup and an early
return) whenever no deadline scope is active, which is the common case for
anything not called through ``call_tool`` -- e.g. unit tests, the CLI, and
any future non-MCP entry point.
"""

from __future__ import annotations

import contextlib
import contextvars
import math
import os
import time
from collections.abc import Iterator

__all__ = [
    "DEFAULT_BUDGET_SECONDS",
    "Deadline",
    "DeadlineExceeded",
    "check_deadline",
    "current_deadline",
    "deadline_scope",
    "resolve_budget_seconds",
]

DEFAULT_BUDGET_SECONDS = 30.0

_ENV_VAR = "CORTEX_TOOL_TIMEOUT_SECONDS"


class DeadlineExceeded(Exception):
    """Raised by :meth:`Deadline.check` (and surfaced by ``check_deadline``)
    once a call has run past its wall-clock budget.

    Carries enough structured data for a caller to build the
    ``{"error": "deadline_exceeded", ...}`` payload described in the fix
    plan without recomputing anything.
    """

    def __init__(self, *, budget_seconds: float, elapsed_seconds: float, tool_name: str | None = None) -> None:
        self.tool_name = tool_name
        self.budget_seconds = budget_seconds
        self.elapsed_seconds = elapsed_seconds
        label = f" in {tool_name!r}" if tool_name else ""
        super().__init__(
            f"deadline exceeded{label}: {elapsed_seconds:.3f}s elapsed against a {budget_seconds:.3f}s budget"
        )


class Deadline:
    """A single cooperative wall-clock budget, started at construction time.

    All timing uses :func:`time.monotonic`, never :func:`time.time`, so the
    budget is immune to system clock adjustments (NTP steps, DST, manual
    clock changes) during a long-running call.

    A ``budget_seconds`` of ``0`` (or less) means "no deadline": ``.expired``
    is always ``False``, ``.remaining`` is always ``float("inf")``, and
    ``.check()`` never raises. This lets callers construct a ``Deadline``
    unconditionally and let the budget value itself decide whether it is
    live, instead of branching on "is there a deadline" everywhere.
    """

    __slots__ = ("tool_name", "budget_seconds", "_start")

    def __init__(self, budget_seconds: float, *, tool_name: str | None = None) -> None:
        self.tool_name = tool_name
        self.budget_seconds = float(budget_seconds)
        self._start = time.monotonic()

    @property
    def enabled(self) -> bool:
        """Whether this deadline can ever expire."""
        return self.budget_seconds > 0

    @property
    def elapsed(self) -> float:
        """Seconds elapsed since this deadline was constructed."""
        return time.monotonic() - self._start

    @property
    def remaining(self) -> float:
        """Seconds left in the budget. ``float("inf")`` when disabled."""
        if not self.enabled:
            return float("inf")
        return self.budget_seconds - self.elapsed

    @property
    def expired(self) -> bool:
        """Whether the budget has been exceeded. Always ``False`` when disabled."""
        if not self.enabled:
            return False
        return self.elapsed > self.budget_seconds

    def check(self) -> None:
        """Raise :class:`DeadlineExceeded` if the budget has been exceeded.

        Cheap: one ``time.monotonic()`` call and a comparison. Safe to call
        from tight loops.
        """
        if not self.enabled:
            return
        elapsed = self.elapsed
        if elapsed > self.budget_seconds:
            raise DeadlineExceeded(
                budget_seconds=self.budget_seconds,
                elapsed_seconds=elapsed,
                tool_name=self.tool_name,
            )


def resolve_budget_seconds(env: dict[str, str] | None = None) -> float:
    """Resolve the tool-call budget from ``CORTEX_TOOL_TIMEOUT_SECONDS``.

    Read at call time (never cached at import time) so tests can
    ``monkeypatch.setenv`` it and so an operator can change it without
    restarting the process's import machinery. Defaults to
    :data:`DEFAULT_BUDGET_SECONDS`.

    Only an explicit ``"0"`` or a negative value disables the deadline.
    A value that fails to parse as a float falls back to the default
    budget, deliberately *not* to "no deadline": this whole module exists
    because an unbounded call wedges the server, so a typo in the override
    (``"30s"``, ``"thirty"``) must not silently remove the protection it
    was meant to configure. Failing back to the default is equally
    non-crashing and leaves the server bounded.
    """
    source = env if env is not None else os.environ
    raw = source.get(_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        # A malformed override must not disable the deadline -- see docstring.
        return DEFAULT_BUDGET_SECONDS
    # float() happily accepts "nan" and "inf", and both would defeat the
    # budget silently: a NaN budget makes Deadline.enabled false, and an
    # infinite one never expires. Neither is a value an operator can have
    # meant, so treat them as malformed rather than as "no deadline".
    if not math.isfinite(value):
        return DEFAULT_BUDGET_SECONDS
    if value <= 0:
        return 0.0
    return value


_current_deadline: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "cortex_current_deadline", default=None
)


def current_deadline() -> Deadline | None:
    """The active :class:`Deadline` for this context, or ``None``.

    A ``contextvars.ContextVar`` rather than a plain module global: a plain
    global would leak one call's deadline into another's if the server ever
    runs calls on separate threads or concurrent async tasks (Phase 3's
    worker pool). A contextvar gives each thread/task its own value, and
    each ``asyncio`` task its own copy-on-fork snapshot, for free.
    """
    return _current_deadline.get()


@contextlib.contextmanager
def deadline_scope(budget_seconds: float, *, tool_name: str | None = None) -> Iterator[Deadline]:
    """Install a :class:`Deadline` as the active deadline for the duration
    of the ``with`` block, restoring whatever was active before on exit
    (including on exception) -- so nested scopes (e.g. a tool that calls
    another tool-shaped helper internally) compose correctly instead of one
    exit clobbering an outer scope's deadline.

    Yields the :class:`Deadline` so callers who already have it in hand
    (e.g. to attach ``elapsed_seconds`` to an error payload) don't need a
    second call to :func:`current_deadline`.
    """
    deadline = Deadline(budget_seconds, tool_name=tool_name)
    token = _current_deadline.set(deadline)
    try:
        yield deadline
    finally:
        _current_deadline.reset(token)


def check_deadline() -> None:
    """Cheap deadline check for deep call sites.

    No-op when no deadline scope is active (a single contextvar lookup and
    an early return) -- this is the hot-path case for anything not called
    through :func:`deadline_scope`, so it must stay cheap. Raises
    :class:`DeadlineExceeded` when a scope is active and its budget has
    been exceeded.
    """
    deadline = _current_deadline.get()
    if deadline is None:
        return
    deadline.check()
