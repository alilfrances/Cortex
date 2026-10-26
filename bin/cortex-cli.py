#!/usr/bin/env python3
"""Self-locating CLI launcher for plugin installs (no pip required): `cortex ...` without `cortex` on PATH."""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

# Same runtime bootstrap as bin/cortex-mcp.py: set up the parser runtime before importing Cortex's parser
# consumers, and fall back to the regex backend (never a host-interpreter parser fetch) when it is not ready.
# Diagnostics go to stderr only.
try:
    from cortex import runtime as _runtime

    _runtime_state = _runtime.ensure_runtime()
    if not _runtime_state.get("ready", False):
        import os
        os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
        print("cortex-cli: parser runtime not ready; using the regex fallback", file=sys.stderr)
    _runtime.configure_parser_environment()
except Exception as error:  # pragma: no cover - launcher fail-open boundary
    import os
    os.environ.setdefault("CORTEX_FORCE_REGEX", "1")
    print(f"cortex-cli: parser runtime unavailable ({error}); using the regex fallback", file=sys.stderr)

from cortex.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
