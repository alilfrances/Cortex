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
}


def supports_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _SUPPORTED_EXTENSIONS


def extract_structural_edges(
    path: str,
    content: str,
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    if not supports_path(path):
        return [], []

    try:
        from .treesitter_backend import extract_treesitter_edges

        return extract_treesitter_edges(path, content, known_paths)
    except Exception:
        pass

    try:
        return regex_backend.extract_regex_edges(path, content, known_paths)
    except Exception:
        return [], []
