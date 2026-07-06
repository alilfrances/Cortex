> Superseded by 2026-07-06-cortex-v3.md.

# Cortex v2 Design Spec

**Date:** 2026-04-23
**Status:** Approved

---

## Goal

Upgrade Cortex from a keyword-overlap bundler into a graph-aware context engine that matches or exceeds Graphify's capabilities — while preserving Cortex's core advantages: deterministic, portable, zero required external dependencies, and fast to set up in any project.

**Primary metrics:**
- Token economy: bundle packs the right context per token budget, not just keyword-matched files
- Portability: `pip install cortex-engine && cortex ingest .` — 30 seconds to context in any repo
- Honest provenance: every edge tagged with confidence and source layer

---

## What Cortex v2 Does

### Three jobs

**1. Map the codebase** (`cortex ingest .`)
Reads every file, understands Python code structure (imports, calls, class/function definitions), mines git history for co-change coupling, and stores everything in `.cortex/cortex.db` — one SQLite file per project.

**2. Pack a smart bundle** (`cortex bundle . --task "..." --budget N`)
Graph-aware retrieval: find the most relevant files by keyword match, then follow graph edges to pull in what they depend on and what depends on them — staying within token budget. Truncates intelligently. Replaces the v1 keyword-overlap-only approach.

**3. Report what matters** (`cortex report .`)
Shows central files (god nodes), natural file clusters (communities), and surprising cross-cluster couplings. Plain markdown, committed to `.cortex/` or printed to stdout.

**Optional upgrade** (`cortex enrich . --provider claude|codex`)
LLM reads docs and comments to find conceptual connections AST cannot see. Results cached in SQLite by content hash — never re-runs on unchanged files. Requires `pip install cortex-engine[llm]`.

---

## Architecture

```
cortex ingest .          → scan + AST + co-change → SQLite graph
cortex bundle . --task   → graph-aware bundle packing → markdown/json
cortex report .          → god nodes + clusters + report
cortex enrich .          → [llm] LLM semantic pass → cached in SQLite
```

### Four extraction layers (stacked, independently useful)

| Layer | Source | How | Always? |
|---|---|---|---|
| STRUCTURAL | Python AST (stdlib `ast`) | imports, calls, class/func definitions | Yes |
| COCHANGE | `git log` | files that change together = coupled | Yes |
| HEADING | File content | heading sections → file→section edges | Yes |
| SEMANTIC | LLM (Claude or Codex) | inferred conceptual edges | Optional, cached |

### Store

SQLite at `.cortex/cortex.db`. Tables:
- `sources` — file records with path, content, kind, hash, modified_at
- `commits` — git commit records
- `nodes` — graph nodes
- `edges` — graph edges with confidence, weight, layer
- `communities` — cluster assignments
- `bundles` — saved bundle outputs
- `llm_cache` — semantic extraction results keyed by path+hash
- `cost` — cumulative LLM token usage tracker

---

## Graph Model

### Nodes

| Kind | ID format | Represents |
|---|---|---|
| `file` | `file:path` | source file |
| `section` | `section:path:N` | heading section within file |
| `commit` | `commit:sha` | git commit |

### Edges

```python
@dataclass
class GraphEdge:
    edge_id:    str
    source:     str
    target:     str
    relation:   str      # imports|calls|inherits|contains|touches|cochange|inferred|similar
    layer:      str      # STRUCTURAL | COCHANGE | HEADING | SEMANTIC
    confidence: str      # EXTRACTED | INFERRED | AMBIGUOUS
    weight:     float    # see rules below
    metadata:   dict
```

### Confidence rules

- `STRUCTURAL` (AST) → always `EXTRACTED`, weight = 1.0
- `COCHANGE` (git) → `EXTRACTED`, weight = co-occurrence count / max count in repo
- `SEMANTIC` (LLM) → `EXTRACTED | INFERRED | AMBIGUOUS` per LLM judgment, weight 0.4–1.0
- No invented edges. Uncertain = AMBIGUOUS, never omitted.

---

## Bundle Packing (Graph-Aware)

Old approach: score files by keyword overlap with task, greedily pack by score.

New approach:
1. Keyword-match task terms → seed nodes (same as before)
2. BFS from seed nodes along graph edges → expand to neighbors weighted by edge confidence + weight
3. Score each candidate by: `keyword_overlap * 10 + graph_proximity_bonus + recency_weight`
4. Greedily pack by score until budget exhausted
5. Truncate last item to fit remaining budget

Graph proximity bonus: neighbor of seed node at depth 1 gets +5, depth 2 gets +2. STRUCTURAL and COCHANGE edges count more than SEMANTIC.

Result: relevant files pulled in along dependency/coupling paths, not just files that happen to mention the task keywords.

---

## Community Detection

