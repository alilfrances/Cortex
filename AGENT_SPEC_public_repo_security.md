# Public Repo Security Plan

## Goal

Make `alilfrances/Cortex` safe and maintainable as a public GitHub repository while keeping the project easy to install, test, and contribute to.

## Current State

- GitHub repository visibility: public.
- Default branch: `main`.
- Local working branch: `feat/cortex-v3`.
- Runtime package: Python 3.11+, stdlib-only core with optional `llm`, `languages`, and `watch` extras.
- Existing local checks: `python3 -m pytest tests/ -q`, `python3 evals/run_evals.py`, `python3 -m build`.
- Initial local secret scan found no committed credential files and no high-confidence secret strings.

## Implemented Guardrails

- Add public repository governance files:
  - `LICENSE`
  - `SECURITY.md`
  - `CONTRIBUTING.md`
  - `.github/pull_request_template.md`
  - `.github/ISSUE_TEMPLATE/bug_report.yml`
  - `.github/ISSUE_TEMPLATE/config.yml`
- Add automated checks:
  - `.github/workflows/ci.yml` for tests, evals, and package build.
  - `.github/workflows/codeql.yml` for CodeQL Python scanning.
  - `.github/dependabot.yml` for GitHub Actions and Python dependency updates.
- Expand `.gitignore` for local env files, virtualenvs, coverage, and editor state.
- Enable GitHub repository security features where available:
  - Dependabot alerts.
  - Dependabot security updates.
  - Secret scanning.
  - Push protection.
  - Secret scanning validity checks where supported.
- Protect `main`:
  - Require pull requests.
  - Require one approving review.
  - Require stale review dismissal.
  - Require conversation resolution.
  - Require linear history.
  - Block force pushes and branch deletion.

## Public Repo Operating Rules

- Do not commit generated `.cortex/` databases, local env files, credentials, build artifacts, or cache directories.
- Keep the core runtime dependency-free unless a new dependency has a clear security and maintenance justification.
- Prefer narrow optional extras for integrations that require networked SDKs or parser packages.
- Treat public issues as untrusted input. Do not run user-provided repro commands without reviewing them first.
- Use pull requests for all changes to `main`; direct pushes should remain unavailable except for repository administrators during emergencies.
- Keep GitHub Actions permissions read-only by default and grant write permissions only per job when required.

## Follow-Up Checklist

- Push these metadata files to `main` through a PR after local review.
- After the first CI run appears on GitHub, confirm required branch status checks match the actual check names.
- Add a PyPI publishing workflow only when release credentials and trusted publishing are intentionally configured.
- Consider adding `pre-commit` or `ruff` later if the project adopts formatting or linting policy.
