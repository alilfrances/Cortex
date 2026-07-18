#!/usr/bin/env sh
# Cortex setup helper. Parser wheels are managed in an isolated runtime for
# plugin launches; pip installs use the same pinned dependencies.
#
#   ./install.sh           show Claude Code one-command install steps
#   ./install.sh --codex   register the Cortex MCP server in ~/.codex/config.toml
#   ./install.sh --pip     editable pip install (cortex CLI + optional extras)
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE="${1:-}"

case "$MODE" in
  --codex)
    CONFIG="${CODEX_HOME:-$HOME/.codex}/config.toml"
    RUNTIME_DIR="${CORTEX_RUNTIME_DIR:-${CODEX_HOME:-$HOME/.codex}/cortex-runtime}"
    mkdir -p "$(dirname "$CONFIG")"
    touch "$CONFIG"
    if grep -q '^\[mcp_servers\.cortex\]' "$CONFIG"; then
      echo "cortex already registered in $CONFIG"
      CORTEX_RUNTIME_DIR="$RUNTIME_DIR" PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m cortex runtime setup 2>/dev/null || true
      exit 0
    fi
    printf '\n[mcp_servers.cortex]\ncommand = "python3"\nargs = ["%s/bin/cortex-mcp.py"]\n' "$ROOT" >> "$CONFIG"
    echo "registered cortex MCP server in $CONFIG"
    echo "bootstrapping the isolated parser runtime (degraded completion is allowed)"
    CORTEX_RUNTIME_DIR="$RUNTIME_DIR" PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m cortex runtime setup 2>/dev/null || true
    echo "skills: point Codex at $ROOT (plugin dir) or copy skills/cortex to ~/.codex/skills/"
    ;;
  --pip)
    python3 -m pip install -e "$ROOT"
    python3 -m cortex runtime setup || true
    echo "cortex CLI installed (parser runtime is managed; extras: pip install -e \"$ROOT[llm,watch]\")"
    ;;
  *)
    cat <<MSG
Claude Code (one-command plugin install):
  claude plugin marketplace add alilfrances/Cortex   # or: claude plugin marketplace add $ROOT
  claude plugin install cortex@cortex

Codex (registers MCP server with an absolute path):
  $0 --codex

Optional cortex CLI via pip (also prepares the parser runtime):
  $0 --pip

Runtime controls:
  cortex runtime status
  CORTEX_RUNTIME_NETWORK=0 cortex runtime setup --offline-bundle bundle.zip --bundle-sha256 SHA256
MSG
    ;;
esac
