---
name: cortex-pre-modification
description: Use when about to edit, refactor, rename, or delete code in an indexed repository, especially shared or unfamiliar files.
user-invocable: false
---

# Check Context Before Modification

Establish the change boundary before editing.

1. Call `cortex_context` once with all known files and symbols. Prefer one batched call over separate lookups; request optional impact, co-change, or symbol detail only when compact cards are insufficient.
2. Call `cortex_impact` on the primary file to inspect structural and historical coupling, likely tests, and related modules.
3. Use `cortex_references` for cross-language, config, documentation, script, and parser-missed references. Pass `mode: "writes"` when locating definitions and mutation sites.
4. Use `cortex_relations` or `cortex_path` when direct or multi-hop graph wiring affects the change. For Qt/QML or build changes, inspect the relevant runtime and build relations rather than assuming grep coverage is complete.
5. Read exact affected spans with `cortex_read_symbol` or indexed files with `cortex_read_file` before editing.

Check response `_meta` before trusting the context. `fingerprint_fresh: false` means the index may not match the working tree; `auto_refreshed: true` means Cortex refreshed it during the call. If freshness remains uncertain, call `cortex_refresh` and retry.
