from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import NamedTuple

from .ast_extract import extract_python_edges
from .cochange import build_cochange_edges
from .hotspots import annotate_file_nodes, compute_hotspots
from .models import CommitRecord, GraphEdge, GraphNode, SourceRecord
from .structural import extract_structural_edges, supports_path


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


class QtSymbolIndex(NamedTuple):
    """Cross-file lookup for Qt signal/slot declarations and class/component
    locations (P0-4), used to resolve `emits`/`connects`/`handles` edge
    endpoints that still point at a `module:<name>` placeholder once every
    file in a `build_file_layer` batch has been parsed -- a signal's
    declaration (a C++ header, or a QML component file) is often in a
    *different* file than the site referencing it, so single-file parsing
    alone can never resolve it.

    `signals` / `slots`: declaring file path -> {member name: symbol node_id}
    for every symbol tagged metadata['qt'] == 'signal' / 'slot'. C++
    `signals:`/`slots:` sections and QML top-level `signal` declarations both
    feed this (queried by `deviceConnected`-style plain names).
    `classes`: C++ class name / QML component name -> the file path that
    declares it.
    """

    signals: dict[str, dict[str, str]]
    slots: dict[str, dict[str, str]]
    classes: dict[str, str]

    @staticmethod
    def empty() -> "QtSymbolIndex":
        return QtSymbolIndex(signals={}, slots={}, classes={})

    def merged_with(self, other: "QtSymbolIndex") -> "QtSymbolIndex":
        """Combine two indices; `other`'s per-path data wins where both have
        an entry for the same path (it's the fresher parse -- e.g. a header
        reparsed in the same incremental batch that also supplied `self`,
        the store's prior state). Paths present only in `self` (a header
        *not* part of this batch, the common incremental case) pass through
        untouched."""
        signals = {path: dict(names) for path, names in self.signals.items()}
        signals.update({path: dict(names) for path, names in other.signals.items()})
        slots = {path: dict(names) for path, names in self.slots.items()}
        slots.update({path: dict(names) for path, names in other.slots.items()})
        classes = dict(self.classes)
        classes.update(other.classes)
        return QtSymbolIndex(signals=signals, slots=slots, classes=classes)

    def without_paths(self, paths: set[str]) -> "QtSymbolIndex":
        """Drop every entry declared by one of `paths` -- used to keep a
        store-supplied index (fetched before a delta delete) from leaking a
        dangling reference to a file that's about to be deleted or rewritten
        in this same incremental run (P0-3 delta writes)."""
        if not paths:
            return self
        return QtSymbolIndex(
            signals={path: names for path, names in self.signals.items() if path not in paths},
            slots={path: names for path, names in self.slots.items() if path not in paths},
            classes={name: path for name, path in self.classes.items() if path not in paths},
        )


def build_qt_symbol_index(nodes: list[GraphNode]) -> QtSymbolIndex:
    """Derive a `QtSymbolIndex` from a freshly parsed batch of nodes."""
    signals: dict[str, dict[str, str]] = {}
    slots: dict[str, dict[str, str]] = {}
    classes: dict[str, str] = {}
    for node in nodes:
        if node.kind == "class":
            classes.setdefault(node.label, node.source_ref)
        qt_kind = node.metadata.get("qt") if isinstance(node.metadata, dict) else None
        if qt_kind == "signal":
            signals.setdefault(node.source_ref, {})[node.label] = node.node_id
        elif qt_kind == "slot":
            slots.setdefault(node.source_ref, {})[node.label] = node.node_id
    return QtSymbolIndex(signals=signals, slots=slots, classes=classes)


