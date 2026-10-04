from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO

from ..store import data_root
from .tools import TOOL_DEFINITIONS, build_status_result, call_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cortex", "version": "0.9.2"}
SERVER_INSTRUCTIONS = (
    "Use Cortex before Grep or raw file reads. Start with cortex_query for a coding task "
    "and cortex_context before multi-file edits. Search with cortex_search_symbols for "
    "named identifiers or cortex_search_text for body text and literals. Read indexed "
    "source with cortex_read_symbol or cortex_read_file. Check blast radius with "
    "cortex_impact and cortex_references, and run cortex_risk before finishing. Fall back "
    "to raw Grep or Read only when the index is missing or Cortex coverage is insufficient; "
    "call cortex_refresh for a missing or stale index."
)


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _handle(frame: dict[str, Any], cancel: threading.Event | None = None) -> dict[str, Any] | None:
    method = frame.get("method")
    request_id = frame.get("id")
    params = frame.get("params") or {}

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        client_version = params.get("protocolVersion")
        return _response(
            request_id,
            {
                "protocolVersion": client_version or PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
                "instructions": SERVER_INSTRUCTIONS,
            },
        )
    if method == "tools/list":
        return _response(request_id, {"tools": TOOL_DEFINITIONS})
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if cancel is None:
            return _response(request_id, call_tool(str(name), arguments))
        return _response(request_id, call_tool(str(name), arguments, cancel=cancel, request_id=request_id))
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Method not found: {method}")


_SENTINEL = object()
_ACTIVITY_DIR_NAME = "mcp-activity"


def _id_key(request_id: Any) -> str:
    """Hashable key that keeps ``1`` and ``"1"`` distinct."""
    return json.dumps(request_id, sort_keys=True)


def _pid_alive(pid: int) -> bool:
    # On Windows os.kill(pid, 0) terminates the process instead of probing it.
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError, ValueError):
        return False
    except OSError:
        return True
    return True


