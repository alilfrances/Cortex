from __future__ import annotations

from collections import defaultdict

from .deadline import check_deadline
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


def _normalized_personalization(node_ids: list[str], seed_scores: dict[str, float]) -> dict[str, float]:
    node_id_set = set(node_ids)
    positive = {node_id: score for node_id, score in seed_scores.items() if node_id in node_id_set and score > 0}
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
    # Preserve node-list order so downstream floating-point accumulation is deterministic.
    for node in nodes:
        if node.kind != "commit" and not node.node_id.startswith("commit:"):
            adjacency[node.node_id]

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


def collapse_to_file_graph(
    nodes: list[GraphNode], edges: list[GraphEdge]
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """File-level view of a graph for ranking files.

    Every symbol node maps to its `file:<source_ref>` node, edges that become
    self-loops are dropped, and parallel edges are merged per (source, target,
    layer) with their weights summed (the layer multiplier is linear, so a merged
    edge carries the same total transition weight). Commit nodes and edges that
    touch unknown nodes are omitted, exactly as `_transition_graph` omits them.
    Merged edges are emitted in sorted order so float accumulation is deterministic.
    """
    collapsed: dict[str, str] = {}
    kept: list[GraphNode] = []
    kept_ids: set[str] = set()
    for node in nodes:
        if node.kind == "commit" or node.node_id.startswith("commit:"):
            continue
        if node.granularity == "symbol":
            collapsed[node.node_id] = f"file:{node.source_ref}"
            continue
        collapsed[node.node_id] = node.node_id
        if node.node_id not in kept_ids:
            kept_ids.add(node.node_id)
            kept.append(node)
    merged: defaultdict[tuple[str, str, str], float] = defaultdict(float)
    for edge in edges:
        source, target = collapsed.get(edge.source), collapsed.get(edge.target)
        if source is None or target is None or source == target or edge.weight <= 0:
            continue
        if source not in kept_ids or target not in kept_ids:
            continue
        merged[(source, target, edge.layer)] += edge.weight
    file_edges = [
        GraphEdge(
            edge_id=f"file-rank:{source}>{target}>{layer}",
            source=source,
            target=target,
            relation="aggregate",
            layer=layer,
            confidence="EXTRACTED",
            weight=weight,
            metadata={},
        )
        for (source, target, layer), weight in sorted(merged.items())
    ]
    return kept, file_edges


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

    node_ids = list(adjacency)
    personalization = _normalized_personalization(node_ids, seed_scores)
    ranks = dict(personalization)

    # Neither the dangling set nor each node's normalised outgoing weights change
    # between iterations, so compute them once. `weight / outgoing_weight` is
    # evaluated exactly as before, so the results are bit-identical.
    dangling_ids = [node_id for node_id in node_ids if not adjacency[node_id]]
    outgoing: list[tuple[str, list[tuple[str, float]]]] = []
    for source, neighbors in adjacency.items():
        if not neighbors:
            continue
        outgoing_weight = sum(weight for _target, weight in neighbors)
        if outgoing_weight <= 0:
            continue
        outgoing.append((source, [(target, weight / outgoing_weight) for target, weight in neighbors]))

    for _ in range(max_iterations):
        # Cooperative MCP deadline; a no-op outside a deadline scope.
        check_deadline()
        dangling_mass = sum(ranks[node_id] for node_id in dangling_ids)
        next_ranks = {
            node_id: (1.0 - damping) * personalization[node_id] + damping * dangling_mass * personalization[node_id]
            for node_id in node_ids
        }

        for source, normalised in outgoing:
            source_rank = ranks[source]
            for target, share in normalised:
                next_ranks[target] += damping * source_rank * share

        delta = sum(abs(next_ranks[node_id] - ranks[node_id]) for node_id in node_ids)
        ranks = next_ranks
        if delta < tolerance:
            break

    total = sum(ranks[node_id] for node_id in node_ids)
    if total <= 0:
        return ranks
    return {node_id: ranks[node_id] / total for node_id in node_ids}
