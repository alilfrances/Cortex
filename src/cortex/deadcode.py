from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Collection, Mapping, Sequence

from .gitutils import discover_repo_root
from .models import GraphEdge, GraphNode
from .references import AnalysisStopped, GraphReferenceIndex, ReferenceCorpus, find_references
from .store import CortexStore, default_db_path
from .testpatterns import is_test_path
from .tokenizer import count_text_tokens


_GRAPH_RELATIONS = {"calls", "imports", "inherits", "references", "reads", "writes", "aliases", "exports"}
_QT_RELATIONS = {"connects", "emits", "handles", "instantiates", "binds"}
_QT_KINDS = {"signal", "slot", "handler", "property", "id", "binding", "enum", "enum_member", "module"}
_COMMENT_PREFIXES = ("#", "//", "/*", "*", "*/", "<!--", "///")
# QML symbol kinds that record a use or a local, never a definition someone could
# delete: property bindings and attached-property settings (`ScrollBar.vertical:`),
# references to external types, module imports, function parameters and `id:`
# names. Reporting them as unreferenced is always a false positive.
_NON_DEFINITION_QML_KINDS = frozenset({"binding", "external", "import", "parameter", "id"})


def _is_test_path(path: str) -> bool:
    return is_test_path(path)


def _is_dunder_or_entry_point(node: GraphNode) -> bool:
    label = node.label
    path = node.source_ref.lower()
    return (
        label == "main"
        or label == "__main__"
        or Path(path).name == "__main__.py"
        or (label.startswith("__") and label.endswith("__"))
    )


def _decorated_lines(content: str) -> frozenset[int]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return frozenset()
    return frozenset(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list
    )


_OVERRIDE_MARKER_RE = re.compile(r"@Override\b|\boverride\b|\bvirtual\b")
_CLASS_QUALIFIER_RE = re.compile(r"\b([A-Za-z_]\w*)::~?[A-Za-z_]\w*\s*\(")
_OVERRIDE_REASON = "no incoming edges; overrides a base-class or framework method"
_METHOD_KINDS = frozenset({"func", "method"})


