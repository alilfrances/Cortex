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
python -m cortex migrate /path/to/repo
python -m cortex hook install /path/to/repo
```

## Recommended Clean First-Time Setup

Use this flow when setting Cortex up for a repo from scratch.

### 1. Install Cortex

Requires Python 3.11+.

From the Cortex repo:

```bash
cd /path/to/Cortex
python3 -m pip install -e .
```

### 2. Create the repo-local Cortex artifacts

From the target project repo:

```bash
cd /path/to/your-project
cortex ingest . --commits 50
cortex report .
```

This creates:

- `.cortex/cortex.db`
- `.cortex/cortex_report.md`

### 3. Install the Cortex plugin

Use this repository as the Claude Code or Codex plugin directory. The plugin registers the shared Cortex skill and MCP server config:

- `.claude-plugin/plugin.json`
- `.codex-plugin/plugin.json`
- `.mcp.json`
- `skills/cortex/SKILL.md`

For older projects that used `cortex codex install .` or `cortex claude install .`, run:

```bash
cortex migrate .
```

This removes the old injected `## cortex` guidance from `AGENTS.md` and `CLAUDE.md`.

### 4. Optionally install repo-local git hooks

If you want Cortex to refresh automatically after commits and checkout events:

```bash
cortex hook install .
```

This installs repo-local git hook blocks that run:

```bash
cortex refresh . --commits 50
```

### 5. Use Cortex during work

```bash
cortex bundle . --task "Summarize the architecture" --budget 4000
cortex refresh .
```

Recommended minimum setup for a Codex-managed repo:

```bash
cortex ingest . --commits 50
cortex report .
cortex hook install .
```

## Agent Workflow

- `refresh` ingests the repo and writes `.cortex/cortex_report.md`
- `benchmark` compares Cortex bundle size with full-corpus token cost
- `migrate` removes old injected Cortex guidance from `AGENTS.md` and `CLAUDE.md`
- `hook install` adds repo-local git hooks that run `cortex refresh .`
- plugin manifests register Cortex skills and MCP config for Claude Code and Codex

## Design Notes

- Graph construction is deterministic and local.
- Token accounting is byte-safe and deterministic, with optional enrichment kept
  off the critical path.
- Prior graph-retrieval experiments and IntelligentConceptStudio are source research inputs, not runtime
  dependencies.
