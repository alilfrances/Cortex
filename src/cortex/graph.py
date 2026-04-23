from __future__ import annotations

from collections import defaultdict

from .models import CommitRecord, GraphEdge, GraphNode, SourceRecord


def build_graph(sources: list[SourceRecord], commits: list[CommitRecord]) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    file_nodes: dict[str, str] = {}
    section_index: defaultdict[str, int] = defaultdict(int)

    for source in sources:
        file_node_id = f"file:{source.path}"
        file_nodes[source.path] = file_node_id
        nodes.append(
            GraphNode(
                node_id=file_node_id,
                kind="file",
                label=source.path,
                source_ref=source.path,
                metadata={"kind": source.kind, "size_bytes": source.size_bytes},
            )
        )

        if source.kind in {"markdown", "text", "code"}:
            for line in source.content.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    if not title:
                        continue
                    section_index[source.path] += 1
                    section_id = f"section:{source.path}:{section_index[source.path]}"
                    nodes.append(
                        GraphNode(
                            node_id=section_id,
                            kind="section",
                            label=title,
                            source_ref=source.path,
                            metadata={"path": source.path},
                        )
                    )
                    edges.append(
                        GraphEdge(
                            edge_id=f"edge:{file_node_id}:{section_id}",
                            source=file_node_id,
                            target=section_id,
                            relation="contains",
                        )
                    )

    for commit in commits:
        commit_node_id = f"commit:{commit.sha}"
        nodes.append(
            GraphNode(
                node_id=commit_node_id,
                kind="commit",
                label=commit.summary,
                source_ref=commit.sha,
                metadata={"author": commit.author, "authored_at": commit.authored_at},
            )
        )
        for file_path in commit.files:
            file_node_id = file_nodes.get(file_path)
            if file_node_id is None:
                continue
            edges.append(
                GraphEdge(
                    edge_id=f"edge:{commit.sha}:{file_path}",
                    source=commit_node_id,
                    target=file_node_id,
                    relation="touches",
                )
            )

    return nodes, edges
