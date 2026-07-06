# Security Policy

## Supported Versions

Security fixes are handled on the default branch. Until the project publishes stable release branches, use the latest commit on `main` or the latest published package version.

## Reporting a Vulnerability

Please report security issues through GitHub private vulnerability reporting if available on the repository.

If private reporting is unavailable, open a minimal public issue that says a vulnerability report is available, but do not include exploit details, secrets, or sensitive reproduction steps.

Useful report details:

- Affected command, MCP tool, hook, or package extra.
- Minimal reproduction steps.
- Expected and actual behavior.
- Impact and whether credentials, private code, generated `.cortex/` databases, or local files can be exposed.

## Security Model

Cortex is local-first. The core package does not require network access, embeddings, a vector database, or an LLM provider. Optional extras may install provider SDKs or language parsers; those integrations should keep credentials outside the repository and outside generated reports.

Generated `.cortex/` databases and reports can contain source-derived metadata from the indexed repository. Do not publish generated `.cortex/` contents from private projects.

## Disclosure Handling

Security reports are triaged for reproducibility and impact. Confirmed issues will be fixed on the default branch and released with a changelog note when appropriate.
