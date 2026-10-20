from __future__ import annotations

from collections import defaultdict

from cortex.models import GraphEdge, GraphNode
from cortex.rank import (
    CONVERGENCE_TOLERANCE,
    DAMPING,
    MAX_ITERATIONS,
    _normalized_personalization,
    _transition_graph,
    personalized_pagerank,
)


def _reference_pagerank(nodes, edges, seed_scores, damping=DAMPING, tolerance=CONVERGENCE_TOLERANCE, max_iterations=MAX_ITERATIONS):
    """The straightforward implementation the optimised one must equal exactly."""
    adjacency = _transition_graph(nodes, edges)
    if not adjacency:
        return {}
    node_ids = list(adjacency)
    personalization = _normalized_personalization(node_ids, seed_scores)
    ranks = dict(personalization)
    for _ in range(max_iterations):
        dangling_mass = sum(ranks[node_id] for node_id in node_ids if not adjacency[node_id])
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
    total = sum(ranks[node_id] for node_id in node_ids)
    if total <= 0:
        return ranks
    return {node_id: ranks[node_id] / total for node_id in node_ids}


def _graph(size: int):
    nodes = [GraphNode(node_id=f"file:f{i}", kind="file", label=f"f{i}", source_ref=f"f{i}", granularity="file") for i in range(size)]
    edges = []
    for i in range(size):
        for step, layer, weight in ((1, "STRUCTURAL", 1.0), (7, "COCHANGE", 0.37), (13, "HEADING", 0.9)):
            target = (i * step + 3) % size
            if target != i:
                edges.append(
                    GraphEdge(edge_id=f"e{i}-{step}", source=f"file:f{i}", target=f"file:f{target}", relation="r", layer=layer, weight=weight)
                )
    nodes.append(GraphNode(node_id="file:island", kind="file", label="island", source_ref="island", granularity="file"))
    return nodes, edges


def test_optimised_pagerank_is_bit_identical_to_the_reference():
    nodes, edges = _graph(300)
    seeds = defaultdict(float)
    for i in (1, 17, 90, 250):
        seeds[f"file:f{i}"] = float(i)
    assert personalized_pagerank(nodes, edges, dict(seeds)) == _reference_pagerank(nodes, edges, dict(seeds))


def test_unseeded_pagerank_is_bit_identical_to_the_reference():
    nodes, edges = _graph(120)
    assert personalized_pagerank(nodes, edges, {}) == _reference_pagerank(nodes, edges, {})
