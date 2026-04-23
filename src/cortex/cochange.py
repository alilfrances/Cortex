from __future__ import annotations

from collections import Counter

from .models import CommitRecord, GraphEdge


def build_cochange_edges(commits: list[CommitRecord]) -> list[GraphEdge]:
    """
    Build COCHANGE edges from git commit history.
    Files appearing together in same commits are co-changed.
    Edge weight = co-occurrence count / max count across all pairs.
    """
    pair_counts: Counter[tuple[str, str]] = Counter()

    for commit in commits:
        files = [f for f in commit.files if f]
        for i, a in enumerate(files):
            for b in files[i + 1 :]:
                pair = (min(a, b), max(a, b))
                pair_counts[pair] += 1

    if not pair_counts:
        return []

    max_count = max(pair_counts.values())
    edges: list[GraphEdge] = []

    for (a, b), count in pair_counts.items():
        weight = count / max_count
        edges.append(
            GraphEdge(
                edge_id=f'cochange:{a}:{b}',
                source=f'file:{a}',
                target=f'file:{b}',
                relation='cochange',
                layer='COCHANGE',
                confidence='EXTRACTED',
                weight=weight,
                metadata={'count': count, 'max_count': max_count},
            )
        )

    return edges
