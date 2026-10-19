from __future__ import annotations

from collections import Counter, defaultdict

from .deadline import check_deadline
from .models import Community, GraphEdge, GraphNode

COCHANGE_WEIGHT_FLOOR = 0.1
# Symbol-level call edges are not a file-cluster signal and, since 0.10.0, make
# up most of the graph (about two thirds of all edges on a large repository), which
# made label propagation several times slower and changed the communities.
EXCLUDED_RELATIONS = frozenset({"calls"})


def detect_communities(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    max_iterations: int = 30,
) -> list[Community]:
    """
    Label propagation community detection. Pure Python, no external deps.
    Each node starts with its own label. Each iteration, every node adopts
    the most common label among its neighbors. Stops when stable.
    """
    if not nodes:
        return []

    node_ids = [n.node_id for n in nodes]
    propagation_node_ids = {
        node.node_id
        for node in nodes
        if node.kind != 'commit' and not node.node_id.startswith('commit:')
    }

    # Build undirected adjacency list, keeping only edges that carry useful
    # community signal for propagation.
    neighbors: defaultdict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.source not in propagation_node_ids or edge.target not in propagation_node_ids:
            continue
        if edge.relation in EXCLUDED_RELATIONS:
            continue
        if edge.layer == 'HEADING' and edge.relation == 'contains':
            continue
        if edge.layer == 'COCHANGE' and edge.weight < COCHANGE_WEIGHT_FLOOR:
            continue
        neighbors[edge.source].append(edge.target)
        neighbors[edge.target].append(edge.source)

    # Each node starts with its own unique integer label
    labels: dict[str, int] = {nid: i for i, nid in enumerate(node_ids)}

    for _ in range(max_iterations):
        # Bounded at max_iterations, but each pass is O(edges) in pure
        # Python; on a large graph the whole loop is still seconds of CPU,
        # so it honours the caller's budget between passes.
        check_deadline()
        changed = False
        for nid in node_ids:
            nbrs = neighbors[nid]
            if not nbrs:
                continue
            neighbor_labels = [labels[n] for n in nbrs if n in labels]
            if not neighbor_labels:
                continue
            most_common = Counter(neighbor_labels).most_common(1)[0][0]
            if labels[nid] != most_common:
                labels[nid] = most_common
                changed = True
        if not changed:
            break

    # Group nodes by final label
    groups: defaultdict[int, list[str]] = defaultdict(list)
    for nid, label in labels.items():
        groups[label].append(nid)

    return [
        Community(community_id=cid, node_ids=members)
        for cid, (_, members) in enumerate(groups.items())
    ]
