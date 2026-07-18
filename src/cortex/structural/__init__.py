from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterator

from ..models import GraphEdge, GraphNode
from . import regex_backend

_SUPPORTED_EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".swift", ".java", ".rb", ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".qml", ".cmake", ".qrc", ".qmltypes", ".qmldir", ".qmlproject",
}


@dataclass(slots=True)
class StructuralResult:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    language: str = ""
    backend: str = "regex"
    parser_version: str = ""
    runtime_version: str = ""
    diagnostics: list[str] = field(default_factory=list)
    degraded_reason: str | None = None

    # Source compatibility: existing callers unpacked ``(nodes, edges)``.
    def __iter__(self) -> Iterator[Any]:
        yield self.nodes
        yield self.edges

    def __len__(self) -> int:
        return 2

    def __getitem__(self, index: int) -> Any:
        return (self.nodes, self.edges)[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple) and len(other) == 2:
            return self.nodes == other[0] and self.edges == other[1]
        if isinstance(other, StructuralResult):
            return (self.nodes, self.edges, self.language, self.backend) == (other.nodes, other.edges, other.language, other.backend)
        return NotImplemented


def supports_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return pure.name.lower() in {"cmakelists.txt", "qmldir"} or pure.suffix.lower() in _SUPPORTED_EXTENSIONS


def _language_for_path(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {".qml": "qml", ".qrc": "qrc", ".qmldir": "qmldir", ".qmltypes": "qmltypes"}.get(suffix, suffix.removeprefix("."))


def _parser_version() -> str:
    try:
        import tree_sitter
        return str(getattr(tree_sitter, "__version__", "0.26.0"))
    except Exception:
        return ""


def _runtime_version() -> str:
    try:
        from ..runtime import RUNTIME_VERSION
        return str(RUNTIME_VERSION)
    except Exception:
        return ""


def _annotate(nodes: list[GraphNode], backend: str, parser_version: str, runtime_version: str, reason: str | None = None) -> None:
    for node in nodes:
        node.metadata.setdefault("parser_backend", backend)
        if parser_version:
            node.metadata.setdefault("parser_version", parser_version)
        if runtime_version:
            node.metadata.setdefault("runtime_version", runtime_version)
        if reason:
            node.metadata.setdefault("degraded_reason", reason)


def _result(nodes: list[GraphNode], edges: list[GraphEdge], *, language: str, backend: str, diagnostics: list[str] | None = None, reason: str | None = None) -> StructuralResult:
    parser_version = _parser_version() if backend == "treesitter" else ""
    runtime_version = _runtime_version() if backend == "treesitter" else ""
    if backend == "regex" and reason:
        # The fallback's LOW confidence is an established public contract.
        for edge in edges:
            edge.confidence = "LOW"
    _annotate(nodes, backend, parser_version, runtime_version, reason)
    return StructuralResult(nodes, edges, language, backend, parser_version, runtime_version, diagnostics or [], reason)


def extract_structural_edges(path: str, content: str, known_paths: set[str], connect_names: list[str] | None = None) -> StructuralResult:
    if not supports_path(path):
        return StructuralResult(language=_language_for_path(path))
    pure = PurePosixPath(path)
    language = _language_for_path(path)
    if pure.name.lower() == "cmakelists.txt" or pure.suffix.lower() in {".cmake", ".qrc", ".qmldir", ".qmltypes", ".qmlproject"}:
        try:
            from .qml_modules import extract_module_edges
            nodes, edges = extract_module_edges(path, content, known_paths)
            if pure.name.lower() == "cmakelists.txt" or pure.suffix.lower() == ".cmake":
                generic_nodes, generic_edges = regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
                node_ids = {node.node_id for node in nodes}
                edge_ids = {edge.edge_id for edge in edges}
                nodes.extend(node for node in generic_nodes if node.node_id not in node_ids)
                edges.extend(edge for edge in generic_edges if edge.edge_id not in edge_ids)
            elif not nodes and not edges and pure.suffix.lower() != ".qmlproject":
                nodes, edges = regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
            return _result(nodes, edges, language=language, backend="regex")
        except Exception as exc:
            try:
                nodes, edges = regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
            except Exception:
                nodes, edges = [], []
            return _result(nodes, edges, language=language, backend="regex", reason=f"module metadata parser failed: {exc}", diagnostics=[str(exc)])

    if os.environ.get("CORTEX_FORCE_REGEX") == "1":
        try:
            nodes, edges = regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
        except Exception as exc:
            return _result([], [], language=language, backend="regex", reason=f"regex extraction failed: {exc}", diagnostics=[str(exc)])
        return _result(nodes, edges, language=language, backend="regex", reason="forced-fallback")

    try:
        if pure.suffix.lower() == ".qml":
            from .qml_backend import extract_qml_edges
            nodes, edges, diagnostics = extract_qml_edges(path, content, known_paths, connect_names)
        else:
            from .treesitter_backend import extract_treesitter_edges
            raw = extract_treesitter_edges(path, content, known_paths, connect_names)
            if isinstance(raw, StructuralResult):
                return raw
            nodes, edges = raw
            diagnostics = []
        return _result(nodes, edges, language=language, backend="treesitter", diagnostics=diagnostics)
    except Exception as exc:
        # Runtime/parser failures are the only fallback trigger. QML's
        # recoverable ERROR nodes are returned by qml_backend with diagnostics.
        try:
            nodes, edges = regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
        except Exception as regex_exc:
            return _result([], [], language=language, backend="regex", reason=f"treesitter failed ({exc}); regex failed ({regex_exc})", diagnostics=[str(exc), str(regex_exc)])
        return _result(nodes, edges, language=language, backend="regex", reason=f"treesitter unavailable or fatal: {exc}", diagnostics=[str(exc)])
