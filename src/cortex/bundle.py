from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from pathlib import Path

from .gitutils import discover_repo_root
from .models import BundleItem, GraphEdge, RetrievalBundle
from .store import CortexStore, default_db_path
from .tokenizer import count_text_tokens, truncate_text_to_budget


def _tokenize_query(task: str) -> set[str]:
    return {token.lower() for token in re.findall(r'[A-Za-z0-9_]+', task) if token}


def _score_text(task_terms: set[str], text: str, recency_weight: float = 0.0) -> float:
    haystack_terms = {token.lower() for token in re.findall(r'[A-Za-z0-9_]+', text)}
    overlap = len(task_terms & haystack_terms)
    return overlap * 10.0 + recency_weight


def _build_adjacency(edges: list[GraphEdge]) -> dict[str, list[tuple[str, float]]]:
    """Build undirected adjacency list: node_id -> [(neighbor_id, weight), ...]"""
    adj: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        adj[edge.source].append((edge.target, edge.weight))
        adj[edge.target].append((edge.source, edge.weight))
    return dict(adj)


def _bfs_proximity(
    seed_ids: set[str],
    adj: dict[str, list[tuple[str, float]]],
    max_depth: int = 2,
) -> dict[str, float]:
    """
    BFS from seed nodes -> proximity bonus scores for neighbors.
    Depth 1 gets +5 * edge_weight, depth 2 gets +2 * edge_weight.
    """
    depth_bonus = {1: 5.0, 2: 2.0}
    scores: dict[str, float] = {}
    frontier = set(seed_ids)
    visited = set(seed_ids)

    for depth in range(1, max_depth + 1):
        bonus = depth_bonus.get(depth, 0.0)
        next_frontier: set[str] = set()
        for nid in frontier:
            for neighbor, weight in adj.get(nid, []):
                if neighbor not in visited:
                    candidate_score = bonus * weight
                    scores[neighbor] = max(scores.get(neighbor, 0.0), candidate_score)
                    next_frontier.add(neighbor)
                    visited.add(neighbor)
        frontier = next_frontier

    return scores


def _bundle_markdown(bundle: RetrievalBundle) -> str:
    lines = [
        '# Cortex Retrieval Bundle',
        '',
        f'- Task: {bundle.task}',
        f'- Budget: {bundle.budget}',
        f'- Total Tokens: {bundle.total_tokens}',
        '',
        '## Confidence Notes',
    ]
    lines.extend(f'- {note}' for note in bundle.confidence_notes)
    lines.extend(['', '## Items'])
    for item in bundle.items:
        lines.extend(
            [
                f'### {item.title}',
                f'- Kind: {item.kind}',
                f'- Path: {item.path}',
                f'- Tokens: {item.token_count}',
                f'- Score: {item.score:.2f}',
                '',
                item.content,
                '',
            ]
        )
    if bundle.open_questions:
        lines.append('## Open Questions')
        lines.extend(f'- {question}' for question in bundle.open_questions)
    return '\n'.join(lines).strip()


def generate_bundle(
    repo_path: Path,
    task: str,
    budget: int,
    db_path: Path | None = None,
    output_format: str = 'md',
) -> str | dict:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = store.fetch_sources(repo_root)
    commits = store.fetch_commits(repo_root)
    _, edges = store.fetch_graph(repo_root)

    task_terms = _tokenize_query(task)
    adj = _build_adjacency(edges)

    newest_commit = max((c.authored_at for c in commits), default=0)
    source_scores: dict[str, float] = {}
    for source in sources:
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - int(source.modified_at) + 1)))
        source_scores[source.path] = _score_text(
            task_terms,
            f'{source.path}\n{source.content}',
            recency_weight,
        )

    seed_ids = {f'file:{path}' for path, score in source_scores.items() if score > 0}
    proximity = _bfs_proximity(seed_ids, adj, max_depth=2)

    candidates: list[BundleItem] = []

    for source in sources:
        file_node_id = f'file:{source.path}'
        keyword_score = source_scores[source.path]
        graph_bonus = proximity.get(file_node_id, 0.0)
        final_score = keyword_score + graph_bonus

        token_count = count_text_tokens(source.content)
        candidates.append(
            BundleItem(
                item_id=f'source:{source.path}',
                kind=source.kind,
                title=source.path,
                path=source.path,
                content=source.content,
                token_count=token_count,
                score=final_score,
                metadata={'modified_at': source.modified_at, 'graph_bonus': graph_bonus},
            )
        )

    for commit in commits:
        recency_weight = 0.0
        if newest_commit:
            recency_weight = max(0.0, 5.0 - math.log2(max(1, newest_commit - commit.authored_at + 1)))
        content = f'{commit.summary}\nFiles: {chr(44).join(commit.files)}'
        candidates.append(
            BundleItem(
                item_id=f'commit:{commit.sha}',
                kind='commit',
                title=commit.summary,
                path=commit.sha,
                content=content,
                token_count=count_text_tokens(content),
                score=_score_text(task_terms, content, recency_weight=recency_weight),
                metadata={'sha': commit.sha, 'files': commit.files, 'authored_at': commit.authored_at},
            )
        )

    candidates.sort(key=lambda item: (-item.score, item.path))

    selected: list[BundleItem] = []
    total_tokens = 0
    for item in candidates:
        if item.score <= 0:
            continue
        if total_tokens + item.token_count <= budget:
            selected.append(item)
            total_tokens += item.token_count
            continue
        remaining = budget - total_tokens
        if remaining <= 16:
            continue
        truncated = truncate_text_to_budget(item.content, remaining)
        truncated_tokens = count_text_tokens(truncated)
        if truncated_tokens <= 0:
            continue
        selected.append(
            BundleItem(
                item_id=item.item_id,
                kind=item.kind,
                title=item.title,
                path=item.path,
                content=truncated,
                token_count=truncated_tokens,
                score=item.score,
                metadata={**item.metadata, 'truncated': True},
            )
        )
        total_tokens += truncated_tokens

    bundle = RetrievalBundle(
        task=task,
        repo_path=str(repo_root),
        budget=budget,
        total_tokens=total_tokens,
        generated_at=int(time.time()),
        items=selected,
        confidence_notes=[
            'Graph-aware packing: keyword-matched files + BFS graph neighbors.',
            'STRUCTURAL (AST) and COCHANGE (git) edges inform neighbor selection.',
            'Token counts use Cortex byte-safe local estimator.',
        ],
        open_questions=[] if selected else ['No matching sources found. Run cortex ingest . first.'],
    )
    store.save_bundle(repo_root, bundle)

    if output_format == 'json':
        return bundle.to_dict()
    return _bundle_markdown(bundle)
