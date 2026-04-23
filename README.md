# Cortex

Cortex is a local-first context engine that turns a git-backed project into a
deterministic graph, provenance store, and token-budgeted retrieval bundle for
live agent work.

## V1 Thin Slice

- ingest the working tree plus a recent commit window
- persist source, commit, graph, and bundle records in SQLite
- emit Markdown or JSON retrieval bundles under a hard token budget
- generate a compact repo report with central nodes and weak links

## CLI

```bash
python -m cortex ingest /path/to/repo --commits 50
python -m cortex bundle /path/to/repo --task "Summarize the architecture" --budget 4000
python -m cortex report /path/to/repo
python -m cortex refresh /path/to/repo
python -m cortex benchmark /path/to/repo --budget 4000
python -m cortex codex install /path/to/repo
python -m cortex claude install /path/to/repo
python -m cortex hook install /path/to/repo
python -m cortex install codex
python -m cortex install claude
```

## Agent Workflow

- `refresh` ingests the repo and writes `.cortex/cortex_report.md`
- `benchmark` compares Cortex bundle size with full-corpus token cost
- local `codex` / `claude` installers append Cortex guidance without removing existing Graphify content
- `hook install` adds repo-local git hooks that run `cortex refresh .`
- global `install codex|claude` writes Cortex skill files under your home directory

## Design Notes

- Graph construction is deterministic and local.
- Token accounting is byte-safe and deterministic, with optional enrichment kept
  off the critical path.
- Graphify and IntelligentConceptStudio are source research inputs, not runtime
  dependencies.
