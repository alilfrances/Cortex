from __future__ import annotations

from collections import defaultdict

from .models import GraphEdge, GraphNode

DAMPING = 0.85
CONVERGENCE_TOLERANCE = 1e-6
MAX_ITERATIONS = 100

STRUCTURAL_EDGE_MULTIPLIER = 3.0
COCHANGE_EDGE_MULTIPLIER = 1.5
HEADING_EDGE_MULTIPLIER = 0.5
SEMANTIC_EDGE_MULTIPLIER = 2.0

LAYER_EDGE_MULTIPLIERS = {
    "STRUCTURAL": STRUCTURAL_EDGE_MULTIPLIER,
    "SEMANTIC": SEMANTIC_EDGE_MULTIPLIER,
    "COCHANGE": COCHANGE_EDGE_MULTIPLIER,
    "HEADING": HEADING_EDGE_MULTIPLIER,
}


def _normalized_personalization(node_ids: set[str], seed_scores: dict[str, float]) -> dict[str, float]:
    positive = {node_id: score for node_id, score in seed_scores.items() if node_id in node_ids and score > 0}
    total = sum(positive.values())
    if total > 0:
        return {node_id: positive.get(node_id, 0.0) / total for node_id in node_ids}
    if not node_ids:
        return {}
    uniform = 1.0 / len(node_ids)
    return {node_id: uniform for node_id in node_ids}


def _transition_graph(nodes: list[GraphNode], edges: list[GraphEdge]) -> dict[str, list[tuple[str, float]]]:
    # Commit nodes are provenance-only, so omit them and edges touching them from
    # the PageRank graph. File-to-file COCHANGE edges still carry history signal.
    ranked_node_ids = {node.node_id for node in nodes if node.kind != "commit" and not node.node_id.startswith("commit:")}
    adjacency: defaultdict[str, list[tuple[str, float]]] = defaultdict(list)
    for node_id in ranked_node_ids:
        adjacency[node_id]

    for edge in edges:
        if edge.source not in ranked_node_ids or edge.target not in ranked_node_ids:
            continue
        multiplier = LAYER_EDGE_MULTIPLIERS.get(edge.layer, 1.0)
        weighted = edge.weight * multiplier
        if weighted <= 0:
            continue
        adjacency[edge.source].append((edge.target, weighted))
        adjacency[edge.target].append((edge.source, weighted))
    return dict(adjacency)


def personalized_pagerank(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seed_scores: dict[str, float],
    damping: float = DAMPING,
    tolerance: float = CONVERGENCE_TOLERANCE,
    max_iterations: int = MAX_ITERATIONS,
) -> dict[str, float]:
    adjacency = _transition_graph(nodes, edges)
    if not adjacency:
        return {}

    node_ids = set(adjacency)
    personalization = _normalized_personalization(node_ids, seed_scores)
    ranks = dict(personalization)

    for _ in range(max_iterations):
        dangling_mass = sum(ranks[node_id] for node_id, neighbors in adjacency.items() if not neighbors)
        next_ranks = {
            node_id: (1.0 - damping) * personalization[node_id] + damping * dangling_mass * personalization[node_id]
            for node_id in node_ids
        }

        for source, neighbors in adjacency.items():
            if not neighbors:
                continue
            outgoing_weight = sum(weight for _target, weight in neighbors)
            if outgoing_weight <= 0:
                continue
            source_rank = ranks[source]
            for target, weight in neighbors:
                next_ranks[target] += damping * source_rank * (weight / outgoing_weight)

        delta = sum(abs(next_ranks[node_id] - ranks[node_id]) for node_id in node_ids)
        ranks = next_ranks
        if delta < tolerance:
            break

    total = sum(ranks.values())
    if total <= 0:
        return ranks
    return {node_id: rank / total for node_id, rank in ranks.items()}
