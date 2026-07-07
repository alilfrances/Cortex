from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ingest import _iter_candidate_files
from .store import CortexStore
from .tokenizer import count_text_tokens

_CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".rb",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".qml", ".go", ".rs",
}
_SCRIPT_SUFFIXES = {".sh"}
_DOC_SUFFIXES = {".md", ".txt", ".rst"}
_CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".qrc", ".cmake"}
_CONFIG_NAMES = {"cmakelists.txt"}


def _bucket(path: Path) -> str:
    suffix = path.suffix.lower()
    if path.name.lower() in _CONFIG_NAMES or suffix in _CONFIG_SUFFIXES:
        return "config"
    if suffix in _CODE_SUFFIXES:
        return "code"
    if suffix in _SCRIPT_SUFFIXES:
        return "script"
    if suffix in _DOC_SUFFIXES:
        return "doc"
    return "other"


def _graph_hits(store: CortexStore, repo_root: Path, symbol: str) -> tuple[list[dict[str, Any]], set[tuple[str, int | None]]]:
    edges = store.query_edges(repo_root, endpoint_substr=symbol, direction="in", limit=200)
    node_ids = sorted({edge.source for edge in edges} | {edge.target for edge in edges})
    nodes = store.get_nodes(repo_root, node_ids)

    hits: list[dict[str, Any]] = []
    covered: set[tuple[str, int | None]] = set()
    seen_edge_locations: set[tuple[str, int | None]] = set()
    for edge in edges:
        for node_id in (edge.source, edge.target):
            node = nodes.get(node_id)
            if node is None or symbol.lower() not in node.label.lower():
                continue
            key = (node.source_ref, node.span_start)
            covered.add(key)
            if key in seen_edge_locations:
                continue
            seen_edge_locations.add(key)
            line_part = f":{node.span_start}" if node.span_start is not None else ""
            hits.append({"bucket": _bucket(Path(node.source_ref)), "text": f"{node.source_ref}{line_part}"})
    return hits, covered


def _grep_hits(repo_root: Path, symbol: str, covered: set[tuple[str, int | None]]) -> list[dict[str, Any]]:
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    hits: list[dict[str, Any]] = []
    for path in _iter_candidate_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            if (rel_path, lineno) in covered:
                continue
            hits.append({"bucket": _bucket(path), "text": f"{rel_path}:{lineno}"})
    return hits


def find_references(store: CortexStore, repo_root: Path, symbol: str, budget: int = 2000) -> dict[str, Any]:
    graph_hits, covered = _graph_hits(store, repo_root, symbol)
    grep_hits = _grep_hits(repo_root, symbol, covered)

    items: dict[str, list[str]] = {"code": [], "script": [], "doc": [], "config": [], "other": []}
    truncated = False
    returned_count = 0
    for hit in [*graph_hits, *grep_hits]:
        candidate = {**items, hit["bucket"]: [*items[hit["bucket"]], hit["text"]]}
        if count_text_tokens(str(candidate)) > budget:
            truncated = True
            break
        items = candidate
        returned_count += 1

    return {"items": items, "truncated": truncated, "returned_count": returned_count}