class _ServerState:
    """Counters and cancellation bookkeeping shared by the reader and main threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cancelled_ids: set[str] = set()
        self.current: dict[str, Any] | None = None
        self.current_key: str | None = None
        self.current_cancel: threading.Event | None = None
        self.queued = 0
        self.completed = 0
        self.cancelled = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "current": dict(self.current) if self.current else None,
                "queue_depth": self.queued,
                "completed": self.completed,
                "cancelled": self.cancelled,
            }


class _Activity:
    """Best-effort status file so a stuck server can be inspected from outside.

    Holds only the tool name, request id and counters, never arguments or repo
    content. Every failure is swallowed; it must never affect the protocol.
    """

    def __init__(self, state: _ServerState) -> None:
        self._state = state
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._path: Path | None = None
        if os.environ.get("CORTEX_MCP_ACTIVITY") == "0":
            return
        try:
            directory = data_root() / _ACTIVITY_DIR_NAME
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            self._path = directory / f"{os.getpid()}.json"
            self._prune_stale(directory)
        except Exception:
            self._path = None
            return
        self.write()

    @staticmethod
    def _prune_stale(directory: Path) -> None:
        for entry in directory.glob("*.json"):
            try:
                pid = int(entry.stem)
                if pid != os.getpid() and not _pid_alive(pid):
                    entry.unlink()
            except Exception:
                continue

    def write(self) -> None:
        if self._path is None:
            return
        with self._lock:
            tmp_name: str | None = None
            try:
                snapshot = self._state.snapshot()
                document = {
                    "pid": os.getpid(),
                    "started_at": self._started_at,
                    "updated_at": time.time(),
                    **snapshot,
                }
                fd, tmp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".tmp-", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, self._path)
                tmp_name = None
            except Exception:
                if tmp_name is not None:
                    try:
                        os.unlink(tmp_name)
                    except Exception:
                        pass

    def remove(self) -> None:
        if self._path is None:
            return
        with self._lock:
            try:
                self._path.unlink()
            except Exception:
                pass
            self._path = None


class _Writer:
    """Serialises output: one full line per write, flushed, under one lock."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.broken = False

    def send(self, response: dict[str, Any]) -> bool:
        line = json.dumps(response, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._stream.write(line)
                self._stream.flush()
            except BrokenPipeError:
                self.broken = True
        return not self.broken


def _request_label(frame: dict[str, Any]) -> str:
    method = frame.get("method")
    params = frame.get("params")
    if method == "tools/call" and isinstance(params, dict):
        return str(params.get("name", ""))
    return str(method)


def _read_input(
    lines: Iterable[str],
    work: queue.Queue[Any],
    state: _ServerState,
    writer: _Writer,
    activity: _Activity,
) -> None:
    """Parse input lines onto the work queue; handle cancel, ping and cortex_status immediately."""

    def enqueue(item: tuple[Any, ...]) -> None:
        with state.lock:
            state.queued += 1
            running = state.current is not None
        work.put(item)
        if running:
            activity.write()

    try:
        for line in lines:
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError as exc:
                enqueue(("response", _error(None, -32700, str(exc))))
                continue
            if isinstance(frame, dict):
                method = frame.get("method")
                params = frame.get("params")
                if method == "notifications/cancelled":
                    if isinstance(params, dict) and "requestId" in params:
                        key = _id_key(params["requestId"])
                        with state.lock:
                            state.cancelled_ids.add(key)
                            if state.current_cancel is not None and state.current_key == key:
                                state.current_cancel.set()
                    continue
                if method == "ping":
                    if frame.get("id") is not None and not writer.send(_response(frame["id"], {})):
                        break
                    continue
                if (
                    method == "tools/call"
                    and frame.get("id") is not None
                    and isinstance(params, dict)
                    and params.get("name") == "cortex_status"
                ):
                    # Answered here, not queued: it must report on the call that is running.
                    status = build_status_result(queue_depth=state.snapshot()["queue_depth"])
                    if not writer.send(_response(frame["id"], status)):
                        break
                    continue
            enqueue(("frame", frame))
    except Exception as exc:
        print(f"cortex mcp reader error: {exc}", file=sys.stderr)
    finally:
        work.put(_SENTINEL)


def serve(input_stream: Iterable[str], output_stream: TextIO) -> int:
    """Run the MCP loop; tool calls execute strictly one at a time on this thread."""
    state = _ServerState()
    writer = _Writer(output_stream)
    activity = _Activity(state)
    work: queue.Queue[Any] = queue.Queue()
    reader = threading.Thread(
        target=_read_input, args=(input_stream, work, state, writer, activity), daemon=True
    )
    reader.start()
    try:
        while True:
            item = work.get()
            if item is _SENTINEL or writer.broken:
                return 0
            if item[0] == "response":
                with state.lock:
                    state.queued -= 1
                if not writer.send(item[1]):
                    return 0
                continue
            if not _run_frame(item[1], state, writer, activity):
                return 0
    finally:
        activity.remove()


def _run_frame(frame: Any, state: _ServerState, writer: _Writer, activity: _Activity) -> bool:
    """Execute one queued frame. Returns False when the output pipe is gone."""
    is_dict = isinstance(frame, dict)
    request_id = frame.get("id") if is_dict else None
    key = _id_key(request_id) if request_id is not None else None
    cancel = threading.Event()
    with state.lock:
        state.queued -= 1
        if key is not None and key in state.cancelled_ids:
            state.cancelled_ids.discard(key)
            state.cancelled += 1
            dropped = True
        else:
            dropped = False
            state.current = {
                "tool": _request_label(frame) if is_dict else "invalid",
                "request_id": request_id,
                "started_at": time.time(),
            }
            state.current_key = key
            state.current_cancel = cancel
    activity.write()
    if dropped:
        return True

    response: dict[str, Any] | None
    try:
        response = _handle(frame, cancel)
    except Exception as exc:
        print(f"cortex mcp server error: {exc}", file=sys.stderr)
        answerable = request_id is not None or not is_dict
        response = _error(request_id, -32603, "Internal error") if answerable else None

    suppress = False
    with state.lock:
        was_cancelled = key is not None and key in state.cancelled_ids
        if key is not None:
            state.cancelled_ids.discard(key)
        if was_cancelled and is_dict and frame.get("method") == "tools/call":
            suppress = True
            state.cancelled += 1
        else:
            state.completed += 1
        state.current = None
        state.current_key = None
        state.current_cancel = None
    activity.write()
    if response is None or suppress:
        return True
    return writer.send(response)


def main() -> int:
    try:
        from ..runtime import configure_parser_environment, ensure_runtime
        state = ensure_runtime()
        if not state.get("ready", False):
            os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
        configure_parser_environment()
    except Exception as exc:  # fail-open: MCP initialize must remain valid
        print(f"cortex runtime degraded: {exc}", file=sys.stderr)
        os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
    return serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
