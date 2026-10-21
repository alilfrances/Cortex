"""Word aliases for concatenated lowercase file names (`widgetwaitready.cpp`).

Qt/C++ repositories often name a file after its class with the case dropped, so the stem is one
token and a task word such as ``wait`` never matches the path.  Two derivations, both computed
from the graph alone so a full ingest and an incremental one agree:

* a class or qualifier declared in the file whose lower-cased label starts with the stem (or the
  other way round) contributes its camelCase parts: ``WidgetWaitReadyStep`` gives
  ``widget wait ready step`` for ``widgetwaitready``;
* otherwise the stem is segmented by dynamic programming over the words that appear as camelCase
  parts of the repository's symbol labels (parts of at least `MIN_PART` characters, whole stem
  covered, fewest parts).

The unsplit stem is never replaced: aliases are extra path tokens, stored on the file node as
``metadata["path_aliases"]``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath

from .models import GraphNode

MIN_PART = 3
MIN_STEM = 6
MAX_PART = 20
# A word seen in this many distinct symbol labels is vocabulary for the segmentation fallback.
MIN_VOCABULARY_LABELS = 2
_STEM_RE = re.compile(r"[a-z]+")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def camel_parts(label: str) -> tuple[str, ...]:
    """Lower-cased camelCase / snake_case parts of an identifier (the bundle tokenizer's split)."""
    normalized = _CAMEL_RE.sub(r"\1 \2", label.replace("_", " "))
    return tuple(part.lower() for part in _WORD_RE.findall(normalized) if part)


def _last_segment(label: str) -> str:
    return label.replace("::", ".").rsplit(".", 1)[-1].strip()


def splittable(token: str) -> bool:
    """A lower-case alphabetic run long enough to be several words."""
    return len(token) >= MIN_STEM and _STEM_RE.fullmatch(token) is not None


def parts_from_labels(token: str, labels: Iterable[str]) -> tuple[str, ...]:
    """camelCase parts of every label that starts with `token` (or that `token` starts with)."""
    if not splittable(token):
        return ()
    found: set[str] = set()
    for label in labels:
        lowered = _last_segment(label).lower()
        if len(lowered) < MIN_PART:
            continue
        if lowered.startswith(token) or token.startswith(lowered):
            found.update(camel_parts(_last_segment(label)))
    return tuple(sorted(found))


def segment_stem(stem: str, vocabulary: frozenset[str] | set[str]) -> tuple[str, ...] | None:
    """Fewest-parts segmentation of `stem` into vocabulary words of at least `MIN_PART` characters.

    The whole stem must be covered; ties prefer longer leading parts, then alphabetical order, so the
    result is deterministic.  None when there is no segmentation or it is the stem itself.
    """
    size = len(stem)
    best: list[tuple[str, ...] | None] = [None] * (size + 1)
    best[0] = ()
    for end in range(MIN_PART, size + 1):
        winner: tuple[str, ...] | None = None
        for start in range(max(0, end - MAX_PART), end - MIN_PART + 1):
            head = best[start]
            word = stem[start:end]
            if head is None or word not in vocabulary:
                continue
            candidate = head + (word,)
            if winner is None or _rank(candidate) < _rank(winner):
                winner = candidate
        best[end] = winner
    result = best[size]
    return result if result is not None and len(result) >= 2 else None


def _rank(parts: tuple[str, ...]) -> tuple[int, tuple[int, ...], tuple[str, ...]]:
    return len(parts), tuple(-len(part) for part in parts), parts


def _symbol_labels(nodes: Iterable[GraphNode]) -> tuple[dict[str, set[str]], dict[str, int]]:
    """Per file the class labels and `Cls::` qualifiers it declares, and the label count of every camel part."""
    declared: defaultdict[str, set[str]] = defaultdict(set)
    seen_in: defaultdict[str, set[str]] = defaultdict(set)
    for node in nodes:
        if node.granularity != "symbol":
            continue
        labels: list[str] = []
        if node.kind == "class":
            labels.append(node.label)
        qualifier = node.metadata.get("qualifier") if isinstance(node.metadata, dict) else None
        if isinstance(qualifier, str) and qualifier:
            labels.append(_last_segment(qualifier))
        for label in labels:
            declared[node.source_ref].add(label)
        for label in {node.label, *labels}:
            for part in camel_parts(_last_segment(label)):
                if len(part) >= MIN_PART:
                    seen_in[part].add(label)
    counts = {part: len(labels) for part, labels in seen_in.items()}
    return declared, counts


def compute_path_aliases(nodes: Sequence[GraphNode]) -> dict[str, list[str]]:
    """file node id -> sorted alias words, for the files whose stem is a concatenation of words."""
    declared, counts = _symbol_labels(nodes)
    vocabulary = frozenset(part for part, count in counts.items() if count >= MIN_VOCABULARY_LABELS)
    aliases: dict[str, list[str]] = {}
    for node in nodes:
        if node.kind != "file" or node.granularity == "symbol":
            continue
        stem = PurePosixPath(node.source_ref).stem
        if not splittable(stem):
            continue
        words = set(parts_from_labels(stem, declared.get(node.source_ref, ())))
        if not words:
            segmented = segment_stem(stem, vocabulary)
            words = set(segmented or ())
        words.discard(stem)
        if words:
            aliases[node.node_id] = sorted(words)
    return aliases


def apply_path_aliases(nodes: Sequence[GraphNode]) -> None:
    """Set (or clear) `metadata["path_aliases"]` on every file node, in place."""
    aliases = compute_path_aliases(nodes)
    for node in nodes:
        if node.kind != "file" or node.granularity == "symbol":
            continue
        words = aliases.get(node.node_id)
        if words:
            node.metadata["path_aliases"] = words
        else:
            node.metadata.pop("path_aliases", None)
