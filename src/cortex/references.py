from __future__ import annotations

import heapq
import re
import string
from array import array
from bisect import bisect_right
from itertools import accumulate, islice
from pathlib import Path
from typing import Any, Collection, Sequence

from .ingest import MAX_SOURCE_BYTES, _iter_candidate_files
from .models import GraphEdge, GraphNode
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
    return _graph_hits_from_edges(edges, nodes, symbol)


def _graph_hits_from_edges(
    edges: Sequence[GraphEdge],
    nodes: dict[str, GraphNode],
    symbol: str,
) -> tuple[list[dict[str, Any]], set[tuple[str, int | None]]]:
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


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_LIKE_WILDCARDS = re.compile(r"[_%]")
_EDGE_LIMIT = 200


def _like_regex(symbol: str) -> re.Pattern[str]:
    """Regex equal to SQLite `LIKE '%' || symbol || '%'` with no ESCAPE clause."""
    parts: list[str] = []
    for char in symbol:
        if char == "_":
            parts.append(".")
        elif char == "%":
            if not parts or parts[-1] != ".*":
                parts.append(".*")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts), re.IGNORECASE | re.ASCII | re.DOTALL)


class GraphReferenceIndex:
    """In-memory equivalent of `_graph_hits` for repeated lookups over one fixed graph.

    Reproduces `query_edges(endpoint_substr=symbol, direction="in", limit=200)`: the
    first 200 edges by edge_id whose target id or target-node label matches the LIKE
    pattern. Candidate strings are narrowed with an ASCII-case-folded trigram index
    (every literal run of the pattern must appear contiguously in a match) and then
    verified with the exact LIKE-equivalent regex.
    """

    def __init__(self, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge]) -> None:
        self._nodes = {node.node_id: node for node in nodes}
        self._edges = sorted(edges, key=lambda edge: edge.edge_id)
        positions_by_target: dict[str, array] = {}
        for position, edge in enumerate(self._edges):
            positions_by_target.setdefault(edge.target, array("I")).append(position)
        self._positions = positions_by_target
        targets_by_text: dict[str, list[str]] = {}
        for target in positions_by_target:
            targets_by_text.setdefault(target, []).append(target)
            node = self._nodes.get(target)
            if node is not None and node.label != target:
                targets_by_text.setdefault(node.label, []).append(target)
        self._texts = list(targets_by_text)
        self._targets = [targets_by_text[text] for text in self._texts]
        self._trigrams: dict[str, array] | None = None

    def _trigram_index(self) -> dict[str, array]:
        if self._trigrams is None:
            index: dict[str, array] = {}
            for position, text in enumerate(self._texts):
                folded = text.translate(_ASCII_LOWER)
                for gram in {folded[i:i + 3] for i in range(len(folded) - 2)}:
                    index.setdefault(gram, array("I")).append(position)
            self._trigrams = index
        return self._trigrams

    def _candidate_texts(self, symbol: str) -> Sequence[int] | range:
        grams = [
            segment[i:i + 3]
            for segment in _LIKE_WILDCARDS.split(symbol.translate(_ASCII_LOWER))
            for i in range(len(segment) - 2)
        ]
        if not grams:
            return range(len(self._texts))
        index = self._trigram_index()
        best: array | None = None
        for gram in grams:
            postings = index.get(gram)
            if postings is None:
                return ()
            if best is None or len(postings) < len(best):
                best = postings
        return best if best is not None else ()

    def _matching_edges(self, symbol: str) -> list[GraphEdge]:
        if not symbol:
            return self._edges[:_EDGE_LIMIT]
        pattern = _like_regex(symbol)
        matched_targets: set[str] = set()
        for position in self._candidate_texts(symbol):
            if pattern.search(self._texts[position]) is not None:
                matched_targets.update(self._targets[position])
        merged = heapq.merge(*(self._positions[target] for target in matched_targets))
        return [self._edges[position] for position in islice(merged, _EDGE_LIMIT)]

    def hits(self, symbol: str) -> tuple[list[dict[str, Any]], set[tuple[str, int | None]]]:
        return _graph_hits_from_edges(self._matching_edges(symbol), self._nodes, symbol)


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


_WORD_RUN = re.compile(r"\w+")


def _has_line_break(symbol: str) -> bool:
    return symbol.splitlines(keepends=True) != [symbol]


class _CorpusFile:
    """One candidate file. `text` is None for a file over the index size limit:
    its content is re-read on demand and never cached, so the corpus holds at
    most MAX_SOURCE_BYTES per file, the same bound the index applies."""

    __slots__ = ("path", "rel_path", "_text", "_tokens", "_lines", "_starts")

    def __init__(self, path: Path, rel_path: str, text: str | None) -> None:
        self.path = path
        self.rel_path = rel_path
        self._text = text
        self._tokens: frozenset[str] | None = None
        self._lines: list[str] | None = None
        self._starts: list[int] | None = None

    @property
    def retained(self) -> bool:
        return self._text is not None

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        try:
            return self.path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return ""

    def tokens(self, wanted: frozenset[str]) -> frozenset[str]:
        if self._tokens is None:
            self._tokens = wanted.intersection(_WORD_RUN.findall(self.text))
        return self._tokens

    def all_tokens(self) -> frozenset[str]:
        if self._tokens is None:
            self._tokens = frozenset(_WORD_RUN.findall(self.text))
        return self._tokens

    def _split(self) -> None:
        kept = self.text.splitlines(keepends=True)
        self._lines = [line.splitlines()[0] for line in kept]
        self._starts = [0, *accumulate(len(line) for line in kept)]

    @property
    def lines(self) -> list[str]:
        if not self.retained:
            return self.text.splitlines()
        if self._lines is None:
            self._split()
        return self._lines  # type: ignore[return-value]

    def line_starts(self) -> list[int]:
        if self._starts is None:
            self._split()
        return self._starts  # type: ignore[return-value]


