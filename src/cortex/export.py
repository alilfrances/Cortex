from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import Community, GraphEdge, GraphNode

_GRAPHML_NS = "http://graphml.graphdrawing.org/xmlns"
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|#^[\]\n\r\t]+')


def _community_map(communities: Mapping[str, int] | Sequence[Community]) -> dict[str, int]:
    if isinstance(communities, Mapping):
        return {str(node_id): int(community_id) for node_id, community_id in communities.items()}
    node_to_community: dict[str, int] = {}
    for community in communities:
        for node_id in community.node_ids:
            node_to_community[node_id] = community.community_id
    return node_to_community


def _safe_filename(value: str, fallback: str = "note") -> str:
    name = _UNSAFE_FILENAME.sub("_", value).strip(" ._")
    return name or fallback


def _dedupe_filename(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def export_graphml(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    communities: Mapping[str, int] | Sequence[Community],
    path: Path,
) -> None:
    community_by_node = _community_map(communities)
    ET.register_namespace("", _GRAPHML_NS)
    graphml = ET.Element(f"{{{_GRAPHML_NS}}}graphml")
    for key_id, target, attr_type in (
        ("kind", "node", "string"),
        ("granularity", "node", "string"),
        ("community", "node", "int"),
        ("layer", "edge", "string"),
        ("relation", "edge", "string"),
        ("weight", "edge", "double"),
        ("confidence", "edge", "string"),
    ):
        ET.SubElement(
            graphml,
            f"{{{_GRAPHML_NS}}}key",
            id=key_id,
            **{"for": target, "attr.name": key_id, "attr.type": attr_type},
        )
    graph = ET.SubElement(graphml, f"{{{_GRAPHML_NS}}}graph", edgedefault="directed")
    for node in nodes:
        elem = ET.SubElement(graph, f"{{{_GRAPHML_NS}}}node", id=node.node_id)
        for key, value in (
            ("kind", node.kind),
            ("granularity", node.granularity),
            ("community", community_by_node.get(node.node_id)),
        ):
            data = ET.SubElement(elem, f"{{{_GRAPHML_NS}}}data", key=key)
            data.text = "" if value is None else str(value)
    for edge in edges:
        elem = ET.SubElement(
            graph,
            f"{{{_GRAPHML_NS}}}edge",
            id=edge.edge_id,
            source=edge.source,
            target=edge.target,
        )
        for key, value in (
            ("layer", edge.layer),
            ("relation", edge.relation),
            ("weight", edge.weight),
            ("confidence", edge.confidence),
        ):
            data = ET.SubElement(elem, f"{{{_GRAPHML_NS}}}data", key=key)
            data.text = str(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(graphml).write(path, encoding="utf-8", xml_declaration=True)


def export_json(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    communities: Mapping[str, int] | Sequence[Community],
    path: Path,
) -> None:
    community_by_node = _community_map(communities)
    payload = {
        "nodes": [node.to_dict() | {"community": community_by_node.get(node.node_id)} for node in nodes],
        "edges": [edge.to_dict() for edge in edges],
        "communities": community_by_node,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def export_obsidian(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    communities: Mapping[str, int] | Sequence[Community],
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    community_by_node = _community_map(communities)
    file_nodes = [node for node in nodes if node.granularity == "file"]
    file_by_id = {node.node_id: node for node in file_nodes}
    used: set[str] = set()
    note_names: dict[str, str] = {}
    for node in file_nodes:
        label = node.label or node.source_ref or node.node_id
        safe = _dedupe_filename(_safe_filename(label), used)
        note_names[node.node_id] = safe

    neighbors: dict[str, list[tuple[GraphEdge, str]]] = {node.node_id: [] for node in file_nodes}
    for edge in edges:
        if edge.source in file_by_id and edge.target in file_by_id:
            neighbors[edge.source].append((edge, edge.target))
            neighbors[edge.target].append((edge, edge.source))

    for node in file_nodes:
        community_id = community_by_node.get(node.node_id)
        tags = [f"cortex/{_safe_filename(node.kind.lower(), 'unknown')}"]
        if community_id is not None:
            tags.append(f"community/{community_id}")
        lines = [
            "---",
            f'id: "{node.node_id}"',
            f'kind: "{node.kind}"',
            f'granularity: "{node.granularity}"',
            f'community: {"null" if community_id is None else community_id}',
            "tags:",
            *[f"  - {tag}" for tag in tags],
            "---",
            "",
            f"# {node.label}",
            "",
            f"Source: `{node.source_ref}`",
            "",
            "## Links",
            "",
        ]
        linked = sorted(neighbors[node.node_id], key=lambda item: note_names[item[1]])
        if linked:
            for edge, other_id in linked:
                lines.append(f"- [[{note_names[other_id]}]] ({edge.relation}, {edge.layer})")
        else:
            lines.append("- No file-level neighbors.")
        (out_dir / f"{note_names[node.node_id]}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    members_by_community: dict[int, list[GraphNode]] = {}
    for node in file_nodes:
        community_id = community_by_node.get(node.node_id)
        if community_id is not None:
            members_by_community.setdefault(community_id, []).append(node)
    for community_id, members in sorted(members_by_community.items()):
        lines = [
            "---",
            f"community: {community_id}",
            "tags:",
            f"  - community/{community_id}",
            "---",
            "",
            f"# Community {community_id}",
            "",
            "## Members",
            "",
        ]
        for node in sorted(members, key=lambda item: note_names[item.node_id]):
            lines.append(f"- [[{note_names[node.node_id]}]]")
        (out_dir / f"community_{community_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    index = [
        "# Cortex Graph Index",
        "",
        f"- File notes: {len(file_nodes)}",
        f"- Communities: {len(members_by_community)}",
        "",
        "## Communities",
        "",
    ]
    for community_id in sorted(members_by_community):
        index.append(f"- [[community_{community_id}]]")
    index.extend(["", "## Files", ""])
    for node in sorted(file_nodes, key=lambda item: note_names[item.node_id]):
        index.append(f"- [[{note_names[node.node_id]}]]")
    (out_dir / "index.md").write_text("\n".join(index) + "\n", encoding="utf-8")
