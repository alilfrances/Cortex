from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from .gitutils import discover_repo_root
from .models import GraphEdge, GraphNode
from .references import find_references
from .store import CortexStore, default_db_path
from .tokenizer import count_text_tokens


_GRAPH_RELATIONS = {"calls", "imports", "inherits", "references"}
_QT_RELATIONS = {"connects", "emits", "handles", "instantiates"}
_QT_KINDS = {"signal", "slot", "handler"}
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "<!--", "///")


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    stem = Path(path).stem.lower()
    return "tests" in parts or stem.startswith("test_") or stem.endswith("_test")


def _is_dunder_or_entry_point(node: GraphNode) -> bool:
    label = node.label
    path = node.source_ref.lower()
    return (
        label == "main"
        or label == "__main__"
        or Path(path).name == "__main__.py"
        or (label.startswith("__") and label.endswith("__"))
    )


def _decorated_symbol(content: str, line: int | None) -> bool:
    if line is None:
        return False
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return False
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.lineno == line
        and bool(node.decorator_list)
        for node in ast.walk(tree)
    )


def _reexported_symbols(sources: Sequence[Any]) -> set[str]:
    exported: set[str] = set()
    for source in sources:
        if not source.path.endswith("__init__.py"):
            continue
        try:
            tree = ast.parse(source.content, filename=source.path)
        except SyntaxError:
            for match in re.finditer(r"\b(?:from|import)\b[^\n]*\b([A-Za-z_]\w*)\b", source.content):
                exported.add(match.group(1))
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                exported.update(alias.asname or alias.name.rsplit(".", 1)[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                exported.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Assign):
                if any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                    values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)) else ()
                    exported.update(value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str))
    return exported


def _line_location(value: Any) -> tuple[str, int | None] | None:
    text = str(value)
    path, separator, line = text.rpartition(":")
    if not separator or not line.isdigit():
        return None
    return path, int(line)


def _comment_or_doc_hit(repo_root: Path, store: CortexStore, location: str) -> bool:
    parsed = _line_location(location)
    if parsed is None:
        return False
    path, line_number = parsed
    if Path(path).suffix.lower() in {".md", ".rst", ".txt"}:
        return True
    content = store.fetch_source_content(repo_root, path)
    if content is None or line_number is None:
        return False
    lines = content.splitlines()
    if not 0 < line_number <= len(lines):
        return False
    line = lines[line_number - 1].strip()
    if line.startswith(_COMMENT_PREFIXES):
        return True
    return any(marker in line for marker in (" #", " //", " /*", " <!--"))


def _qt_macro_flags(node: GraphNode, content: str, all_sources: Sequence[Any]) -> tuple[bool, bool]:
    lines = content.splitlines()
    start = max(0, (node.span_start or 1) - 1 - 20)
    end = min(len(lines), (node.span_end or node.span_start or 1) + 20)
    nearby = "\n".join(lines[start:end])
    label = re.escape(node.label)
    qml_registration = any(
        re.search(pattern, nearby) is not None
        for pattern in (
            r"\bQML_ELEMENT\b",
            rf"\bqmlRegisterType\s*<\s*{label}\s*>",
            rf"\bqmlRegisterType\s*\([^)]*\b{label}\b",
        )
    )
    if not qml_registration and node.kind == "class":
        qml_registration = any(
            re.search(rf"\bqmlRegisterType\s*<\s*{label}\s*>", source.content)
            or re.search(rf"\bqmlRegisterType\s*\([^)]*\b{label}\b", source.content)
            for source in all_sources
        )
    qt_macro = bool(re.search(r"\bQ_INVOKABLE\b|\bQ_PROPERTY\b", nearby))
    return qt_macro, qml_registration


def _reference_tier(
    repo_root: Path,
    store: CortexStore,
    node: GraphNode,
    reference_budget: int,
) -> tuple[str, str]:
    result = find_references(store, repo_root, node.label, reference_budget)
    locations: list[tuple[str, str]] = []
    own_start = node.span_start or 0
    own_end = node.span_end or own_start
    for bucket in sorted(result.get("items", {})):
        for value in sorted(result["items"].get(bucket, [])):
            parsed = _line_location(value)
            if parsed is not None and parsed[0] == node.source_ref and own_start <= (parsed[1] or 0) <= own_end:
                continue
            locations.append((bucket, value))

    if not locations:
        return "high", "no incoming edges; no grep refs"
    if all(bucket == "doc" or _comment_or_doc_hit(repo_root, store, location) for bucket, location in locations):
        return "medium", "no incoming edges; grep refs only in comments/docs"
    return "low", "no incoming edges; grep refs in code/config"


