"""Reciprocal Rank Fusion (RRF) for combining ranked-list retrieval signals.

P0-2 (IMPROVEMENT_PLAN.md): Cortex's bundle ranking (`bundle.py::generate_bundle`)
combined a name/keyword score with graph proximity/pagerank directly on one
hand-tuned numeric scale. Adding FTS5 body-text search (`CortexStore.search_fulltext`)
introduces a second ranking signal -- BM25 -- on a *different* scale that
would need its own calibration against `NAME_MATCH_BONUS`/`PATH_MATCH_BONUS`
if mixed in directly. RRF sidesteps that: every input is just an ordering
(a ranked list of ids), and fusion only cares about rank position, not raw
score magnitude, so no cross-signal calibration is needed.

This module is intentionally generic -- `rrf_fuse` accepts **N** ranked
lists of arbitrary hashable ids, not just two, so a future optional
retriever (P1-7's static-embedding search) can be appended as one more list
without changing this function's signature or the call sites that already
use it.
"""

from __future__ import annotations

from typing import Hashable, Sequence, TypeVar

T = TypeVar("T", bound=Hashable)


def rrf_fuse(ranked_lists: Sequence[Sequence[T]], k: int = 60) -> dict[T, float]:
    """Fuse N ranked lists into one score per id via Reciprocal Rank Fusion.

    For every list, each id contributes ``1 / (k + rank)`` to its fused
    score, where ``rank`` is the id's 1-based position within *that* list
    (ids the list doesn't contain contribute nothing from it). Scores from
    all lists are summed. This is the standard RRF formulation (Cormack,
    Clarke & Buettcher, 2009); ``k=60`` is their recommended default and
    Cortex's, chosen because it is large enough that a handful of
    low-ranked, low-confidence hits can't dominate over one strong top hit
    in any single list.

    A duplicate id within a single input list is credited only once, at its
    *best* (first-seen, lowest-numbered) rank in that list -- callers should
    not rely on later duplicates affecting the score.

    Deterministic and side-effect-free: same inputs always produce the same
    output dict, order of ``ranked_lists`` does not matter (fusion is
    commutative), and empty lists / empty input are handled without error
    (returns ``{}``).
    """
    scores: dict[T, float] = {}
    for ranked in ranked_lists:
        seen: set[T] = set()
        rank = 0
        for item_id in ranked:
            if item_id in seen:
                continue
            seen.add(item_id)
            rank += 1
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return scores
