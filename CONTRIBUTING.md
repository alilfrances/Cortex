# Contributing

Thanks for helping improve Cortex.

## Development Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[llm,watch,dev]"
python3 -m cortex runtime setup  # or use a verified offline bundle
python3 -m pip install build
```

The `dev` extra installs `pytest` and `pytest-cov`. It is for development and CI only and is never a runtime dependency.

The core package has no required runtime dependencies. Keep new dependencies optional unless they are essential to the default CLI, MCP server, or package import path.

## Checks

Run the narrowest useful check for your change:

```bash
python3 -m pytest tests/ -q
python3 evals/run_evals.py --stdlib-tokens
python3 evals/perf_ingest.py
python3 -m build
```

`python3 -m build` requires the `build` package to be installed in the active environment first.

Keep eval fixtures small and deterministic so the suite stays fast.

The regex-fallback goldens and MCP shape goldens must also pass without the tree-sitter runtime in play:

```bash
CORTEX_FORCE_REGEX=1 python3 -m pytest tests/test_golden_extraction.py tests/test_golden_mcp_shapes.py -q
```

## Golden snapshots

`tests/goldens/` holds checked-in JSON snapshots that lock Cortex's observable behavior (the 7b0e6e0 baseline) so later changes cannot alter it silently:

| Directory | What it pins | Test |
|---|---|---|
| `extraction/<treesitter\|regex>/<fixture>/<file>.json` | node/edge set `build_file_layer` produces for every file of `tests/fixtures/multilang_repo` and `qml_full_repo` (plus `__whole_fixture__.json` for cross-file passes) | `tests/test_golden_extraction.py` |
| `extraction/python/` | stdlib-`ast` output for the `python_app` eval fixture | same |
| `graph/<backend>/qt_app.json` | full `build_graph` result (nodes, edges, degree metadata) for the Qt eval fixture | same |
| `mcp/tools_list.json`, `mcp/initialize.json` | `TOOL_DEFINITIONS` and `initialize` responses | `tests/test_golden_mcp_shapes.py` |
| `mcp/shapes/<tool>.<format>.json` | key sets (no values) of all 15 tools in `concise` and `detailed`; backend independent | same |
| `mcp/payloads/<backend>/<name>.<format>.json` | full scrubbed payloads of the deterministic tools | same |
| `evals/default_matrix.json`, `evals/default_files.<backend>.json` | precision / recall of the 17 historical eval tasks, and the files each selected | `tests/test_eval_baseline.py` |

Tree-sitter goldens are skipped when the managed parser runtime is not ready (`python3 -m cortex runtime status`) or `CORTEX_FORCE_REGEX=1` is set. The regex goldens always run.

**Regenerating.** Goldens are rewritten only on request:

```bash
CORTEX_UPDATE_GOLDENS=1 python3 -m pytest tests/test_golden_*.py tests/test_eval_baseline.py
```

then re-run without the variable to confirm no diff (`git diff --exit-code tests/goldens` after a normal run must be clean). **A change that intentionally alters a golden must regenerate it in the same commit and add a `CHANGELOG.md` line naming the golden and the reason.** Review the golden diff like code: it should contain only what the change intends (for example, only added `calls` edges). Never regenerate to make an unexplained failure go away.

**Determinism rules** (enforced by `tests/test_golden_hygiene.py`): canonical JSON (sorted keys, 2-space indent, trailing newline, floats rounded to 6 places), no machine paths (`<repo>` replaces repository paths), no real commit SHAs, and none of the volatile keys in `tests.golden_utils.VOLATILE_KEYS` (timestamps, fingerprints, index age, `saved_tokens`, parser/runtime versions). Fixture repos are built with fixed `GIT_*_DATE` values and the stdlib tokenizer is forced (`tiktoken` would change token counts).

**List order.** `assert_golden` preserves list order on purpose: ranked outputs (impact items, search hits, risk files) are behavior, and an automatic sort would hide ranking regressions. Graph snapshots are sorted explicitly by `canonical_graph` (nodes by `node_id`, edges by `edge_id`). This deliberately deviates from the original plan text, which asked for a generic id-based list sort in the canonicalizer; `assert_golden(..., sort_lists=True)` remains available as an opt-in.

## Coverage floors

Per-module line-coverage floors for the modules the 0.10.0 work rewrites live in `tests/coverage_floors.toml` (the file records the measured values). The rule is `floor = floor(measured) - 2`, raise-only: add tests instead of lowering a floor. Check them with:

```bash
python3 -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json
python3 scripts/check_coverage_floors.py
```

`coverage.json` is a generated artifact; delete it after checking (or write it elsewhere with `--cov-report=json:/tmp/coverage.json` and pass `--coverage /tmp/coverage.json`). The checker exits 1 when a listed module is below its floor or absent from the report, and 2 on unusable input. CI runs the same two commands in a dedicated step.

## Baseline findings (7b0e6e0)

Writing the Task 0 baseline surfaced behavior that differs from what the plan assumed. It is pinned as it is today, marked in test docstrings, and must be changed deliberately:

- **Merge and empty commits corrupt `collect_recent_commits`.** A commit with no file list is not followed by the blank line the parser splits on, so the next older commit's raw header is glued into its `files` and that commit disappears (`tests/test_gitutils.py`). An empty `files` list is only observed when no older commit follows inside `limit`.
- **Non-ASCII paths are returned git-quoted** (`"h\303\251llo.py"`), because `git log` runs without `-z`.
- **Fingerprint call counts.** `cortex_search_symbols`, `cortex_overview`, `cortex_relations` and `cortex_path` compute the repository fingerprint twice per call (the token ledger re-enters the tool); `cortex_query`, `cortex_read_file`, `cortex_impact` and the rest compute it once (`tests/test_fingerprint_baseline.py`).
- **`cochange` does not de-duplicate or normalize paths** within a commit (`x.py:x.py` self-pairs, `./z.py`), whereas `compute_churn` does.
- **`truncate_risk_result` budget accounting** measures `budget_feasible` before the flag is added.
- **Regex backend QML ids.** The regex backend nests a QML child under the preceding sibling object (`Main.DeviceManager.DeviceDelegate.onClicked`), tree-sitter under its parent (`Main.DeviceDelegate.onClicked`).
- **`python3 evals/run_evals.py` is timing-sensitive.** Fixture commits carry wall-clock timestamps and bundle ranking weights recency by their difference, so one row can flip when commits straddle a second boundary. The pinned eval tests fix the git dates.
- **Platform scope.** Goldens were created on Python 3.14 (macOS arm64) with the managed tree-sitter runtime ready; CI runs Python 3.11. Python-version differences (for example in `ast.unparse` signature text) would surface as golden diffs; investigate them, do not regenerate blindly.

## Pull Requests

- Use a pull request for changes to `main`.
- Include tests for behavior changes.
- Keep generated files, local databases, credentials, and build artifacts out of commits.
- Update `README.md` or `CHANGELOG.md` when user-facing behavior changes.
- Avoid broad formatting churn unless the PR is only a formatting cleanup.

## Security

Do not include secrets, private repository contents, generated `.cortex/` data from private projects, or credentials in issues, PRs, tests, or fixtures. See `SECURITY.md` for vulnerability reporting.
