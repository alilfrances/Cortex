from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
from typing import Any

from .community import detect_communities
from .deadcode import analyze_dead_code
from .gitutils import discover_repo_root
from .hotspots import top_hotspots
from .models import GraphEdge, GraphNode
from .store import CortexStore, default_db_path


def _normalized_test_base(path: str) -> tuple[str, bool]:
    parts = Path(path).parts
    stem = Path(path).stem
    is_test = "tests" in parts or stem.startswith("test_") or stem.endswith("_test")
    base = re.sub(r"^(test_)+", "", stem)
    base = re.sub(r"(_test)+$", "", base)
    return base, is_test


def _looks_like_src_test_pair(source: str, target: str) -> bool:
    source_base, source_is_test = _normalized_test_base(source)
    target_base, target_is_test = _normalized_test_base(target)
    return source_is_test != target_is_test and source_base == target_base


def default_report_path(repo_path: Path, db_path: Path | None = None) -> Path:
    resolved_db = db_path or default_db_path(repo_path.resolve())
    return resolved_db.parent / "cortex_report.md"


def _god_nodes(nodes: list[GraphNode], edges: list[GraphEdge], top_n: int = 5) -> list[tuple[GraphNode, int]]:
    degree: Counter[str] = Counter()
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1

    file_nodes = [node for node in nodes if node.kind == "file"]
    sorted_nodes = sorted(file_nodes, key=lambda node: (-degree[node.node_id], node.node_id))
    return [(node, degree[node.node_id]) for node in sorted_nodes[:top_n]]


def _surprising_connections(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    node_community: dict[str, int],
    top_n: int = 5,
    include_test_pairs: bool = False,
) -> list[tuple[str, str, str, float]]:
    node_label = {node.node_id: node.label for node in nodes}
    surprises: list[tuple[str, str, str, float]] = []

    for edge in edges:
        if edge.layer != "COCHANGE" or edge.relation != "cochange":
            continue

        source_community = node_community.get(edge.source)
        target_community = node_community.get(edge.target)
        if source_community is None or target_community is None or source_community == target_community:
            continue

        source_label = node_label.get(edge.source, edge.source)
        target_label = node_label.get(edge.target, edge.target)
        if not include_test_pairs and _looks_like_src_test_pair(source_label, target_label):
            continue

        surprises.append(
            (
                source_label,
                target_label,
                f"co-change weight={edge.weight:.2f}",
                edge.weight,
            )
        )

    surprises.sort(key=lambda item: (-item[3], item[0], item[1]))
    return surprises[:top_n]


def build_report_data(
    repo_path: Path,
    db_path: Path | None = None,
    include_test_pairs: bool = False,
) -> dict[str, Any]:
    """Collect the deterministic, JSON-serializable inputs to a report.

    Community detection remains here (rather than in the renderer), so every
    report format has the same analysis and continues to persist communities.
    """
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    nodes, edges = store.fetch_graph(repo_root)

    detected_communities = detect_communities(nodes, edges)
    node_community = {
        node_id: community.community_id
        for community in detected_communities
        for node_id in community.node_ids
    }
    store.save_communities(repo_root, detected_communities)

    communities = [
        {"community_id": community.community_id, "node_ids": sorted(community.node_ids), "label": community.label}
        for community in sorted(detected_communities, key=lambda item: item.community_id)
    ]
    return {
        "repo_name": repo_root.name,
        "repo_path": str(repo_root),
        "file_count": sum(1 for node in nodes if node.kind == "file"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "communities": communities,
        "god_nodes": [
            {"path": node.label, "connections": degree}
            for node, degree in _god_nodes(nodes, edges)
        ],
        "hotspots": top_hotspots(nodes),
        "surprising_connections": [
            {"source": source, "target": target, "note": note, "weight": weight}
            for source, target, note, weight in _surprising_connections(
                nodes, edges, node_community, include_test_pairs=include_test_pairs
            )
        ],
        "dead_code": analyze_dead_code(repo_root, store=store, nodes=nodes, edges=edges)["findings"],
    }


def render_report(
    data: Mapping[str, Any],
    dead_code: Sequence[Mapping[str, Any]],
    omitted_count: int = 0,
) -> str:
    """Render report data as Markdown using the supplied dead-code findings.

    ``dead_code`` is deliberately an explicit input so callers can apply a
    presentation budget without re-running analysis.  A zero omission count
    produces the legacy report text exactly.
    """
    communities = data["communities"]
    lines = [
        f"# Cortex Report: {data['repo_name']}",
        "",
        f"- Files: {data['file_count']}",
        f"- Total Nodes: {data['node_count']}",
        f"- Edges: {data['edge_count']}",
        f"- Communities: {len(communities)}",
        "",
        "## God Nodes (Most Connected Files)",
    ]
    lines.extend(f"- `{item['path']}` — {item['connections']} connections" for item in data["god_nodes"])

    lines.extend(["", "## Hotspots"])
    hotspots = data["hotspots"]
    if hotspots:
        lines.extend(
            f"- `{item['path']}` — score={item['score']} (churn={item['churn']}, complexity={item['complexity']})"
            for item in hotspots
        )
    else:
        lines.append("- None detected yet.")

    lines.extend(["", "## Communities"])
    for community in sorted(communities, key=lambda item: (-len(item["node_ids"]), item["community_id"]))[:10]:
        file_members = [node_id for node_id in community["node_ids"] if node_id.startswith("file:")][:5]
        member_labels = [node_id.removeprefix("file:") for node_id in file_members]
        preview = ", ".join(member_labels) if member_labels else "no file nodes"
        lines.append(f"- Community {community['community_id']} ({len(community['node_ids'])} nodes): {preview}")

    lines.extend(["", "## Surprising Cross-Community Connections"])
    surprises = data["surprising_connections"]
    if surprises:
        lines.extend(f"- `{item['source']}` ↔ `{item['target']}` ({item['note']})" for item in surprises)
    else:
        lines.append("- None detected yet. Run `cortex enrich .` for deeper semantic analysis.")

    lines.extend(["", "## Dead Code Candidates"])
    if dead_code:
        lines.extend(
            f"- `{item['symbol']}` — `{item['file']}:{item['line']}` — {item['confidence']}: {item['reason']}"
            for item in dead_code
        )
    if omitted_count > 0:
        lines.append(
            f"- {omitted_count} additional candidate(s) omitted; use `cortex_dead_code` for the complete list."
        )
    elif not dead_code:
        lines.append("- None detected yet.")

    return "\n".join(lines).strip()


def generate_report(
    repo_path: Path,
    db_path: Path | None = None,
    out_dir: Path | None = None,
    include_test_pairs: bool = False,
) -> str:
    data = build_report_data(repo_path, db_path=db_path, include_test_pairs=include_test_pairs)
    report = render_report(data, data["dead_code"])

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "cortex_report.md").write_text(report, encoding="utf-8")
    return report


def write_report(repo_path: Path, db_path: Path | None = None) -> Path:
    repo_root = discover_repo_root(repo_path)
    report_path = default_report_path(repo_root, db_path=db_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(generate_report(repo_root, db_path=db_path), encoding="utf-8")
    return report_path
