from __future__ import annotations

import json
from collections import defaultdict

from .models import GraphEdge, GraphNode
from .tokenizer import count_text_tokens


IMPACT_LAYERS = {"COCHANGE", "STRUCTURAL"}

# Real co-change history is a stronger coupling signal than a bare #include.
COCHANGE_IMPACT_MULTIPLIER = 2.0
STRUCTURAL_IMPACT_MULTIPLIER = 1.0

_LAYER_MULTIPLIERS = {
    "COCHANGE": COCHANGE_IMPACT_MULTIPLIER,
    "STRUCTURAL": STRUCTURAL_IMPACT_MULTIPLIER,
}


# Bump when the cortex_impact payload changes meaning, so query-cache rows
# written by an older version are bypassed instead of served (v2: symbol-level
# STRUCTURAL edges are aggregated per file).
IMPACT_CACHE_VERSION = 2

# A neighbour linked by many symbol-level edges lists at most this many of
# them in ``why`` (sorted); the score still counts every distinct edge.
MAX_SYMBOL_WHY_PER_NEIGHBOR = 5

# ``contains`` is the file->own-symbol structure edge; it never crosses files.
_SYMBOL_EDGE_SKIP_RELATIONS = {"contains"}


class UnknownPathError(ValueError):
    """Raised when the requested path has no matching file node in the graph."""

    def __init__(self, path: str):
        super().__init__(f"path not found in graph: {path}")
        self.path = path


def _file_node_id(path: str) -> str:
    return path if path.startswith("file:") else f"file:{path}"


def _aggregate_symbol_edge(
    edge: GraphEdge,
    seed_path: str,
    nodes_by_id: dict[str, GraphNode],
    file_paths: dict[str, str],
    file_id_by_path: dict[str, str],
    scores: defaultdict[str, float],
    symbol_why: defaultdict[str, list[dict]],
    seen_pairs: set[tuple[str, str, str]],
) -> None:
    """Credit a cross-file symbol-level STRUCTURAL edge to the other file.

    Both endpoints must be indexed nodes whose ``source_ref`` is an indexed
    file and the two files must differ; one of them must be the seed.  Several
    call sites between the same two endpoints (one edge per line) count once.
    """
    if edge.relation in _SYMBOL_EDGE_SKIP_RELATIONS:
        return
    source_node = nodes_by_id.get(edge.source)
    target_node = nodes_by_id.get(edge.target)
    if source_node is None or target_node is None:
        return
    if source_node.kind == "commit" or target_node.kind == "commit":
        return
    source_path = source_node.source_ref
    target_path = target_node.source_ref
    if source_path == target_path:
        return
    if source_path == seed_path:
        other_path = target_path
    elif target_path == seed_path:
        other_path = source_path
    else:
        return
    neighbor = file_id_by_path.get(other_path)
    if neighbor is None or neighbor not in file_paths:
        return
    pair = (edge.source, edge.target, edge.relation)
    if pair in seen_pairs:
        return
    seen_pairs.add(pair)
    weight = float(edge.weight)
    scores[neighbor] += weight * _LAYER_MULTIPLIERS.get(edge.layer, 1.0)
    symbol_why[neighbor].append(
        {
            "relation": edge.relation,
            "symbol": f"{source_node.label} -> {target_node.label}",
            "weight": edge.weight,
            "layer": edge.layer,
            "confidence": edge.confidence,
        }
    )


def rank_file_impact(
    path: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    limit: int = 10,
    budget: int = 2000,
) -> tuple[list[dict], bool]:
    seed_id = _file_node_id(path)
    # Commit nodes default to granularity="file" (see models.GraphNode) and are
    # linked to files by COCHANGE "touches" edges, so they must be excluded here
    # or their SHAs surface as bogus impacted "paths" ranked 1.0. Same guard the
    # community/rank layers use.
    file_nodes = {
        node.node_id: node
        for node in nodes
        if (node.granularity == "file" or node.kind == "file")
        and node.kind != "commit"
        and not node.node_id.startswith("commit:")
    }
    file_paths = {node_id: node.source_ref for node_id, node in file_nodes.items()}
    if seed_id not in file_paths:
        raise UnknownPathError(path)
    scores: defaultdict[str, float] = defaultdict(float)
    why: defaultdict[str, list[dict]] = defaultdict(list)
    seed_path = file_paths[seed_id]
    file_id_by_path = {source_ref: node_id for node_id, source_ref in file_paths.items()}
    nodes_by_id = {node.node_id: node for node in nodes}
    symbol_why: defaultdict[str, list[dict]] = defaultdict(list)
    seen_symbol_pairs: set[tuple[str, str, str]] = set()

    for edge in edges:
        if edge.layer not in IMPACT_LAYERS:
            continue
        if edge.layer == "STRUCTURAL" and (edge.source not in file_paths or edge.target not in file_paths):
            # Symbol-level edge (at least one endpoint is not a file node):
            # aggregate it onto the files its endpoints live in.  Unresolved
            # ``name:``/``module:`` endpoints have no node and count nothing.
            _aggregate_symbol_edge(
                edge, seed_path, nodes_by_id, file_paths, file_id_by_path,
                scores, symbol_why, seen_symbol_pairs,
            )
            continue
        if edge.source == seed_id:
            neighbor = edge.target
        elif edge.target == seed_id:
            neighbor = edge.source
        else:
            continue
        if neighbor == seed_id or neighbor not in file_paths:
            continue
        scores[neighbor] += edge.weight * _LAYER_MULTIPLIERS.get(edge.layer, 1.0)
        entry = {
            "relation": edge.relation,
            "weight": edge.weight,
            "layer": edge.layer,
            "confidence": edge.confidence,
        }
        if edge.relation == "cochange" and "count" in edge.metadata:
            entry["cochange_count"] = edge.metadata["count"]
        why[neighbor].append(entry)

    for neighbor, entries in symbol_why.items():
        entries.sort(key=lambda item: (-item["weight"], item["relation"], item["symbol"]))
        why[neighbor].extend(entries[:MAX_SYMBOL_WHY_PER_NEIGHBOR])

    ranked = sorted(scores, key=lambda node_id: (-scores[node_id], file_paths[node_id], node_id))
    result = []
    for node_id in ranked[:limit]:
        raw_hotspot = file_nodes[node_id].metadata.get("hotspot", {})
        hotspot = raw_hotspot if isinstance(raw_hotspot, dict) else {}
        hotspot_churn = int(hotspot.get("churn", 0))
        hotspot_complexity = int(hotspot.get("complexity", 0))
        hotspot_score = int(hotspot.get("score", hotspot_churn * hotspot_complexity))
        result.append(
            {
                "path": file_paths[node_id],
                "node_id": node_id,
                "score": scores[node_id],
                "why": sorted(why[node_id], key=lambda item: (-item["weight"], item["relation"])),
                "hotspot": {
                    "churn": hotspot_churn,
                    "complexity": hotspot_complexity,
                    "score": hotspot_score,
                },
            }
        )
    truncated = False
    while result and count_text_tokens(json.dumps(result)) > budget:
        result.pop()
        truncated = True
    return result, truncated
