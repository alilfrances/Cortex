from __future__ import annotations

import json
from collections import defaultdict

from .models import GraphEdge, GraphNode
from .tokenizer import count_text_tokens


IMPACT_LAYERS = {"COCHANGE", "STRUCTURAL"}


class UnknownPathError(ValueError):
    """Raised when the requested path has no matching file node in the graph."""

    def __init__(self, path: str):
        super().__init__(f"path not found in graph: {path}")
        self.path = path


def _file_node_id(path: str) -> str:
    return path if path.startswith("file:") else f"file:{path}"


def rank_file_impact(
    path: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    limit: int = 10,
    budget: int = 2000,
) -> tuple[list[dict], bool]:
    seed_id = _file_node_id(path)
    file_paths = {node.node_id: node.source_ref for node in nodes if node.granularity == "file" or node.kind == "file"}
    if seed_id not in file_paths:
        raise UnknownPathError(path)
    scores: defaultdict[str, float] = defaultdict(float)
    why: defaultdict[str, list[dict]] = defaultdict(list)

    for edge in edges:
        if edge.layer not in IMPACT_LAYERS:
            continue
        if edge.source == seed_id:
            neighbor = edge.target
        elif edge.target == seed_id:
            neighbor = edge.source
        else:
            continue
        if neighbor == seed_id or neighbor not in file_paths:
            continue
        scores[neighbor] += edge.weight
        why[neighbor].append({"relation": edge.relation, "weight": edge.weight})

    ranked = sorted(scores, key=lambda node_id: (-scores[node_id], file_paths[node_id], node_id))
    result = [
        {
            "path": file_paths[node_id],
            "node_id": node_id,
            "score": scores[node_id],
            "why": sorted(why[node_id], key=lambda item: (-item["weight"], item["relation"])),
        }
        for node_id in ranked[:limit]
    ]
    truncated = False
    while result and count_text_tokens(json.dumps(result)) > budget:
        result.pop()
        truncated = True
    return result, truncated
