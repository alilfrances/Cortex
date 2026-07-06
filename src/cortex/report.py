from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

from .community import detect_communities
from .gitutils import discover_repo_root
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


def default_report_path(repo_path: Path) -> Path:
    repo_root = repo_path.resolve()
    return repo_root / ".cortex" / "cortex_report.md"


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


def generate_report(
    repo_path: Path,
    db_path: Path | None = None,
    out_dir: Path | None = None,
    include_test_pairs: bool = False,
) -> str:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    nodes, edges = store.fetch_graph(repo_root)

    communities = detect_communities(nodes, edges)
    node_community: dict[str, int] = {}
    for community in communities:
        for node_id in community.node_ids:
            node_community[node_id] = community.community_id
    store.save_communities(repo_root, communities)

    god_nodes = _god_nodes(nodes, edges)
    surprises = _surprising_connections(nodes, edges, node_community, include_test_pairs=include_test_pairs)
    file_node_count = sum(1 for node in nodes if node.kind == "file")

    lines = [
        f"# Cortex Report: {repo_root.name}",
        "",
        f"- Files: {file_node_count}",
        f"- Total Nodes: {len(nodes)}",
        f"- Edges: {len(edges)}",
        f"- Communities: {len(communities)}",
        "",
        "## God Nodes (Most Connected Files)",
    ]
    lines.extend(f"- `{node.label}` — {degree} connections" for node, degree in god_nodes)

    lines.extend(["", "## Communities"])
    for community in sorted(communities, key=lambda item: (-len(item.node_ids), item.community_id))[:10]:
        file_members = [node_id for node_id in community.node_ids if node_id.startswith("file:")][:5]
        member_labels = [node_id.removeprefix("file:") for node_id in file_members]
        preview = ", ".join(member_labels) if member_labels else "no file nodes"
        lines.append(f"- Community {community.community_id} ({len(community.node_ids)} nodes): {preview}")

    lines.extend(["", "## Surprising Cross-Community Connections"])
    if surprises:
        for source, target, note, _weight in surprises:
            lines.append(f"- `{source}` ↔ `{target}` ({note})")
    else:
        lines.append("- None detected yet. Run `cortex enrich .` for deeper semantic analysis.")

    report = "\n".join(lines).strip()

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "cortex_report.md").write_text(report, encoding="utf-8")
    return report


def write_report(repo_path: Path, db_path: Path | None = None) -> Path:
    repo_root = discover_repo_root(repo_path)
    report_path = default_report_path(repo_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(generate_report(repo_root, db_path=db_path), encoding="utf-8")
    return report_path
