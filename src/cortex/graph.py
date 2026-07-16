from __future__ import annotations

import hashlib
from collections import defaultdict

from .ast_extract import extract_python_edges
from .cochange import build_cochange_edges
from .models import CommitRecord, GraphEdge, GraphNode, SourceRecord
from .structural import extract_structural_edges, supports_path


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


def build_file_layer(
    sources: list[SourceRecord],
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """HEADING + STRUCTURAL layer nodes/edges for exactly the given `sources`.

    Every edge produced here is stamped with metadata['source_file'] set to
    the owning source path, so CortexStore.delete_graph_for_sources can prune
    it later without touching any other file's rows (P0-3 delta writes).
    `known_paths` is only used to resolve cross-file references (e.g. Python
    imports to a local module) and may be a superset of `sources` — callers
    doing a partial (incremental) rebuild should pass the full current repo
    path set even though `sources` itself is just the changed/new files.
    """
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    section_index: defaultdict[str, int] = defaultdict(int)

    for source in sources:
        file_node_id = f'file:{source.path}'
        nodes.append(
            GraphNode(
                node_id=file_node_id,
                kind='file',
                label=source.path,
                source_ref=source.path,
                metadata={'kind': source.kind, 'size_bytes': source.size_bytes},
            )
        )

        source_edges: list[GraphEdge] = []

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
                    source_edges.append(
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
            source_edges.extend(ast_edges)
        elif source.kind == 'code' and supports_path(source.path):
            structural_nodes, structural_edges = extract_structural_edges(source.path, source.content, known_paths)
            nodes.extend(structural_nodes)
            source_edges.extend(structural_edges)

        # Regex/tree-sitter structural edges already carry metadata['source_file']
        # (see structural/regex_backend.py, structural/treesitter_backend.py);
        # stamping it here too (idempotent overwrite with the same value) makes
        # every file-layer edge -- Python AST and Markdown headings included --
        # uniformly attributable to one file for delta deletes.
        for edge in source_edges:
            edge.metadata['source_file'] = source.path
        edges.extend(source_edges)

    return nodes, edges


def build_cochange_layer(
    commits: list[CommitRecord],
    known_paths: set[str],
    *,
    filter_cochange_pairs: bool = False,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """COCHANGE layer: file-pair co-change edges plus commit -> file
    provenance ("touches") edges, both derived purely from `commits`.

    Touches edges have always been restricted to `known_paths` (a commit that
    mentions a file we never ingested produces no node to point at). Cochange
    *pair* edges historically were not restricted, so a file deleted from the
    tree could keep a `cochange:` edge referencing it forever. Full ingest
    preserves that historical (unfiltered) behavior for compatibility;
    `filter_cochange_pairs=True` is used by the incremental refresh path to
    also drop pair edges whose file no longer exists (P0-3 COCHANGE
    correctness).
    """
    nodes: list[GraphNode] = []

    cochange_edges = build_cochange_edges(commits)
    if filter_cochange_pairs:
        cochange_edges = [
            e for e in cochange_edges
            if e.source.removeprefix('file:') in known_paths
            and e.target.removeprefix('file:') in known_paths
        ]
    edges: list[GraphEdge] = list(cochange_edges)

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
            if file_path not in known_paths:
                continue
            edges.append(
                GraphEdge(
                    edge_id=f'edge:{commit.sha}:{file_path}',
                    source=commit_node_id,
                    target=f'file:{file_path}',
                    relation='touches',
                    layer='COCHANGE',
                    confidence='EXTRACTED',
                    weight=1.0,
                )
            )

    return nodes, edges


def build_graph(
    sources: list[SourceRecord],
    commits: list[CommitRecord],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    known_paths = {s.path for s in sources}

    nodes, edges = build_file_layer(sources, known_paths)
    cochange_nodes, cochange_edges = build_cochange_layer(commits, known_paths)
    nodes.extend(cochange_nodes)
    edges.extend(cochange_edges)

    return nodes, edges
