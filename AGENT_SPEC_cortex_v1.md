# AGENT_SPEC_cortex_v1

## Goal
- Build Cortex as a standalone Python CLI context engine in `/Users/alilkuizon/Personal Projects/Cortex`.
- Ship a thin working slice that ingests a repo working tree plus recent commits, persists a native graph and provenance store, and emits a token-budgeted retrieval bundle.

## Constraints
- Keep the first wave local-first and auditable.
- Use a native Cortex graph model rather than Graphify as a runtime dependency.
- Use SQLite as the canonical durable store.
- Keep optional LLM enrichment off the critical path.
- Preserve the existing IntelligentConceptStudio repo as a research input only.

## Current State
- Cortex directory exists but is empty.
- IntelligentConceptStudio contains the source product and learning-system research docs used to derive the Cortex design.
- The first implementation should target a Python CLI/core stack with tests and a smoke path against IntelligentConceptStudio.

## Decisions
- New sibling repo under `Personal Projects`.
- CLI-first Python implementation.
- Working tree plus recent commit provenance in v1.
- Deterministic ingestion/graphing/token budgeting with optional enrichment hooks.

## Ownership
- Slice A: repo scaffold, config, and CLI shell.
- Slice B: ingest, graph, store, bundle, and report core.
- Slice C: tests, fixtures, and smoke verification.

## Artifact Rules
- Master spec is updated by the orchestrator.
- Parallel agents should not edit the same file directly.
- Use sidecar notes only when needed, then merge into the master spec.

## Research Notes
- Karpathy `minbpe` informs byte-safe tokenization and deterministic token accounting patterns.
- IntelligentConceptStudio learning-system docs define the target concepts for retrieval bundles, provenance, confidence, and freshness.
- Graphify informs reporting ergonomics and graph summaries but is not a runtime dependency for v1.

## Implementation Plan
1. Create the Cortex repo scaffold, packaging, and CLI entrypoint.
2. Implement the deterministic thin slice across ingest, graph/store, bundle, and report.
3. Add tests and run fixture plus IntelligentConceptStudio smoke verification.

## Verification
- Build: Python import and CLI invocation succeed.
- Tests: targeted unit and integration test suite passes.
- Manual checks: ingest IntelligentConceptStudio and generate a bounded bundle/report.

## Status
- Pending: scaffold, implementation, tests, verification.
- In progress: wave setup.
- Done:
