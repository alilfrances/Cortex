---
name: cortex-change-review
description: Use before committing, when reviewing a diff, or when cleaning up code in an indexed repository.
user-invocable: false
---

# Review a Change

Use Cortex as a deterministic pre-finish check.

1. Call `cortex_risk` with `staged: true` to review the index, or pass `range` for a revision range. Follow up on missing co-change, test, Qt/QML, or build-reference directives. Without a Cortex index, the tool may return a clearly marked partial git-only result.
2. Run the impacted tests first: `tests_to_run` lists test files selected by structural reachability from the changed symbols (`structural`), by co-change history (`cochange`), by file-name pairing (`pair`), or because the test itself changed (`changed`); `via` and `reason` say why. When `tests_run_all` is true, run the whole suite as well and read `tests_run_all_reason` (index missing, build/config change, deletion, unindexed file, no indexed tests, a changed file no test reaches, or a list cut by the response budget). From a shell: `cortex risk --tests-only` (in a Claude Code plugin install without `cortex` on `PATH`: `python3 "${CLAUDE_PLUGIN_ROOT}/bin/cortex-cli.py" risk --tests-only`) prints the paths for a runner and exits 2 when everything should run. When `partial` is true, test selection ran out of time: `untraced_changed_files_total` counts the changed files whose tests were not traced, `uncovered_changed_files` is empty because nothing was shown to be uncovered, and the whole suite should run (`tests_run_all` is true).
3. Investigate directives with `cortex_context`, `cortex_impact`, `cortex_relations`, or `cortex_references`, then inspect exact suspect code before reporting a finding.
4. Use `cortex_dead_code` when cleanup or newly orphaned code is in scope. Treat `high`, `medium`, and `low` as confidence tiers, not proof of dead code.

Dead-code analysis is conservative but static analysis has limits. `regex-backend` languages cannot receive the strongest confidence without corroboration, and Qt signals, slots, handlers, invokable methods, properties, and other meta-object surfaces may be reached dynamically. Confirm runtime and framework usage before deleting anything.
