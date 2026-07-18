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

Cortex is local-first. Parser setup is the one automatic artifact-egress path: the isolated runtime downloads only locked, hash-verified parser wheels/artifacts on first setup. It sends no repository path, source, graph, or company metadata. After setup, ingest and query are cache-only; `CORTEX_RUNTIME_NETWORK=0` disables setup/parser network access and an administrator can pre-seed a verified offline bundle. `cortex semantic setup` downloads the optional static model only when explicitly invoked; normal ingest, query, and local semantic retrieval do not use the network.

`cortex enrich` is different from parser artifact egress: it sends up to 8,000 characters from each uncached indexed source file to the selected Anthropic or OpenAI provider. It is disabled unless `--allow-code-upload` is passed. Do not use it for repositories whose contents cannot be shared with that provider, and keep API keys outside the repository.

Cortex databases contain full indexed source text, commit-author metadata, query caches, and source-derived graph data. Managed database directories are owner-only on POSIX systems, and generated databases/reports are gitignored here. Do not publish or copy generated Cortex data from private projects. Symlinks and files larger than 5 MiB are excluded from indexing to reduce local-file disclosure and resource-exhaustion risks.

## Disclosure Handling

Security reports are triaged for reproducibility and impact. Confirmed issues will be fixed on the default branch and released with a changelog note when appropriate.
