from __future__ import annotations

import hashlib
from collections import defaultdict

from .ast_extract import extract_python_edges
from .cochange import build_cochange_edges
from .models import CommitRecord, GraphEdge, GraphNode, SourceRecord
from .structural import extract_structural_edges, supports_path


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


def build_graph(
    sources: list[SourceRecord],
    commits: list[CommitRecord],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    file_nodes: dict[str, str] = {}
    section_index: defaultdict[str, int] = defaultdict(int)
    known_paths = {s.path for s in sources}

    for source in sources:
        file_node_id = f'file:{source.path}'
        file_nodes[source.path] = file_node_id
        nodes.append(
            GraphNode(
                node_id=file_node_id,
                kind='file',
                label=source.path,
                source_ref=source.path,
                metadata={'kind': source.kind, 'size_bytes': source.size_bytes},
            )
        )

        # Heading edges (HEADING layer)
        if source.kind == 'markdown':
            for line in source.content.splitlines():
                if line.startswith('#'):
                    title = line.lstrip('#').strip()
                    if not title:
                        continue
                    section_index[source.path] += 1
                    section_id = f'section:{source.path}:{section_index[source.path]}'
                    nodes.append(
                        GraphNode(
                            node_id=section_id,
                            kind='section',
                            label=title,
                            source_ref=source.path,
                            metadata={'path': source.path},
                        )
                    )
                    edges.append(
                        GraphEdge(
                            edge_id=f'edge:{file_node_id}:{section_id}',
                            source=file_node_id,
                            target=section_id,
                            relation='contains',
                            layer='HEADING',
                            confidence='EXTRACTED',
                            weight=1.0,
                        )
                    )

        # AST edges for Python files (STRUCTURAL layer)
        if source.kind == 'code' and source.path.endswith('.py'):
            ast_nodes, ast_edges = extract_python_edges(source.path, source.content, known_paths)
            nodes.extend(ast_nodes)
            edges.extend(ast_edges)
        elif source.kind == 'code' and supports_path(source.path):
            structural_nodes, structural_edges = extract_structural_edges(source.path, source.content, known_paths)
            nodes.extend(structural_nodes)
            edges.extend(structural_edges)

    # Co-change edges from git history (COCHANGE layer)
    edges.extend(build_cochange_edges(commits))

    # Commit -> file edges (provenance)
    for commit in commits:
        commit_node_id = f'commit:{commit.sha}'
        nodes.append(
            GraphNode(
                node_id=commit_node_id,
                kind='commit',
                label=commit.summary,
                source_ref=commit.sha,
                metadata={'author': commit.author, 'authored_at': commit.authored_at},
            )
        )
        for file_path in commit.files:
            file_node_id = file_nodes.get(file_path)
            if file_node_id is None:
                continue
            edges.append(
                GraphEdge(
                    edge_id=f'edge:{commit.sha}:{file_path}',
                    source=commit_node_id,
                    target=file_node_id,
                    relation='touches',
                    layer='COCHANGE',
                    confidence='EXTRACTED',
                    weight=1.0,
                )
            )

    return nodes, edges
