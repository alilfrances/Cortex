#!/usr/bin/env sh
set -eu

python3 -m pip install -e .

cat <<'MSG'
Cortex is installed in editable mode.

Plugin entrypoints:
- Claude Code: use this repository as a plugin directory.
- Codex: use this repository as a plugin directory; the Codex manifest points to ./skills/ and ./.mcp.json.

If the host cannot find the cortex executable, use the fallback MCP command:
python3 -m cortex.mcp.server
MSG
