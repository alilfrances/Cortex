#!/usr/bin/env python3
"""Self-locating MCP server launcher for plugin installs (no pip required)."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

from cortex.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
