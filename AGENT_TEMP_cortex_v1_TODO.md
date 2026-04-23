# AGENT_TEMP_cortex_v1_TODO

## Phases
- Phase 1: Scaffold repo and capture architecture artifacts.
- Phase 2: Implement thin-slice core and tests.
- Phase 3: Verify with fixtures and IntelligentConceptStudio smoke flow.

## Acceptance Criteria
- Cortex CLI can ingest a target repo into SQLite.
- Cortex CLI can emit a token-budgeted bundle for a task prompt.
- Cortex CLI can emit a graph/report summary for the ingested repo.
- Tests cover core parsing, storage, ranking, and budget enforcement.

## Risks
- Out-of-sandbox sibling repo writes require escalated commands.
- Token accounting must stay deterministic even when optional enrichment is enabled.
- The first graph model must stay small and comprehensible.

## Current Status
- Pending: scaffold, implementation, verification.
- In progress: artifact creation.
- Blocked:
- Done:

## Next Step
- Scaffold the Python project and dispatch implementation slices.
