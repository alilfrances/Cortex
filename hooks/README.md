# Cortex Claude hooks

`hooks.json` registers two fail-open hooks:

- `SessionStart` gives a short index/tool inventory.
- `PreToolUse` runs `pre-tool-use.py` for Claude's built-in `Read`, `Grep`, and
  `Glob` tools. It uses only read-only SQLite metadata and never refreshes the
  index or reads a working-tree file.

The PreToolUse hook is advisory by default:

```text
CORTEX_HOOK_MODE=off|advise|enforce       # default: advise
CORTEX_HOOK_READ_THRESHOLD_BYTES=512      # default; CORTEX_HOOK_READ_THRESHOLD is an alias
CORTEX_HOOK_STALE_AFTER_SECONDS=86400     # enforce downgrade age
```

`advise` adds a non-blocking `additionalContext` containing an exact Cortex
replacement call. `enforce` is experimental and denies only fresh unscoped
indexed redirects; path-scoped Grep/Glob and other option-rich searches are
never enforced because their filters cannot be represented exactly. An index
older than the stale threshold automatically downgrades an otherwise blocking
decision to advice. It is never the default.

Decisions for an indexed repository are appended, best-effort, to
`<CORTEX_DATA_DIR>/<repo-hash>/usage.jsonl` (or `~/.cortex/data/<repo-hash>/`)
for future missed-savings analysis. Rows contain timestamps, tool/target
metadata, action, freshness, match counts, and token estimates—not regexes,
source content, or secrets. Logging errors, malformed events, unavailable
SQLite databases, locks, and non-git directories all produce exit 0 with no
stdout, leaving the original Claude tool call untouched. The warm decision
budget is under 50 ms on the eval fixture; a separately measured subprocess
number also includes Python/plugin startup.
