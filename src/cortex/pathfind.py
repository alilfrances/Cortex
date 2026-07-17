from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import GraphEdge, GraphNode

DEFAULT_EXCLUDE_LAYERS = frozenset({"COCHANGE"})


def shortest_paths(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    source_id: str,
    target_id: str,
    max_depth: int = 6,
    max_paths: int = 3,
    exclude_layers: frozenset[str] = DEFAULT_EXCLUDE_LAYERS,
) -> list[list[dict[str, Any]]]:
    """Up to ``max_paths`` shortest undirected paths between two nodes.

    Each path is a list of hops ``{node, relation, direction, layer,
    confidence, edge_id}`` where ``node`` is the node stepped onto and
    ``direction`` says whether the edge was followed source->target ("out")
    or against it ("in"). Commit nodes and excluded layers never appear.
    """
    if source_id == target_id or max_depth < 1 or max_paths < 1:
        return []

    commit_ids = {node.node_id for node in nodes if node.kind == "commit"}
    adjacency: defaultdict[str, list[tuple[str, GraphEdge, str]]] = defaultdict(list)
    for edge in edges:
        if edge.layer in exclude_layers:
            continue
        if edge.source in commit_ids or edge.target in commit_ids:
            continue
        adjacency[edge.source].append((edge.target, edge, "out"))
        adjacency[edge.target].append((edge.source, edge, "in"))

    distance = {source_id: 0}
    parents: defaultdict[str, list[tuple[str, GraphEdge, str]]] = defaultdict(list)
    frontier = [source_id]
    depth = 0
    while frontier and depth < max_depth and target_id not in distance:
        depth += 1
        next_frontier: list[str] = []
        for node_id in frontier:
            for neighbor, edge, direction in adjacency.get(node_id, []):
                if neighbor == source_id:
                    continue
                if neighbor not in distance:
                    distance[neighbor] = depth
                    next_frontier.append(neighbor)
                    parents[neighbor].append((node_id, edge, direction))
                elif distance[neighbor] == depth:
                    parents[neighbor].append((node_id, edge, direction))
        frontier = next_frontier

    if target_id not in distance:
        return []

    paths: list[list[dict[str, Any]]] = []

    def backtrack(node_id: str, suffix: list[dict[str, Any]]) -> None:
        if len(paths) >= max_paths:
            return
        if node_id == source_id:
            paths.append(suffix)
            return
        for prev_id, edge, direction in parents[node_id]:
            hop = {
                "node": node_id,
                "relation": edge.relation,
                "direction": direction,
                "layer": edge.layer,
                "confidence": edge.confidence,
                "edge_id": edge.edge_id,
            }
            backtrack(prev_id, [hop, *suffix])

    backtrack(target_id, [])
    return paths