Pure Python (no networkx dep). Label propagation algorithm:
1. Each node starts with its own label
2. Each iteration: every node adopts the most frequent label among its neighbors
3. Repeat until stable (typically 10–30 iterations)
4. Nodes with same label = one community

Optional: `pip install cortex-engine[graph]` unlocks networkx Louvain (higher quality clustering) + HTML visualization output.

---

## LLM Enrichment Layer

Provider abstraction:

```python
class LLMProvider(Protocol):
    def extract_semantic_edges(self, files: list[str], content: str) -> list[dict]: ...
```

Implementations: `ClaudeProvider`, `CodexProvider`. Selected via `--provider claude|codex` or `CORTEX_LLM_PROVIDER` env var.

Cache logic:
- Each file's result stored in `llm_cache` table keyed by `sha256(path + content)`
- Re-enrich: hash match → skip (zero API calls), hash changed → re-extract
- Cumulative token cost tracked in `cost` table per run

Semantic edges follow Graphify's honesty contract: EXTRACTED/INFERRED/AMBIGUOUS, confidence score required on every edge.

---

## Incremental Update

`cortex ingest . --update`:
1. Hash-check all files against `sources` table manifest
2. Find changed/new/deleted files
3. Re-extract changed files only (AST + co-change)
4. Prune deleted file nodes and their edges
5. Merge new nodes/edges

Co-change edges self-update: re-reads recent git log, recalculates frequencies, adjusts weights.

Fast on large repos: 5-file change in 500-file repo = 5 files processed.

---

## CLI Surface

```bash
# Core (stdlib only, no extras needed)
pip install cortex-engine

cortex ingest .                                              # full ingest
cortex ingest . --update                                     # incremental
cortex bundle . --task "add auth middleware" --budget 4000   # context bundle
cortex bundle . --task "..." --budget 4000 --format json     # json output
cortex report .                                              # graph report

# Optional LLM enrichment
pip install cortex-engine[llm]
cortex enrich . --provider claude                            # or --provider codex
cortex enrich . --provider claude --force                    # re-run all files

# Optional graph viz
pip install cortex-engine[graph]
cortex report . --html                                       # HTML graph output

# Project integration
cortex install-skill .                                       # writes .cortex/skill-claude.md
```

---

## Install & Packaging

```toml
[project]
name = "cortex-engine"
requires-python = ">=3.10"
dependencies = []          # stdlib only: sqlite3, ast, pathlib, subprocess, hashlib

[project.optional-dependencies]
llm   = ["anthropic>=0.40", "openai>=1.0"]
graph = ["networkx>=3.0"]
```

Zero hard deps. Core runs anywhere Python 3.10+ exists, no version conflicts.

---

## Advantage Over Graphify

| Capability | Graphify | Cortex v2 |
|---|---|---|
| AST structural extraction | Yes (multi-language, tree-sitter) | Yes (Python only, stdlib) |
| Git co-change coupling | No | **Yes — unique advantage** |
| LLM semantic edges | Yes (mandatory for docs) | Yes (optional, cached) |
| Community detection | Louvain (networkx) | Label propagation (stdlib) or Louvain ([graph]) |
| Token budget enforcement | Basic | Graph-aware packing |
| Incremental update | Yes (file manifest) | Yes (hash-based manifest) |
| Portable install | pip (heavy deps) | pip (zero hard deps) |
| Multi-LLM support | Claude only | Claude + Codex |
| Persistent store | JSON files | SQLite (queryable) |
| Cost tracking | Per-run JSON | Cumulative SQLite |

**Key differentiator:** Git co-change mining surfaces hidden coupling that static analysis and LLM reading both miss. Files that always break together are coupled by behavior, not just by code structure.

---

## File Structure

```
src/cortex/
  models.py       # dataclasses: SourceRecord, CommitRecord, GraphNode, GraphEdge, BundleItem, RetrievalBundle
  store.py        # CortexStore: all SQLite reads/writes
  ingest.py       # scan files + call graph/gitutils + save to store
  graph.py        # build_graph: AST + co-change + heading edges
  ast_extract.py  # NEW: Python AST → structural edges
  cochange.py     # NEW: git log → co-change edges
  community.py    # NEW: label propagation community detection
  bundle.py       # graph-aware bundle packing
  report.py       # god nodes + communities + report markdown
  enrich.py       # NEW: LLM enrichment layer + provider abstraction
  tokenizer.py    # byte-safe token counting
  gitutils.py     # git subprocess helpers
  cli.py          # Click CLI entrypoint
```

---

## Out of Scope (v2)

- Multi-language AST (JS/TS, Go, Swift) — future v3 via tree-sitter
- MCP server — future v3
- HTML visualization without `[graph]` extra
- Watch mode / git hook auto-rebuild