def _repo_unique_match(name: str, *member_indices: dict[str, dict[str, str]]) -> str | None:
    matches = {
        symbol_id
        for members in member_indices
        for by_name in members.values()
        for member_name, symbol_id in by_name.items()
        if member_name == name
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _resolve_class_member(class_name: str | None, member_name: str, index: QtSymbolIndex) -> str | None:
    if not class_name:
        return None
    path = index.classes.get(class_name)
    if path is None:
        return None
    return index.signals.get(path, {}).get(member_name) or index.slots.get(path, {}).get(member_name)


def _resolve_qt_edges(edges: list[GraphEdge], index: QtSymbolIndex) -> None:
    """Second pass (P0-4): resolve `emits`/`connects`/`handles` placeholder
    endpoints against `index` now that every file in this batch has been
    parsed (plus, for an incremental re-ingest, whatever the store already
    knew -- see `QtSymbolIndex.merged_with`). Ordering within the batch never
    matters here: this scans the finished `edges` list once to build the
    "file -> included headers" map, then a second time to resolve, so a
    signal declared in a header parsed *after* the emitting .cpp (or not
    reparsed in this batch at all) still resolves correctly.
    """
    imports_by_file: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.relation == "imports" and edge.target.startswith("file:"):
            imports_by_file[edge.source.removeprefix("file:")].append(edge.target.removeprefix("file:"))

    for edge in edges:
        if edge.relation == "emits":
            name = edge.metadata.get("signal_name")
            if not name or edge.target != f"module:{name}":
                continue
            source_file = edge.metadata.get("source_file", "")
            resolved = None
            for header_path in imports_by_file.get(source_file, ()):
                resolved = index.signals.get(header_path, {}).get(name)
                if resolved:
                    break
            if resolved is None:
                resolved = _repo_unique_match(name, index.signals)
            if resolved:
                edge.target = resolved

        elif edge.relation == "connects":
            signal_name = edge.metadata.get("signal_name")
            if signal_name and edge.source == f"module:{signal_name}":
                resolved = _resolve_class_member(edge.metadata.get("sender_class"), signal_name, index)
                if resolved is None:
                    resolved = _repo_unique_match(signal_name, index.signals, index.slots)
                if resolved:
                    edge.source = resolved
            slot_name = edge.metadata.get("slot_name")
            if slot_name and edge.target == f"module:{slot_name}":
                resolved = _resolve_class_member(edge.metadata.get("receiver_class"), slot_name, index)
                if resolved is None:
                    resolved = _repo_unique_match(slot_name, index.signals, index.slots)
                if resolved:
                    edge.target = resolved

        elif edge.relation == "handles":
            signal_name = edge.metadata.get("signal_name")
            component_path = edge.metadata.get("component_path")
            if not signal_name or not component_path or edge.target != f"module:{signal_name}":
                continue
            resolved = index.signals.get(component_path, {}).get(signal_name)
            if resolved:
                edge.target = resolved

        elif edge.relation == "instantiates":
            # QML scenes can instantiate a registered QObject type whose
            # declaration lives in a C++ header rather than in Type.qml.
            # The regex/tree-sitter extractors emit a placeholder only for a
            # local matching C++ path; resolve it here after the complete
            # batch (or the stored incremental Qt index) has supplied class
            # symbols.  Unresolved framework/external types remain absent from
            # the graph rather than being invented.
            type_name = edge.metadata.get("type_name")
            if not type_name or edge.target != f"module:{type_name}":
                continue
            class_path = index.classes.get(type_name)
            if class_path:
                edge.target = f"symbol:{class_path}:{type_name}"


def build_file_layer(
    sources: list[SourceRecord],
    known_paths: set[str],
    *,
    existing_qt_index: QtSymbolIndex | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """HEADING + STRUCTURAL layer nodes/edges for exactly the given `sources`.

    Every edge produced here is stamped with metadata['source_file'] set to
    the owning source path, so CortexStore.delete_graph_for_sources can prune
    it later without touching any other file's rows (P0-3 delta writes).
    `known_paths` is only used to resolve cross-file references (e.g. Python
    imports to a local module) and may be a superset of `sources` — callers
    doing a partial (incremental) rebuild should pass the full current repo
    path set even though `sources` itself is just the changed/new files.

    `existing_qt_index` (P0-4) supplies Qt signal/slot declarations and class
    locations from files *outside* this batch -- the incremental ingest path
    (P0-3) reparses only the changed/new files, so a signal declared in an
    unchanged header (or an unchanged QML component) wouldn't otherwise be
    visible to the cross-file `emits`/`connects`/`handles` resolution pass
    run at the end of this function. Full ingest passes nothing here: a
    single batch containing every source file is already self-sufficient.
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

    # P0-4: resolve Qt emit/connect/handle placeholders across file
    # boundaries now that every file in this batch has produced its nodes.
    qt_index = build_qt_symbol_index(nodes)
    if existing_qt_index is not None:
        qt_index = existing_qt_index.merged_with(qt_index)
    _resolve_qt_edges(edges, qt_index)

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
    annotate_file_nodes(nodes, compute_hotspots(sources, commits))

    return nodes, edges