class ReferenceCorpus:
    """Repo text read once, so repeated grep passes skip the file listing and disk reads.

    `symbols` optionally lists the identifiers that will be queried; per-file token
    sets then keep only those, and any other identifier falls back to a substring test.
    """

    def __init__(self, repo_root: Path, symbols: Collection[str] | None = None) -> None:
        self.repo_root = repo_root
        self._wanted = None if symbols is None else frozenset(symbols)
        self._by_symbol: dict[str, list[_CorpusFile]] | None = None
        self._files: list[_CorpusFile] = []
        for path in _iter_candidate_files(repo_root):
            try:
                oversized = path.stat().st_size > MAX_SOURCE_BYTES
                text = None if oversized else path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            self._files.append(_CorpusFile(path, path.relative_to(repo_root).as_posix(), text))

    def files_for(self, symbol: str) -> list[_CorpusFile]:
        """Files that can contain a `\\bsymbol\\b` match, in listing order.

        For a pure `\\w+` symbol both \\b anchors sit between a word and a non-word
        character, so a match is exactly a maximal word run equal to the symbol; a
        file qualifies iff the symbol is one of its word-run tokens (line breaks are
        non-word, so whole-file tokens equal the union of per-line tokens). Any other
        symbol must at least occur as a substring of a line, hence of the text.
        """
        if _WORD_RUN.fullmatch(symbol):
            if self._wanted is None:
                return [
                    item for item in self._files
                    if (symbol in item.all_tokens() if item.retained else symbol in item.text)
                ]
            if symbol in self._wanted:
                return self._symbol_files().get(symbol, [])
        return [item for item in self._files if symbol in item.text]

    def _symbol_files(self) -> dict[str, list[_CorpusFile]]:
        if self._by_symbol is None:
            index: dict[str, list[_CorpusFile]] = {}
            for item in self._files:
                for token in item.tokens(self._wanted):
                    index.setdefault(token, []).append(item)
            self._by_symbol = index
        return self._by_symbol


def _scan_lines(
    hits: list[dict[str, Any]],
    pattern: re.Pattern[str],
    symbol: str,
    covered: set[tuple[str, int | None]],
    path: Path,
    rel_path: str,
    lines: list[str],
) -> None:
    for lineno, line in enumerate(lines, start=1):
        if not pattern.search(line):
            continue
        if (rel_path, lineno) in covered:
            continue
        hits.append({
            "bucket": _bucket(path),
            "text": f"{rel_path}:{lineno}",
            "origin": "grep",
            "access": _line_access(symbol, line),
        })


def _scan_text(
    hits: list[dict[str, Any]],
    pattern: re.Pattern[str],
    symbol: str,
    covered: set[tuple[str, int | None]],
    item: _CorpusFile,
) -> None:
    """One regex pass over the whole file; same hits as `_scan_lines` over its lines.

    Valid only for a non-empty symbol with no line-break character: a match then
    cannot span lines, and every line separator is a non-word character, so `\\b`
    behaves at line edges as it does at the edges of a standalone line.
    """
    last_index = -1
    for match in pattern.finditer(item.text):
        index = bisect_right(item.line_starts(), match.start()) - 1
        if index == last_index:
            continue
        last_index = index
        lineno = index + 1
        if (item.rel_path, lineno) in covered:
            continue
        hits.append({
            "bucket": _bucket(item.path),
            "text": f"{item.rel_path}:{lineno}",
            "origin": "grep",
            "access": _line_access(symbol, item.lines[index]),
        })


def _grep_hits(
    repo_root: Path,
    symbol: str,
    covered: set[tuple[str, int | None]],
    corpus: ReferenceCorpus | None = None,
) -> list[dict[str, Any]]:
    pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
    hits: list[dict[str, Any]] = []
    if corpus is not None:
        whole_text = bool(symbol) and not _has_line_break(symbol)
        for item in corpus.files_for(symbol):
            if whole_text and item.retained:
                _scan_text(hits, pattern, symbol, covered, item)
            else:
                _scan_lines(hits, pattern, symbol, covered, item.path, item.rel_path, item.lines)
        return hits
    for path in _iter_candidate_files(repo_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        _scan_lines(hits, pattern, symbol, covered, path, rel_path, text.splitlines())
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
    corpus: ReferenceCorpus | None = None,
    graph_index: GraphReferenceIndex | None = None,
) -> dict[str, Any]:
    if graph_index is not None:
        graph_hits, covered = graph_index.hits(symbol)
    else:
        graph_hits, covered = _graph_hits(store, repo_root, symbol)
    grep_hits = _grep_hits(repo_root, symbol, covered, corpus)

    eligible = [
        hit for hit in (*graph_hits, *grep_hits)
        if not (mode == "writes" and hit["access"] not in {"definition", "write"})
    ]

    # Token count only grows with the prefix length, so the first hit that
    # overflows under a one-at-a-time scan is the boundary a binary search finds.
    lo, hi, best = 0, len(eligible), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0 or count_text_tokens(str(_items_with_prefix(eligible, mid))) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return {
        "items": _items_with_prefix(eligible, best),
        "truncated": best < len(eligible),
        "returned_count": best,
    }
