---
name: cortex-exploration
description: Use when asked "how does X work?", "where is Y implemented?", or "find the code that...", and when exploring or understanding an indexed repository.
user-invocable: false
---

# Explore an Indexed Repository

Prefer Cortex over broad Grep, Glob, or Read exploration.

| Need | Route |
|---|---|
| Find code for a task or concept | Start with `cortex_query`; include a known filename, extension, or language in the task. |
| Find an identifier | Use `cortex_search_symbols`. |
| Find literals, errors, comments, or prose | Use `cortex_search_text`. |
| Inspect a result | Use `cortex_read_symbol` for an exact span or `cortex_read_file` for an indexed file; file reads default to a skeleton. |
| Understand structure or flow | Use `cortex_relations` for one hop and `cortex_path` for multi-hop connections. |

Delegate multi-step questions such as where something is handled or how it flows to `cortex-explorer`. Keep a single lookup direct because delegation costs more than one tool call.

Fall back to raw tools only when content is unindexed or Cortex coverage is insufficient. If the index is missing or stale, suggest `cortex_refresh` and retry.
