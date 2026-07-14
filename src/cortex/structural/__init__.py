from __future__ import annotations

from pathlib import PurePosixPath

from ..models import GraphEdge, GraphNode
from . import regex_backend

_SUPPORTED_EXTENSIONS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".swift",
    ".java",
    ".rb",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".hh",
    ".hxx",
    ".qml",
    ".cmake",
    ".qrc",
}


def supports_path(path: str) -> bool:
    return PurePosixPath(path).name.lower() == "cmakelists.txt" or PurePosixPath(path).suffix.lower() in _SUPPORTED_EXTENSIONS


def extract_structural_edges(
    path: str,
    content: str,
    known_paths: set[str],
    connect_names: list[str] | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    if not supports_path(path):
        return [], []

    if PurePosixPath(path).name.lower() == "cmakelists.txt" or PurePosixPath(path).suffix.lower() in {".cmake", ".qrc"}:
        return regex_backend.extract_regex_edges(path, content, known_paths, connect_names)

    try:
        from .treesitter_backend import extract_treesitter_edges

        return extract_treesitter_edges(path, content, known_paths, connect_names)
    except Exception:
        pass

    try:
        return regex_backend.extract_regex_edges(path, content, known_paths, connect_names)
    except Exception:
        return [], []
