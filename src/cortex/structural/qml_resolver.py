"""Conservative, module-aware QML endpoint resolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable

from ..models import GraphEdge, GraphNode


@dataclass
class QmlSymbolIndex:
    """Global QML lookup kept separate from the Qt signal index."""
    components: dict[tuple[str, str, str], list[str]] = field(default_factory=dict)
    members: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "QmlSymbolIndex":
        return cls()

    def add(self, node: GraphNode) -> None:
        qml_kind = node.metadata.get("qml_kind") if isinstance(node.metadata, dict) else None
        owner = str(node.metadata.get("qml_owner", ""))
        if qml_kind in {"component", "inline_component", "qmltypes_component", "export", "registered_type"}:
            uri = str(node.metadata.get("uri", ""))
            version = str(node.metadata.get("version", ""))
            key = (uri, version, str(node.metadata.get("qml_name", node.label)))
            self.components.setdefault(key, []).append(node.node_id)
        if qml_kind in {"property", "signal", "method", "parameter", "enum", "enum_member", "binding"}:
            self.members.setdefault(owner, {})[node.label] = node.node_id


def build_qml_symbol_index(nodes: Iterable[GraphNode]) -> QmlSymbolIndex:
    index = QmlSymbolIndex.empty()
    for node in nodes:
        if node.source_ref and node.metadata.get("qml_kind"):
            index.add(node)
    for values in index.components.values():
        values.sort()
    return index


def _local_component_candidates(name: str, source_path: str, known_paths: set[str]) -> list[str]:
    basename = f"{name.rsplit('.', 1)[-1]}.qml"
    directory = PurePosixPath(source_path).parent
    direct = (directory / basename).as_posix()
    if direct in known_paths:
        return [direct]
    return sorted(path for path in known_paths if PurePosixPath(path).name == basename)


def _resolve_component(name: str, source_path: str, known_paths: set[str], index: QmlSymbolIndex) -> str | None:
    local = _local_component_candidates(name, source_path, known_paths)
    if len(local) == 1:
        return f"file:{local[0]}"
    type_name = name.rsplit(".", 1)[-1]
    candidates = sorted({node_id for key, ids in index.components.items() if key[2] == type_name for node_id in ids})
    return candidates[0] if len(candidates) == 1 else None


def resolve_qml_edges(nodes: list[GraphNode], edges: list[GraphEdge], known_paths: set[str] | None = None, index: QmlSymbolIndex | None = None) -> QmlSymbolIndex:
    """Resolve only deterministic local/module references; mutate edges in place."""
    known_paths = known_paths or {node.source_ref for node in nodes if node.source_ref}
    index = index or build_qml_symbol_index(nodes)
    node_by_id = {node.node_id: node for node in nodes}
    export_files: dict[str, list[str]] = {}
    exports_by_module: dict[tuple[str, str, str], list[str]] = {}
    imports_by_file: dict[str, list[tuple[str, str, str]]] = {}
    export_nodes = {
        node.node_id: node for node in nodes
        if node.metadata.get("qml_kind") == "export"
    }
    for edge in edges:
        if edge.relation == "exports" and edge.source.startswith("export:") and edge.target.startswith("file:"):
            export_files.setdefault(edge.source, []).append(edge.target)
            export_node = export_nodes.get(edge.source)
            target_path = edge.target.removeprefix("file:")
            if export_node is not None and target_path in known_paths:
                key = (
                    str(export_node.metadata.get("uri", "")),
                    str(export_node.metadata.get("version", "")),
                    export_node.label,
                )
                exports_by_module.setdefault(key, []).append(edge.target)
        elif edge.relation == "imports" and edge.source.startswith("file:") and edge.metadata.get("uri"):
            imports_by_file.setdefault(edge.source.removeprefix("file:"), []).append(
                (
                    str(edge.metadata.get("uri", "")),
                    str(edge.metadata.get("version", "")),
                    str(edge.metadata.get("alias", "")),
                )
            )
    for edge in edges:
        source_file = str(edge.metadata.get("source_file", ""))
        if edge.relation == "instantiates" and edge.target.startswith("file:") and edge.target.removeprefix("file:") not in known_paths:
            name = str(edge.metadata.get("type_name", PurePosixPath(edge.target.removeprefix("file:")).stem))
            edge.target = f"module:{name}"
            edge.metadata["unverified"] = True
        if edge.relation == "instantiates" and edge.target.startswith("module:"):
            name = str(edge.metadata.get("type_name", edge.target.removeprefix("module:")))
            short_name = name.rsplit(".", 1)[-1]
            qualifier = name.rsplit(".", 1)[0] if "." in name else ""
            imported_targets: set[str] = set()
            for uri, version, alias in imports_by_file.get(source_file, []):
                if qualifier and qualifier not in {alias, uri, uri.rsplit(".", 1)[-1]}:
                    continue
                if version:
                    imported_targets.update(exports_by_module.get((uri, version, short_name), []))
                else:
                    for (export_uri, _export_version, export_name), targets in exports_by_module.items():
                        if export_uri == uri and export_name == short_name:
                            imported_targets.update(targets)
            target = next(iter(imported_targets)) if len(imported_targets) == 1 else None
            if target is None and not imported_targets:
                target = _resolve_component(name, source_file, known_paths, index)
            if target:
                mapped = export_files.get(target, [])
                edge.target = mapped[0] if len(mapped) == 1 else target
                edge.metadata.pop("unverified", None)
            else:
                edge.metadata["unverified"] = True
        elif edge.relation == "handles" and (edge.target.startswith("module:") or edge.target not in node_by_id):
            name = str(edge.metadata.get("signal_name", edge.target.removeprefix("module:")))
            if not edge.target.startswith("module:"):
                edge.target = f"module:{name}"
            component_path = str(edge.metadata.get("component_path", ""))
            source_node = node_by_id.get(edge.source)
            owner = str(source_node.metadata.get("qml_owner", "")) if source_node else ""
            base_name = name[:-7] if name.endswith("Changed") and len(name) > 7 else ""
            candidates = [
                index.members.get(owner, {}).get(name),
                index.members.get(owner, {}).get(base_name) if base_name else None,
                index.members.get(owner, {}).get(name + "Changed"),
            ]
            if component_path:
                candidates.extend(
                    node.node_id for node in nodes
                    if node.source_ref == component_path and node.metadata.get("qml_kind") == "signal"
                    and node.label in {name, base_name, name + "Changed"}
                )
            target = next((candidate for candidate in candidates if candidate), None)
            if target:
                edge.target = target
                edge.metadata.pop("unverified", None)
            else:
                edge.metadata["unverified"] = True
        elif edge.relation == "aliases" and edge.target.startswith("external:"):
            edge.metadata["unverified"] = True
    return index


# Friendly aliases for integrations that use verb-oriented names.
resolve = resolve_qml_edges
