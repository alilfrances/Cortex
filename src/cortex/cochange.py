from __future__ import annotations

from collections import Counter

from .gitutils import canonical_commit_files
from .models import CommitRecord, GraphEdge

# Commits touching more files than this (bulk refactors, vendor imports)
# would create O(n^2) meaningless pairs, so they are ignored.
MAX_COMMIT_FILES = 50


def build_cochange_edges(commits: list[CommitRecord]) -> list[GraphEdge]:
    """
    Build COCHANGE edges from git commit history.
    Files appearing together in same commits are co-changed.
    Edge weight = co-occurrence count / max count across all pairs.

    Rename-aware (0.10.0): ``commits`` (newest first) are mapped through
    :func:`cortex.gitutils.canonical_commit_files`, so a pair is counted
    against each file's current path even for commits made before a rename.
    Paths are normalised and de-duplicated per commit, so a commit never
    pairs a file with itself.
    """
    pair_counts: Counter[tuple[str, str]] = Counter()

    for files in canonical_commit_files(commits):
        if len(files) > MAX_COMMIT_FILES:
            continue
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
