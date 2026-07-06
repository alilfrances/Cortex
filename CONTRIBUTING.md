# Contributing

Thanks for helping improve Cortex.

## Development Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[llm,languages,watch]"
```

The core package has no required runtime dependencies. Keep new dependencies optional unless they are essential to the default CLI, MCP server, or package import path.

## Checks

Run the narrowest useful check for your change:

```bash
python3 -m pytest tests/ -q
python3 evals/run_evals.py
python3 -m build
```

Keep eval fixtures small and deterministic so the suite stays fast.

## Pull Requests

- Use a pull request for changes to `main`.
- Include tests for behavior changes.
- Keep generated files, local databases, credentials, and build artifacts out of commits.
- Update `README.md` or `CHANGELOG.md` when user-facing behavior changes.
- Avoid broad formatting churn unless the PR is only a formatting cleanup.

## Security

Do not include secrets, private repository contents, generated `.cortex/` data from private projects, or credentials in issues, PRs, tests, or fixtures. See `SECURITY.md` for vulnerability reporting.
