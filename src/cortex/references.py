from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .ingest import _iter_candidate_files
from .deadline import check_deadline
from .store import CortexStore
from .tokenizer import count_text_tokens

_CODE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".rb",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".qml", ".go", ".rs",
}
_SCRIPT_SUFFIXES = {".sh"}
_DOC_SUFFIXES = {".md", ".txt", ".rst"}
_CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".qrc", ".cmake", ".pro", ".ui"}
_CONFIG_NAMES = {"cmakelists.txt"}
_MUTATING_METHODS = (
    "append",
    "insert",
    "push_back",
    "clear",
    "remove",
    "pop",
    "emplace_back",
    "erase",
    "update",
)


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
            access = "definition"
            if edge.relation in {"writes", "binds", "aliases"} or (node_id == edge.target and edge.relation in {"references", "exports"}):
                access = "write"
            elif edge.relation in {"reads", "calls"}:
                access = "read"
            hits.append({
                "bucket": _bucket(Path(node.source_ref)),
                "text": f"{node.source_ref}{line_part}",
                "origin": "graph",
                "access": access,
            })
    return hits, covered


def _line_access(symbol: str, line: str) -> str:
    symbol_pattern = r"\b" + re.escape(symbol) + r"\b"
    if re.search(symbol_pattern + r"\s*(?:<<=|>>=|\+=|-=|\*=|/=|\|=|&=|\^=)", line):
        return "write"
    if re.search(r"(?:\+\+|--)\s*" + symbol_pattern, line) or re.search(symbol_pattern + r"\s*(?:\+\+|--)", line):
        return "write"
    if re.search(symbol_pattern + r"\s*(?:\.\s*(?:" + "|".join(_MUTATING_METHODS) + r")\s*\(|\[[^\]]*\]\s*=(?!=|>))", line):
        return "write"
    if re.search(r"\bdel\s+" + symbol_pattern, line):
        return "write"
    if re.search(symbol_pattern + r"\s*=(?!=|>)", line):
        return "write"
    return "read"


class _GrepCorpus:
    """Lazily cached ``(rel_path, bucket, lines)`` for every candidate file.

    The walk-and-read happens on FIRST USE, not at construction. That
    matters: `analyze_dead_code` creates one of these up front but only
    reaches a grep scan for candidates that survive every earlier filter,
    and on many repos that is zero candidates. Building eagerly would make
    every `cortex_overview` call pay a full working-tree read it never paid
    before -- a new cost on the exact hot path this work exists to speed up.

    Once built, the entries are reused for every subsequent lookup, so
    callers that run `find_references` many times in a row (one dead-code
    candidate after another) walk and read the tree exactly once instead of
    once per candidate.
    """

    __slots__ = ("_repo_root", "_entries")

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root
        self._entries: list[tuple[str, str, list[str]]] | None = None

    @property
    def entries(self) -> list[tuple[str, str, list[str]]]:
        if self._entries is None:
            self._entries = _load_grep_entries(self._repo_root)
        return self._entries


def _load_grep_entries(repo_root: Path) -> list[tuple[str, str, list[str]]]:
    """Read every candidate file once, splitting each into lines.

    Skip rules (UnicodeDecodeError/OSError), the candidate file set, the
    bucket and the line numbering are all unchanged from the original
    per-call scan -- only the number of times the work happens changes.
    """
    entries: list[tuple[str, str, list[str]]] = []
    for path in _iter_candidate_files(repo_root):
        # Reading the whole working tree is the other multi-second step in a
        # dead-code run; check between files so a budget can interrupt it.
        check_deadline()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        entries.append((rel_path, _bucket(path), text.splitlines()))
    return entries


def build_grep_corpus(repo_root: Path) -> _GrepCorpus:
    """A reusable, lazily-populated grep corpus for one repo snapshot."""
    return _GrepCorpus(repo_root)


def _grep_hits(
    repo_root: Path,
    symbol: str,
    covered: set[tuple[str, int | None]],
    corpus: _GrepCorpus | None = None,
) -> list[dict[str, Any]]:
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    hits: list[dict[str, Any]] = []
    entries = corpus.entries if corpus is not None else build_grep_corpus(repo_root).entries
    for rel_path, bucket, lines in entries:
        for lineno, line in enumerate(lines, start=1):
            if not pattern.search(line):
                continue
            if (rel_path, lineno) in covered:
                continue
            hits.append({
                "bucket": bucket,
                "text": f"{rel_path}:{lineno}",
                "origin": "grep",
                "access": _line_access(symbol, line),
            })
    return hits


def _items_with_prefix(eligible: list[dict[str, Any]], count: int) -> dict[str, list[dict[str, str]]]:
    items: dict[str, list[dict[str, str]]] = {"code": [], "script": [], "doc": [], "config": [], "other": []}
    for hit in eligible[:count]:
        items[hit["bucket"]].append({"text": hit["text"], "origin": hit["origin"], "access": hit["access"]})
    return items


def find_references(
    store: CortexStore,
    repo_root: Path,
    symbol: str,
    budget: int = 2000,
    mode: str = "all",
    *,
    corpus: _GrepCorpus | None = None,
) -> dict[str, Any]:
    graph_hits, covered = _graph_hits(store, repo_root, symbol)
    grep_hits = _grep_hits(repo_root, symbol, covered, corpus=corpus)

    # Equivalent to the old "grow items by one hit, re-tokenize the whole
    # thing, stop at the first hit that doesn't fit" loop, but without
    # re-serializing the whole (growing) items dict on every single hit --
    # that made this O(hits^2) in token-counting work. `items` only ever
    # grows in this fixed hit order, so token count is monotone
    # non-decreasing in the prefix length, and the first hit that overflows
    # under the old sequential scan is exactly the boundary a binary search
    # over prefix length finds.
    eligible = [
        hit for hit in (*graph_hits, *grep_hits)
        if not (mode == "writes" and hit["access"] not in {"definition", "write"})
    ]

    lo, hi, best = 0, len(eligible), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0 or count_text_tokens(str(_items_with_prefix(eligible, mid))) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    items = _items_with_prefix(eligible, best)
    truncated = best < len(eligible)
    return {"items": items, "truncated": truncated, "returned_count": best}