class _OverrideIndex:
    """Recognises methods that a base class or a framework may call.

    A method is flagged when its declaration carries an override marker
    (`@Override`, `override`, `virtual`) or when its enclosing class declares it
    with one (an out-of-line C++ definition has no marker of its own, its header
    declaration does), or when it shares a name with a method of a resolved base
    class. The graph links methods to their file, not their class, so the
    enclosing class comes from spans (Java, Kotlin, Swift, in-class C++) or from a
    `Class::method` qualifier (out-of-line C++ definitions). A method of a class
    with an external base and no marker is deliberately not flagged: nearly every
    class has some framework base, so that rule would hide real dead code.
    """

    def __init__(self, nodes: Sequence[GraphNode], edges: Sequence[GraphEdge], sources: Mapping[str, str]) -> None:
        self._sources = sources
        self._classes_by_file: dict[str, list[GraphNode]] = defaultdict(list)
        self._classes_by_label: dict[str, list[GraphNode]] = defaultdict(list)
        self._methods_by_file: dict[str, list[GraphNode]] = defaultdict(list)
        self._nodes_by_id = {node.node_id: node for node in nodes}
        for node in nodes:
            if node.granularity != "symbol":
                continue
            if node.kind == "class":
                self._classes_by_file[node.source_ref].append(node)
                self._classes_by_label[node.label].append(node)
            elif node.kind in _METHOD_KINDS:
                self._methods_by_file[node.source_ref].append(node)
        self._bases: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            if edge.relation == "inherits":
                self._bases[edge.source].append(edge.target)
        self._lines: dict[str, list[str]] = {}

    def _file_lines(self, path: str) -> list[str]:
        lines = self._lines.get(path)
        if lines is None:
            lines = self._lines[path] = self._sources.get(path, "").splitlines()
        return lines

    def _declaration_text(self, node: GraphNode) -> str:
        lines = self._file_lines(node.source_ref)
        start = node.span_start or 0
        if not 0 < start <= len(lines):
            return node.signature
        picked = [lines[start - 1]]
        index = start - 2
        while index >= 0 and start - 2 - index < 2 and lines[index].lstrip().startswith(("@", "[")):
            picked.append(lines[index])
            index -= 1
        if lines[start - 1].lstrip().startswith("@") and start < len(lines):
            picked.append(lines[start])  # the declaration under an annotation-only line
        return "\n".join(picked)

    def _enclosing_class(self, node: GraphNode) -> GraphNode | None:
        start = node.span_start or 0
        best: GraphNode | None = None
        for cls in self._classes_by_file.get(node.source_ref, ()):
            if cls.span_start is None or cls.span_end is None or cls.node_id == node.node_id:
                continue
            if cls.span_start <= start <= cls.span_end and (
                best is None or (cls.span_end - cls.span_start) < ((best.span_end or 0) - (best.span_start or 0))
            ):
                best = cls
        if best is not None:
            return best
        qualifier = _CLASS_QUALIFIER_RE.search(node.signature or "")
        if qualifier:
            matches = self._classes_by_label.get(qualifier.group(1), [])
            if len(matches) == 1:
                return matches[0]
        return None

    def _base_nodes(self, cls: GraphNode) -> list[GraphNode]:
        """Base classes that are defined in the repository."""
        resolved: list[GraphNode] = []
        for target in self._bases.get(cls.node_id, ()):
            base = self._nodes_by_id.get(target)
            if base is None and target.startswith("name:"):
                # Cross-file bases are often left as `name:<Label>`; accept a unique class of that name.
                named = self._classes_by_label.get(target.removeprefix("name:").rsplit("::", 1)[-1], [])
                base = named[0] if len(named) == 1 else None
            if base is not None and base.kind == "class":
                resolved.append(base)
        return resolved

    def _class_declares_with_marker(self, cls: GraphNode, name: str) -> bool:
        lines = self._file_lines(cls.source_ref)
        start, end = cls.span_start or 0, cls.span_end or 0
        if not 0 < start <= end:
            return False
        body = "\n".join(lines[start - 1:end])
        escaped = re.escape(name)
        return bool(
            re.search(rf"\b{escaped}\s*\([^;{{}}]*\)[^;{{}}]*\boverride\b", body)
            or re.search(rf"\bvirtual\b[^;{{}}]*\b{escaped}\s*\(", body)
        )

    def _declares_method(self, cls: GraphNode, name: str, seen: set[str], depth: int = 0) -> bool:
        if cls.node_id in seen or depth > 6:
            return False
        seen.add(cls.node_id)
        for method in self._methods_by_file.get(cls.source_ref, ()):
            if method.label == name and cls.span_start is not None and cls.span_end is not None \
                    and cls.span_start <= (method.span_start or 0) <= cls.span_end:
                return True
        for method in self._methods_by_file.values():
            for candidate in method:
                if candidate.label == name and f"{cls.label}::{name}" in (candidate.signature or ""):
                    return True
        return any(self._declares_method(base, name, seen, depth + 1) for base in self._base_nodes(cls))

    def is_override(self, node: GraphNode) -> bool:
        if node.kind not in _METHOD_KINDS:
            return False
        if _OVERRIDE_MARKER_RE.search(self._declaration_text(node)):
            return True
        cls = self._enclosing_class(node)
        if cls is None:
            return False
        if self._class_declares_with_marker(cls, node.label):
            return True
        return any(self._declares_method(base, node.label, set()) for base in self._base_nodes(cls))


class _AnalysisContext:
    """Per-analysis caches so per-symbol work does not repeat repo-wide I/O and parsing."""

    def __init__(
        self,
        repo_root: Path,
        store: CortexStore,
        all_sources: Sequence[Any],
        reference_budget: int,
        graph_nodes: Sequence[GraphNode] | None = None,
        graph_edges: Sequence[GraphEdge] | None = None,
        should_stop: Callable[[], bool] | None = None,
        corpus_symbols: Collection[str] | None = None,
    ) -> None:
        self.should_stop = should_stop
        self.corpus_symbols = corpus_symbols
        self.repo_root = repo_root
        self.store = store
        self.reference_budget = reference_budget
        self._graph_nodes = graph_nodes
        self._graph_edges = graph_edges
        self._graph_index: GraphReferenceIndex | None = None
        self.source_by_path: dict[str, str] = {source.path: source.content for source in all_sources}
        # Both repo-wide qmlRegisterType patterns require this literal, so other sources cannot match.
        self.qml_register_sources: list[Any] = [source for source in all_sources if "qmlRegisterType" in source.content]
        self._decorated: dict[str, frozenset[int]] = {}
        self._overrides: _OverrideIndex | None = None
        self._corpus: ReferenceCorpus | None = None
        self._references: dict[str, dict[str, Any]] = {}

    def content(self, path: str) -> str:
        return self.source_by_path.get(path) or ""

    def is_override(self, node: GraphNode) -> bool:
        if self._overrides is None:
            self._overrides = _OverrideIndex(self._graph_nodes or (), self._graph_edges or (), self.source_by_path)
        return self._overrides.is_override(node)

    def is_decorated(self, path: str, line: int | None) -> bool:
        if line is None:
            return False
        lines = self._decorated.get(path)
        if lines is None:
            lines = self._decorated[path] = _decorated_lines(self.content(path))
        return line in lines

    def references(self, label: str) -> dict[str, Any]:
        result = self._references.get(label)
        if result is None:
            if self._corpus is None:
                symbols = self.corpus_symbols
                if symbols is None and self._graph_nodes is not None:
                    symbols = {node.label for node in self._graph_nodes if node.granularity == "symbol"}
                self._corpus = ReferenceCorpus(self.repo_root, symbols, self.should_stop)
            if self._graph_index is None and self._graph_nodes is not None and self._graph_edges is not None:
                self._graph_index = GraphReferenceIndex(self._graph_nodes, self._graph_edges)
            result = self._references[label] = find_references(
                self.store,
                self.repo_root,
                label,
                self.reference_budget,
                corpus=self._corpus,
                graph_index=self._graph_index,
            )
        return result


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


