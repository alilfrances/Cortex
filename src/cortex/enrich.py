from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from .gitutils import discover_repo_root
from .models import GraphEdge, GraphNode
from .store import CortexStore, default_db_path

_PROMPT_TEMPLATE = """\
You are a semantic graph extraction agent. Read the file below and find conceptual relationships that static analysis cannot detect — design patterns, shared abstractions, implicit dependencies, architectural intent.

File: {file_path}
---
{content}
---

Output ONLY valid JSON matching this schema (no markdown fences, no preamble):
{{"nodes":[],"edges":[{{"source":"node_id","target":"node_id","relation":"conceptually_related_to|semantically_similar_to|rationale_for|implements","confidence":"INFERRED|AMBIGUOUS","confidence_score":0.75,"metadata":{{}}}}],"input_tokens":0,"output_tokens":0}}

Rules:
- Only add edges you are confident about. Uncertain = AMBIGUOUS, not omitted.
- confidence_score: INFERRED=0.6-0.9, AMBIGUOUS=0.1-0.3
- Source and target node IDs must reference existing file or concept nodes.
- If nothing meaningful found, return empty edges array.
"""


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def extract_semantic_edges(self, file_path: str, content: str) -> dict: ...


class ClaudeProvider:
    name = 'claude'

    def __init__(self) -> None:
        api_key = os.environ.get('ANTHROPIC_API_KEY')
        if not api_key:
            raise RuntimeError(
                'ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY=your-key'
            )
        try:
            from anthropic import Anthropic
        except ImportError:
            raise RuntimeError(
                'anthropic package not installed. Run: pip install cortex-context-engine[llm]'
            )
        self._client = Anthropic(api_key=api_key)

    def extract_semantic_edges(self, file_path: str, content: str) -> dict:
        prompt = _PROMPT_TEMPLATE.format(file_path=file_path, content=content[:8000])
        message = self._client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = message.content[0].text.strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = {'nodes': [], 'edges': []}
        result['input_tokens'] = message.usage.input_tokens
        result['output_tokens'] = message.usage.output_tokens
        return result


class CodexProvider:
    name = 'codex'

    def __init__(self) -> None:
        api_key = os.environ.get('OPENAI_API_KEY')
        if not api_key:
            raise RuntimeError(
                'OPENAI_API_KEY not set. Run: export OPENAI_API_KEY=your-key'
            )
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError(
                'openai package not installed. Run: pip install cortex-context-engine[llm]'
            )
        self._client = OpenAI(api_key=api_key)

    def extract_semantic_edges(self, file_path: str, content: str) -> dict:
        prompt = _PROMPT_TEMPLATE.format(file_path=file_path, content=content[:8000])
        response = self._client.chat.completions.create(
            model='gpt-4o',
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or ''
        try:
            result = json.loads(raw.strip())
        except json.JSONDecodeError:
            result = {'nodes': [], 'edges': []}
        result['input_tokens'] = response.usage.prompt_tokens if response.usage else 0
        result['output_tokens'] = response.usage.completion_tokens if response.usage else 0
        return result


def make_provider(name: str) -> LLMProvider:
    if name == 'claude':
        return ClaudeProvider()
    if name == 'codex':
        return CodexProvider()
    raise ValueError(f'Unknown provider: {name!r}. Choices: claude, codex')


def enrich_repository(
    repo_path: Path,
    provider_name: str,
    db_path: Path | None = None,
    force: bool = False,
) -> dict:
    """
    Run LLM semantic enrichment on all source files.
    Results cached in SQLite by content_hash — skips unchanged files.
    """
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    provider = make_provider(provider_name)

    sources = store.fetch_sources(repo_root)
    all_new_nodes: list[dict] = []
    all_new_edges: list[dict] = []
    total_input = 0
    total_output = 0
    cached = 0
    enriched = 0

    for source in sources:
        content_hash = source.content_hash
        if not content_hash:
            continue

        if not force:
            cached_result = store.get_llm_cache(content_hash, provider_name)
            if cached_result is not None:
                all_new_nodes.extend(cached_result['nodes'])
                all_new_edges.extend(cached_result['edges'])
                cached += 1
                continue

        result = provider.extract_semantic_edges(source.path, source.content)
        nodes_data = result.get('nodes', [])
        edges_data = result.get('edges', [])
        input_tok = result.get('input_tokens', 0)
        output_tok = result.get('output_tokens', 0)

        store.set_llm_cache(content_hash, provider_name, nodes_data, edges_data, input_tok, output_tok)
        all_new_nodes.extend(nodes_data)
        all_new_edges.extend(edges_data)
        total_input += input_tok
        total_output += output_tok
        enriched += 1

    semantic_edges: list[GraphEdge] = []
    for edge_data in all_new_edges:
        try:
            semantic_edges.append(
                GraphEdge(
                    edge_id=f"semantic:{edge_data['source']}:{edge_data['target']}",
                    source=edge_data['source'],
                    target=edge_data['target'],
                    relation=edge_data.get('relation', 'conceptually_related_to'),
                    layer='SEMANTIC',
                    confidence=edge_data.get('confidence', 'INFERRED'),
                    weight=float(edge_data.get('confidence_score', 0.7)),
                    metadata=edge_data.get('metadata', {}),
                )
            )
        except (KeyError, TypeError):
            continue

    if semantic_edges:
        existing_nodes, existing_edges = store.fetch_graph(repo_root)
        store.save_graph(repo_root, existing_nodes, existing_edges + semantic_edges)

    store.record_cost(repo_root, provider_name, total_input, total_output)
    cumulative = store.fetch_cumulative_cost(repo_root)

    return {
        'enriched_files': enriched,
        'cached_files': cached,
        'semantic_edges_added': len(semantic_edges),
        'this_run_input_tokens': total_input,
        'this_run_output_tokens': total_output,
        'cumulative_input_tokens': cumulative['total_input_tokens'],
        'cumulative_output_tokens': cumulative['total_output_tokens'],
        'total_runs': cumulative['runs'],
    }
