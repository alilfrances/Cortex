from __future__ import annotations

import json
import sys
from typing import Any

from .tools import TOOL_DEFINITIONS, call_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "cortex", "version": "0.9.0"}
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


def _handle(frame: dict[str, Any]) -> dict[str, Any] | None:
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
        return _response(request_id, call_tool(str(name), arguments))
    if request_id is None:
        return None
    return _error(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    try:
        from ..runtime import configure_parser_environment, ensure_runtime
        state = ensure_runtime()
        if not state.get("ready", False):
            import os
            os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
        configure_parser_environment()
    except Exception as exc:  # fail-open: MCP initialize must remain valid
        print(f"cortex runtime degraded: {exc}", file=sys.stderr)
        import os
        os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
            response = _handle(frame)
        except json.JSONDecodeError as exc:
            response = _error(None, -32700, str(exc))
        except Exception as exc:
            print(f"cortex mcp server error: {exc}", file=sys.stderr)
            response = _error(None, -32603, "Internal error")
        if response is None:
            continue
        try:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