def _candidate_reason(
    repo_root: Path,
    store: CortexStore,
    node: GraphNode,
    all_sources: Sequence[Any],
    reference_budget: int,
) -> tuple[str, str]:
    content = store.fetch_source_content(repo_root, node.source_ref) or ""
    qt_kind = node.metadata.get("qt") if isinstance(node.metadata, dict) else None
    qt_macro, qml_registration = _qt_macro_flags(node, content, all_sources)
    if qt_kind in _QT_KINDS or qt_macro or qml_registration:
        return "low", "no incoming edges; Qt meta-object or dynamic-language caveat"
    if not node.source_ref.lower().endswith(".py"):
        return "low", "no incoming edges; regex/dynamic-language caveat"
    return _reference_tier(repo_root, store, node, reference_budget)


def analyze_dead_code(
    repo_path: Path | str,
    db_path: Path | None = None,
    *,
    budget: int = 2000,
    store: CortexStore | None = None,
    nodes: Sequence[GraphNode] | None = None,
    edges: Sequence[GraphEdge] | None = None,
) -> dict[str, Any]:
    repo_root = discover_repo_root(Path(repo_path))
    active_store = store or CortexStore(db_path or default_db_path(repo_root))
    graph_nodes, graph_edges = (
        (list(nodes), list(edges)) if nodes is not None and edges is not None else active_store.fetch_graph(repo_root)
    )
    all_sources = active_store.fetch_sources(repo_root)
    nodes_by_id = {node.node_id: node for node in graph_nodes}
    incoming: defaultdict[str, list[GraphEdge]] = defaultdict(list)
    for edge in graph_edges:
        if edge.relation in _GRAPH_RELATIONS | _QT_RELATIONS:
            incoming[edge.target].append(edge)

    credited_labels: set[str] = set()
    credited_nodes: set[str] = set()
    for edge in graph_edges:
        if edge.relation not in _QT_RELATIONS:
            continue
        target = nodes_by_id.get(edge.target)
        if target is not None:
            credited_nodes.add(target.node_id)
            credited_labels.add(target.label)
        for key in ("signal_name", "slot_name", "handler_name", "type_name"):
            value = edge.metadata.get(key)
            if value:
                credited_labels.add(str(value))

    reexports = _reexported_symbols(all_sources)
    findings: list[dict[str, Any]] = []
    for node in sorted(
        (item for item in graph_nodes if item.granularity == "symbol"),
        key=lambda item: (item.source_ref, item.label, item.node_id),
    ):
        if _is_test_path(node.source_ref) or _is_dunder_or_entry_point(node) or node.label in reexports:
            continue
        if node.metadata.get("qt") == "handler":
            continue
        content = active_store.fetch_source_content(repo_root, node.source_ref) or ""
        if _decorated_symbol(content, node.span_start):
            continue
        node_incoming = incoming.get(node.node_id, [])
        if node_incoming:
            continue
        if node.node_id in credited_nodes or node.label in credited_labels:
            continue
        tier, reason = _candidate_reason(repo_root, active_store, node, all_sources, max(2000, int(budget)))
        findings.append(
            {
                "symbol": node.label,
                "file": node.source_ref,
                "line": node.span_start or 0,
                "confidence": tier,
                "reason": reason,
            }
        )

    findings.sort(key=lambda item: (str(item["file"]), str(item["symbol"]), int(item["line"])))
    return {"repo_path": str(repo_root), "findings": findings}


def truncate_dead_code_result(result: dict[str, Any], budget: int) -> dict[str, Any]:
    output = json.loads(json.dumps(result, sort_keys=True))
    limit = max(0, int(budget))
    output["budget"] = limit
    output["truncated"] = False
    while count_text_tokens(json.dumps(output, sort_keys=True)) > limit and output.get("findings"):
        output["findings"].pop()
        output["truncated"] = True
    output["returned_count"] = len(output.get("findings", []))
    output["budget_feasible"] = count_text_tokens(json.dumps(output, sort_keys=True)) <= limit
    return output


__all__ = [
    "analyze_dead_code",
    "truncate_dead_code_result",
]