def _reference_text(value: Any) -> str:
    """find_references items are dicts ({text, origin, access}); older rows
    and tests may still hand in plain strings."""
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value)


def _line_location(value: Any) -> tuple[str, int | None] | None:
    text = _reference_text(value)
    path, separator, line = text.rpartition(":")
    if not separator or not line.isdigit():
        return None
    return path, int(line)


def _comment_or_doc_hit(
    repo_root: Path,
    store: CortexStore,
    location: str,
    source_by_path: Mapping[str, str] | None = None,
) -> bool:
    parsed = _line_location(location)
    if parsed is None:
        return False
    path, line_number = parsed
    if Path(path).suffix.lower() in {".md", ".rst", ".txt"}:
        return True
    content = source_by_path.get(path) if source_by_path is not None else store.fetch_source_content(repo_root, path)
    if content is None or line_number is None:
        return False
    lines = content.splitlines()
    if not 0 < line_number <= len(lines):
        return False
    line = lines[line_number - 1].strip()
    if line.startswith(_COMMENT_PREFIXES):
        return True
    return any(marker in line for marker in (" #", " //", " /*", " <!--"))


def _qt_macro_flags(node: GraphNode, content: str, qml_register_sources: Sequence[Any]) -> tuple[bool, bool]:
    lines = content.splitlines()
    start = max(0, (node.span_start or 1) - 1 - 20)
    end = min(len(lines), (node.span_end or node.span_start or 1) + 20)
    nearby = "\n".join(lines[start:end])
    label = re.escape(node.label)
    qml_registration = re.search(r"\bQML_ELEMENT\b", nearby) is not None
    # The per-label patterns need this literal; checking it first avoids compiling them per symbol.
    if not qml_registration and "qmlRegisterType" in nearby:
        qml_registration = any(
            re.search(pattern, nearby) is not None
            for pattern in (
                rf"\bqmlRegisterType\s*<\s*{label}\s*>",
                rf"\bqmlRegisterType\s*\([^)]*\b{label}\b",
            )
        )
    if not qml_registration and node.kind == "class":
        qml_registration = any(
            re.search(rf"\bqmlRegisterType\s*<\s*{label}\s*>", source.content)
            or re.search(rf"\bqmlRegisterType\s*\([^)]*\b{label}\b", source.content)
            for source in qml_register_sources
        )
    qt_macro = bool(re.search(r"\bQ_INVOKABLE\b|\bQ_PROPERTY\b", nearby))
    return qt_macro, qml_registration


def _reference_tier(
    repo_root: Path,
    store: CortexStore,
    node: GraphNode,
    reference_budget: int,
    context: _AnalysisContext | None = None,
) -> tuple[str, str]:
    if context is not None:
        result = context.references(node.label)
    else:
        result = find_references(store, repo_root, node.label, reference_budget)
    source_by_path = context.source_by_path if context is not None else None
    locations: list[tuple[str, str]] = []
    own_start = node.span_start or 0
    own_end = node.span_end or own_start
    for bucket in sorted(result.get("items", {})):
        for value in sorted(_reference_text(entry) for entry in result["items"].get(bucket, [])):
            parsed = _line_location(value)
            if parsed is not None and parsed[0] == node.source_ref and own_start <= (parsed[1] or 0) <= own_end:
                continue
            locations.append((bucket, value))

    if not locations:
        return "high", "no incoming edges; no grep refs"
    if all(bucket == "doc" or _comment_or_doc_hit(repo_root, store, location, source_by_path) for bucket, location in locations):
        return "medium", "no incoming edges; grep refs only in comments/docs"
    return "low", "no incoming edges; grep refs in code/config"


