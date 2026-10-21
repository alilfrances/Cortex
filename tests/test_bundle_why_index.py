"""`_bundle_why` with a prebuilt seed-edge index equals the per-item edge scan it replaced."""
from __future__ import annotations

import random
from typing import Any

from cortex.mcp.tools import _bundle_why, _seed_edge_index
from cortex.models import GraphEdge


def _old_bundle_why(item: dict[str, Any], terms: set[str], seed_paths: set[str], edges: list) -> list[dict[str, Any]]:
    """The implementation shipped through 0.11: scans every edge for every item."""
    node_id = str(item.get("metadata", {}).get("node_id", ""))
    symbol_name = node_id.split(":", 2)[-1] if node_id else ""
    haystack = f"{item.get('path', '')}\n{symbol_name}\n{item.get('content', '')}".lower()
    matched = sorted(term for term in terms if term in haystack)
    why: list[dict[str, Any]] = []
    if matched:
        entry: dict[str, Any] = {"type": "keyword", "terms": matched[:8], "path": item.get("path", "")}
        if node_id:
            entry["node_id"] = node_id
        why.append(entry)
    item_node = f"file:{item.get('path', '')}"
    seed_nodes = {f"file:{path}" for path in seed_paths}
    if item.get("path") in seed_paths:
        why.append({"type": "seed_path", "path": item.get("path", "")})
    contributing_edges = []
    for edge in edges:
        if edge.source == item_node and edge.target in seed_nodes:
            seed = edge.target.removeprefix("file:")
        elif edge.target == item_node and edge.source in seed_nodes:
            seed = edge.source.removeprefix("file:")
        else:
            continue
        contributing_edges.append(
            {"seed_path": seed, "edge_id": edge.edge_id, "layer": edge.layer, "relation": edge.relation, "weight": edge.weight}
        )
    if contributing_edges:
        why.append({"type": "graph_path", "edges": sorted(contributing_edges, key=lambda e: (-e["weight"], e["edge_id"]))[:5]})
    graph_bonus = item.get("metadata", {}).get("graph_bonus", 0.0)
    if graph_bonus:
        why.append({"type": "graph", "score": graph_bonus})
    if not why:
        why.append({"type": "rank", "reason": "selected by Cortex ranking and budget packing"})
    return why


def test_index_matches_the_full_scan_on_random_graphs() -> None:
    rng = random.Random(11)
    paths = [f"p{i}.py" for i in range(12)]
    for trial in range(40):
        seeds = set(rng.sample(paths, rng.randint(0, 6)))
        edges = [
            GraphEdge(
                edge_id=f"e{trial}-{n}",
                source=rng.choice([f"file:{p}" for p in paths] + [f"symbol:{paths[0]}:f"]),
                target=rng.choice([f"file:{p}" for p in paths] + [f"symbol:{paths[1]}:g"]),
                relation=rng.choice(["imports", "calls", "cochange"]),
                layer=rng.choice(["STRUCTURAL", "COCHANGE"]),
                weight=rng.choice([0.25, 0.5, 1.0]),
            )
            for n in range(80)
        ]
        index = _seed_edge_index(seeds, edges)
        for path in paths:
            item = {"path": path, "content": "login token", "metadata": {"graph_bonus": 0.1}}
            assert _bundle_why(item, {"login"}, seeds, edges, seed_edges=index) == _old_bundle_why(item, {"login"}, seeds, edges)
            assert _bundle_why(item, {"login"}, seeds, edges) == _old_bundle_why(item, {"login"}, seeds, edges)


def test_self_loop_on_a_seed_is_listed_once() -> None:
    edge = GraphEdge(edge_id="loop", source="file:a.py", target="file:a.py", relation="imports", layer="STRUCTURAL", weight=1.0)
    item = {"path": "a.py", "content": "", "metadata": {}}

    why = _bundle_why(item, set(), {"a.py"}, [edge], seed_edges=_seed_edge_index({"a.py"}, [edge]))

    assert [entry for entry in why if entry["type"] == "graph_path"][0]["edges"] == [
        {"seed_path": "a.py", "edge_id": "loop", "layer": "STRUCTURAL", "relation": "imports", "weight": 1.0}
    ]
