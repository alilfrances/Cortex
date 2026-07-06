#!/usr/bin/env sh
# Cortex setup helper. The core is stdlib-only: no pip install required.
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
    mkdir -p "$(dirname "$CONFIG")"
    touch "$CONFIG"
    if grep -q '^\[mcp_servers\.cortex\]' "$CONFIG"; then
      echo "cortex already registered in $CONFIG"
      exit 0
    fi
    printf '\n[mcp_servers.cortex]\ncommand = "python3"\nargs = ["%s/bin/cortex-mcp.py"]\n' "$ROOT" >> "$CONFIG"
    echo "registered cortex MCP server in $CONFIG"
    echo "skills: point Codex at $ROOT (plugin dir) or copy skills/cortex to ~/.codex/skills/"
    ;;
  --pip)
    python3 -m pip install -e "$ROOT"
    echo "cortex CLI installed (extras: pip install -e \"$ROOT[llm,languages,watch]\")"
    ;;
  *)
    cat <<MSG
Claude Code (one-command plugin install):
  claude plugin marketplace add alilfrances/Cortex   # or: claude plugin marketplace add $ROOT
  claude plugin install cortex@cortex

Codex (registers MCP server with an absolute path):
  $0 --codex

Optional cortex CLI via pip:
  $0 --pip
MSG
    ;;
esac