def _candidate_reason(
    repo_root: Path,
    store: CortexStore,
    node: GraphNode,
    all_sources: Sequence[Any],
    reference_budget: int,
    context: _AnalysisContext | None = None,
) -> tuple[str, str]:
    if context is None:
        context = _AnalysisContext(repo_root, store, all_sources, reference_budget)
    if context.is_override(node):
        return "low", _OVERRIDE_REASON
    content = context.content(node.source_ref)
    qt_kind = node.metadata.get("qt") if isinstance(node.metadata, dict) else None
    qml_kind = node.metadata.get("qml_kind") if isinstance(node.metadata, dict) else None
    qt_macro, qml_registration = _qt_macro_flags(node, content, context.qml_register_sources)
    if qt_kind in _QT_KINDS or qml_kind in _QT_KINDS or qt_macro or qml_registration:
        return "low", "no incoming edges; Qt meta-object or dynamic-language caveat"
    parser_backend = node.metadata.get("parser_backend") if isinstance(node.metadata, dict) else None
    if not (node.source_ref.lower().endswith(".py") or parser_backend == "treesitter"):
        return "low", "no incoming edges; regex/dynamic-language caveat"
    return _reference_tier(repo_root, store, node, reference_budget, context)


def analyze_dead_code(
    repo_path: Path | str,
    db_path: Path | None = None,
    *,
    budget: int = 2000,
    store: CortexStore | None = None,
    nodes: Sequence[GraphNode] | None = None,
    edges: Sequence[GraphEdge] | None = None,
    should_stop: Callable[[], bool] | None = None,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    """Dead-code candidates. `path_prefix` (a repo-relative file or directory)
    restricts which symbols are *reported*; references are still searched
    repo-wide, so a use outside the prefix still credits a symbol inside it."""
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
    def in_scope(node: GraphNode) -> bool:
        return not path_prefix or node.source_ref == path_prefix or node.source_ref.startswith(path_prefix.rstrip("/") + "/")

    # Only the candidates' own names are ever looked up, so a scoped run indexes just those
    # (any other name would fall back to a slower substring test, but is never asked for).
    corpus_symbols = (
        {item.label for item in graph_nodes if item.granularity == "symbol" and in_scope(item)} if path_prefix else None
    )
    context = _AnalysisContext(
        repo_root, active_store, all_sources, max(2000, int(budget)), graph_nodes, graph_edges,
        should_stop=should_stop, corpus_symbols=corpus_symbols,
    )
    findings: list[dict[str, Any]] = []
    partial = False
    for node in sorted(
        (item for item in graph_nodes if item.granularity == "symbol"),
        key=lambda item: (item.source_ref, item.label, item.node_id),
    ):
        if not in_scope(node):
            continue
        if should_stop is not None and should_stop():
            partial = True
            break
        if _is_test_path(node.source_ref) or _is_dunder_or_entry_point(node) or node.label in reexports:
            continue
        if node.metadata.get("qt") == "handler":
            continue
        if node.metadata.get("qml_kind") in _NON_DEFINITION_QML_KINDS:
            continue
        if context.is_decorated(node.source_ref, node.span_start):
            continue
        node_incoming = incoming.get(node.node_id, [])
        if node_incoming:
            continue
        if node.node_id in credited_nodes or node.label in credited_labels:
            continue
        try:
            tier, reason = _candidate_reason(
                repo_root, active_store, node, all_sources, context.reference_budget, context
            )
        except AnalysisStopped:
            partial = True  # stopped while building the reference corpus: this candidate is unfinished
            break
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
    return {"repo_path": str(repo_root), "findings": findings, "partial": partial}


def truncate_dead_code_result(result: dict[str, Any], budget: int) -> dict[str, Any]:
    base = json.loads(json.dumps(result, sort_keys=True))
    limit = max(0, int(budget))
    has_findings = "findings" in base
    findings: list[Any] = base.get("findings") or []
    total = len(findings)

    # Dropping findings from the end only shrinks the payload, so the largest
    # prefix that fits is found by binary search instead of re-serializing
    # after every single pop.
    def fits(count: int) -> bool:
        candidate = dict(base)
        candidate["budget"] = limit
        candidate["truncated"] = count < total
        candidate["findings"] = findings[:count]
        return count_text_tokens(json.dumps(candidate, sort_keys=True)) <= limit

    lo, hi, best = 0, total, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0 or fits(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    output = dict(base)
    output["budget"] = limit
    output["truncated"] = best < total
    if has_findings:
        output["findings"] = findings[:best]
    output["returned_count"] = best
    output["budget_feasible"] = count_text_tokens(json.dumps(output, sort_keys=True)) <= limit
    return output


__all__ = [
    "analyze_dead_code",
    "truncate_dead_code_result",
]
