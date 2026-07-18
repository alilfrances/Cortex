#!/usr/bin/env python3
"""Self-locating MCP server launcher for plugin installs (no pip required)."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

# Runtime setup is deliberately before importing Cortex's parser consumers.
# Diagnostics are persisted by cortex.runtime and never written to stdout,
# which is reserved for MCP JSON-RPC frames.
try:
    from cortex import runtime as _runtime

    _runtime_state = _runtime.ensure_runtime()
    if not _runtime_state.get("ready", False):
        # A degraded launch must not accidentally let language-pack fetch a
        # parser through the host interpreter.
        import os
        os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
    _runtime.configure_parser_environment()
except Exception:  # pragma: no cover - launcher fail-open boundary
    import os
    os.environ.setdefault("CORTEX_FORCE_REGEX", "1")

from cortex.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
