---
name: cortex-change-review
description: Use before committing, when reviewing a diff, or when cleaning up code in an indexed repository.
user-invocable: false
---

# Review a Change

Use Cortex as a deterministic pre-finish check.

1. Call `cortex_risk` with `staged: true` to review the index, or pass `range` for a revision range. Follow up on missing co-change, test, Qt/QML, or build-reference directives. Without a Cortex index, the tool may return a clearly marked partial git-only result.
2. Investigate directives with `cortex_context`, `cortex_impact`, `cortex_relations`, or `cortex_references`, then inspect exact suspect code before reporting a finding.
3. Use `cortex_dead_code` when cleanup or newly orphaned code is in scope. Treat `high`, `medium`, and `low` as confidence tiers, not proof of dead code.

Dead-code analysis is conservative but static analysis has limits. `regex-backend` languages cannot receive the strongest confidence without corroboration, and Qt signals, slots, handlers, invokable methods, properties, and other meta-object surfaces may be reached dynamically. Confirm runtime and framework usage before deleting anything.
