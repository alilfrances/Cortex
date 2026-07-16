from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from .models import CommitRecord, GraphNode, SourceRecord
from .structural.regex_backend import _mask_comments_and_strings

# The score intentionally stays a small, explainable analytic: a file is a
# hotspot when it is both changed often and dense with control-flow branches.
# Complexity is reported as branch points per KLOC (rounded to an integer), so
# a short but branch-heavy file is comparable to a larger file.
_HOTSPOT_LIMIT = 10

_PYTHON_SUFFIXES = {".py"}
_CPP_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}
_QML_JS_SUFFIXES = {".qml", ".js", ".jsx", ".ts", ".tsx"}

_WORD_RULES: dict[str, tuple[str, ...]] = {
    "python": ("if", "elif", "for", "while", "except", "and", "or"),
    "cpp": ("if", "for", "while", "switch", "case", "catch"),
    "qml": ("if", "for", "while", "switch", "case"),
    # A conservative fallback for other indexed code. It is deliberately not
    # the Python table: language-specific rules above are selected whenever
    # Cortex knows the suffix.
    "generic": ("if", "for", "while", "switch", "case", "catch"),
}

_HANDLER_RE = re.compile(r"^\s*on[A-Z][A-Za-z0-9_]*\s*:")
_BINDING_RE = re.compile(
    r"^\s*(?!on[A-Z][A-Za-z0-9_]*\s*:)(?P<name>"
    r"property\b[^:]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r")\s*:"
)
_BINDING_EXCLUSIONS = {"case", "default", "else"}


def _commit_files(commit: CommitRecord | Mapping[str, Any]) -> Iterable[str]:
    if isinstance(commit, Mapping):
        files = commit.get("files", ())
    else:
        files = getattr(commit, "files", ())
    return files or ()


def _normalise_path(path: Any) -> str:
    value = str(path).replace("\\", "/")
    if value.startswith("./"):
        value = value[2:]
    return value


def compute_churn(commits: Iterable[CommitRecord | Mapping[str, Any]]) -> dict[str, int]:
    """Return the number of distinct commits touching each repository path.

    ``git log --name-only`` normally emits each path once per commit, but
    de-duplicating each commit makes this function deterministic for callers
    that construct ``CommitRecord`` values by hand. The result intentionally
    uses integer touch counts; a caller that wants recency weighting can apply
    it as a separate ranking policy without changing the persisted statistic.
    """
    churn: dict[str, int] = {}
    for commit in commits:
        paths = sorted({_normalise_path(path) for path in _commit_files(commit) if str(path).strip()})
        for path in paths:
            churn[path] = churn.get(path, 0) + 1
    return churn


def _source_parts(source: SourceRecord | str | Mapping[str, Any] | Any, path: str | None) -> tuple[str, str]:
    if isinstance(source, str):
        return path or "", source
    if isinstance(source, Mapping):
        return str(path or source.get("path", "")), str(source.get("content", ""))
    content = getattr(source, "content", None)
    if content is not None:
        return str(path or getattr(source, "path", "")), str(content)
    return str(path or ""), str(source)


def _language_for_path(path: str) -> str:
    language_name = path.strip().lower()
    if language_name in {"python", "py"}:
        return "python"
    if language_name in {"cpp", "c++", "c", "qml", "javascript", "js"}:
        return "cpp" if language_name in {"cpp", "c++", "c"} else "qml"
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in _PYTHON_SUFFIXES:
        return "python"
    if suffix in _CPP_SUFFIXES:
        return "cpp"
    if suffix in _QML_JS_SUFFIXES:
        return "qml"
    return "generic"


def _word_count(masked: str, words: tuple[str, ...]) -> int:
    return sum(len(re.findall(rf"\b{re.escape(word)}\b", masked)) for word in words)


def _operator_count(masked: str, language: str) -> int:
    if language not in {"cpp", "qml"}:
        return 0
    # A ternary is represented by its question mark. `?=` is not a C++/QML
    # branch and is excluded defensively for JavaScript-like input.
    return masked.count("&&") + masked.count("||") + len(re.findall(r"\?(?!=)", masked))


def _qml_binding_count(content: str, masked: str) -> int:
    """Count QML handler and property-binding expressions.

    Handlers are counted independently from ordinary bindings. Matching is
    done against the masked line so a comment containing ``onFoo:`` cannot
    manufacture complexity, while the original line is consulted for a
    string-literal-only binding such as ``text: \"ready\"`` (the expression
    is intentionally masked before branch keywords are counted).
    """
    raw_lines = content.splitlines()
    code_lines = masked.splitlines()
    total = 0
    for raw_line, code_line in zip(raw_lines, code_lines):
        if _HANDLER_RE.match(code_line):
            total += 1
            continue
        match = _BINDING_RE.match(code_line)
        if match:
            name = match.group("name").strip().split()[0].lower()
            if name in _BINDING_EXCLUSIONS:
                continue
            colon = code_line.find(":", match.start("name"))
            code_after = code_line[colon + 1 :].strip() if colon >= 0 else ""
            # If the expression is only a quoted literal, masking removes it;
            # retain the point because it is still a real binding. Do not count
            # a binding whose right side is only a line/block comment.
            raw_colon = raw_line.find(":")
            raw_after = raw_line[raw_colon + 1 :].strip() if raw_colon >= 0 else ""
            if code_after or (raw_after and not raw_after.startswith(("//", "/*"))):
                total += 1
    return total


def estimate_complexity(source: SourceRecord | str | Mapping[str, Any] | Any, path: str | None = None) -> int:
    """Estimate branch complexity for one source, normalized to branches/KLOC.

    This is intentionally parser-free and deterministic. Comments, quoted
    strings, triple-quoted strings, JavaScript templates, and C++ raw strings
    are masked by the same scanner used by the regex structural backend before
    branch tokens are counted. ``source`` may be a ``SourceRecord``, a mapping
    with ``path``/``content``, or raw content plus an optional ``path``.
    """
    source_path, content = _source_parts(source, path)
    language = _language_for_path(source_path)
    masked = _mask_comments_and_strings(content, hash_comments=language == "python")
    raw_complexity = _word_count(masked, _WORD_RULES[language]) + _operator_count(masked, language)
    if language == "qml":
        raw_complexity += _qml_binding_count(content, masked)
    if raw_complexity <= 0:
        return 0
    line_count = max(1, len(content.splitlines()))
    return max(1, int(round(raw_complexity * 1000 / line_count)))


def hotspot_stats(churn: int, complexity: int) -> dict[str, int]:
    """Build the persisted, JSON-friendly hotspot payload."""
    churn_value = max(0, int(churn))
    complexity_value = max(0, int(complexity))
    return {
        "churn": churn_value,
        "complexity": complexity_value,
        "score": churn_value * complexity_value,
    }


def compute_hotspots(
    sources: Iterable[SourceRecord | Mapping[str, Any]],
    commits: Iterable[CommitRecord | Mapping[str, Any]],
) -> dict[str, dict[str, int]]:
    """Compute ``{path: {churn, complexity, score}}`` for indexed sources."""
    source_list = list(sources)
    commit_list = list(commits)
    churn = compute_churn(commit_list)
    result: dict[str, dict[str, int]] = {}
    for source in source_list:
        source_path = str(source.get("path", "")) if isinstance(source, Mapping) else str(getattr(source, "path", ""))
        result[source_path] = hotspot_stats(churn.get(_normalise_path(source_path), 0), estimate_complexity(source))
    return result


def annotate_file_nodes(nodes: Iterable[GraphNode], stats: Mapping[str, Mapping[str, Any]]) -> None:
    """Attach hotspot payloads to file nodes without disturbing other metadata."""
    for node in nodes:
        if node.kind != "file":
            continue
        values = stats.get(node.source_ref)
        if values is None:
            values = hotspot_stats(0, 0)
        churn = int(values.get("churn", 0))
        complexity = int(values.get("complexity", 0))
        node.metadata["hotspot"] = {
            "churn": churn,
            "complexity": complexity,
            "score": int(values.get("score", churn * complexity)),
        }


def top_hotspots(nodes: Iterable[GraphNode], limit: int = _HOTSPOT_LIMIT) -> list[dict[str, int | str]]:
    """Return ranked, flat hotspot records suitable for MCP/report output."""
    records: list[dict[str, int | str]] = []
    for node in nodes:
        if node.kind != "file":
            continue
        values = node.metadata.get("hotspot", {})
        if not isinstance(values, Mapping):
            continue
        churn = int(values.get("churn", 0))
        complexity = int(values.get("complexity", 0))
        score = int(values.get("score", churn * complexity))
        if score <= 0:
            continue
        records.append(
            {"path": node.source_ref, "churn": churn, "complexity": complexity, "score": score}
        )
    records.sort(
        key=lambda item: (
            -int(item["score"]),
            -int(item["churn"]),
            -int(item["complexity"]),
            str(item["path"]),
        )
    )
    return records[: max(0, limit)]


__all__ = [
    "annotate_file_nodes",
    "compute_churn",
    "compute_hotspots",
    "estimate_complexity",
    "hotspot_stats",
    "top_hotspots",
]
