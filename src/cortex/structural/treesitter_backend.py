from __future__ import annotations

import importlib
from pathlib import PurePosixPath
from typing import Any

from ..models import GraphEdge, GraphNode
from . import regex_backend

_LANGUAGE_MODULES = {
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
    ".swift": ("tree_sitter_swift", "language"),
    ".java": ("tree_sitter_java", "language"),
    ".rb": ("tree_sitter_ruby", "language"),
    ".c": ("tree_sitter_c", "language"),
    ".h": ("tree_sitter_cpp", "language"),
    ".cpp": ("tree_sitter_cpp", "language"),
    ".cc": ("tree_sitter_cpp", "language"),
    ".cxx": ("tree_sitter_cpp", "language"),
    ".hpp": ("tree_sitter_cpp", "language"),
    ".hh": ("tree_sitter_cpp", "language"),
    ".hxx": ("tree_sitter_cpp", "language"),
    ".qml": ("tree_sitter_language_pack", "qml"),
}

_IMPORT_TYPES = {
    "import_declaration",
    "import_statement",
    "use_declaration",
    "require",
    "call",
    "preproc_include",
    "ui_import",
}

_DEF_TYPES = {
    "function_declaration": "func",
    "function_definition": "func",
    "method_declaration": "func",
    "method_definition": "func",
    "function_item": "func",
    "class_declaration": "class",
    "class": "class",
    "class_definition": "class",
    "interface_declaration": "class",
    "struct_item": "class",
    "struct_declaration": "class",
    "enum_item": "class",
    "enum_declaration": "class",
    "trait_item": "class",
    "protocol_declaration": "class",
    "module": "class",
    "struct_specifier": "class",
    "class_specifier": "class",
    "enum_specifier": "class",
    "union_specifier": "class",
    "namespace_definition": "class",
}

_BODY_REQUIRED_TYPES = {
    "struct_specifier",
    "class_specifier",
    "enum_specifier",
    "union_specifier",
    "namespace_definition",
}

_CPP_SUFFIXES = {".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}


def _language_for_suffix(suffix: str) -> Any:
    from tree_sitter import Language

    module_name, func_name = _LANGUAGE_MODULES[suffix]
    if suffix == ".qml":
        from tree_sitter_language_pack import get_language

        return get_language("qml")
    grammar = importlib.import_module(module_name)
    return Language(getattr(grammar, func_name)())


def _parser_for_language(language: Any) -> Any:
    from tree_sitter import Parser

    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        parser.set_language(language)
        return parser


def _node_text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _name_for_node(node: Any, source: bytes) -> str:
    for field in ("name", "declarator"):
        child = node.child_by_field_name(field)
        if child is None:
            continue
        name = _identifier_text(child, source)
        if name:
            return name
    for child in node.children:
        name = _identifier_text(child, source)
        if name:
            return name
    return ""


def _cpp_member_for_definition(node: Any, source: bytes) -> tuple[str | None, str]:
    """Return the class qualifier and bare member name for a C++ definition."""
    declarator = node.child_by_field_name("declarator")
    first_declarator = declarator
    while declarator is not None:
        if declarator.type in {"qualified_identifier", "scoped_identifier"}:
            scope = declarator.child_by_field_name("scope")
            name = declarator.child_by_field_name("name")
            if scope is not None and name is not None:
                if name.type in {"qualified_identifier", "scoped_identifier"}:
                    # In A::B::member, the nested name carries the final
                    # scope (B) and member (member).
                    declarator = name
                    continue
                qualifier = _node_text(scope, source).rstrip(":").split("::")[-1]
                return qualifier or None, _node_text(name, source)
        declarator = declarator.child_by_field_name("declarator")

    return None, _identifier_text(first_declarator, source) if first_declarator is not None else ""


def _identifier_text(node: Any, source: bytes) -> str:
    if node.type in {
        "identifier",
        "property_identifier",
        "type_identifier",
        "constant",
        "simple_identifier",
        "field_identifier",
        "namespace_identifier",
    }:
        return _node_text(node, source)
    for child in node.children:
        name = _identifier_text(child, source)
        if name:
            return name
    return ""


def _iter_nodes(root: Any) -> Any:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _cpp_base_names(node: Any, source: bytes) -> list[str]:
    bases: list[str] = []
    for child in node.children:
        if child.type in {"type_identifier", "qualified_identifier", "scoped_type_identifier"}:
            bases.append(_node_text(child, source))
            continue
        if child.type in {"access_specifier", ",", ":"}:
            continue
        name = _identifier_text(child, source)
        if name:
            bases.append(name)
    return bases


def _extract_cpp_inheritance_edges(
    path: str,
    node: Any,
    source: bytes,
    symbol_id: str,
    class_name: str,
    edges: list[GraphEdge],
) -> None:
    for child in node.children:
        if child.type != "base_class_clause":
            continue
        line = node.start_point[0] + 1
        for base in _cpp_base_names(child, source):
            edges.append(
                GraphEdge(
                    edge_id=f"treesitter:{path}:inherits:{class_name}:{base}",
                    source=symbol_id,
                    target=f"name:{base}",
                    relation="inherits",
                    layer="STRUCTURAL",
                    confidence="EXTRACTED",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )


def _import_target(node: Any, source: bytes) -> str:
    for child in node.children:
        if child.type in {
            "string",
            "interpreted_string_literal",
            "raw_string_literal",
            "string_literal",
            "system_lib_string",
        }:
            return _node_text(child, source).strip("\"'`<>")
        if child.type in {"scoped_identifier", "identifier", "package_identifier", "dotted_name"}:
            return _node_text(child, source)
    text = _node_text(node, source).strip().rstrip(";")
    if text.startswith("import "):
        return text.split(maxsplit=2)[1].strip("\"'`")
    return text.split(maxsplit=1)[-1].strip("\"'`") if text else "unknown"


def _signature(node: Any, source: bytes) -> str:
    text = _node_text(node, source)
    return text.splitlines()[0].strip()


def _qml_component_node(
    path: str,
    content: str,
    source: bytes,
    root: Any,
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen_symbols: set[str],
) -> None:
    stem = PurePosixPath(path).stem
    if not stem or not stem[0].isupper():
        return

    root_object = next((node for node in _iter_nodes(root) if node.type == "ui_object_definition"), None)
    if root_object is None:
        line = 1
        signature = content.splitlines()[0].strip() if content.splitlines() else ""
        span_end = regex_backend._line_count(content)
    else:
        line = root_object.start_point[0] + 1
        signature = _signature(root_object, source)
        span_end = root_object.end_point[0] + 1

    seen_symbols.add(stem)
    node = regex_backend._symbol_node(path, stem, "class", signature, line, span_end=span_end)
    nodes.append(node)
    edges.append(
        GraphEdge(
            edge_id=f"treesitter:{path}:contains:{stem}",
            source=file_node_id,
            target=node.node_id,
            relation="contains",
            layer="STRUCTURAL",
            confidence="EXTRACTED",
            weight=1.0,
            metadata={"lineno": line, "source_file": path},
        )
    )


def _qml_instantiates_edge(
    path: str,
    node: Any,
    source: bytes,
    known_paths: set[str],
    file_node_id: str,
    index: int,
) -> GraphEdge | None:
    name = _name_for_node(node, source)
    if not name:
        return None
    resolved = regex_backend.resolve_qml_component(name, known_paths)
    component_kind = "qml"
    if resolved is None:
        resolved = regex_backend.resolve_qml_cpp_type(name, known_paths)
        component_kind = "cpp"
    if resolved is None or resolved == path:
        return None
    line = node.start_point[0] + 1
    target = f"file:{resolved}" if component_kind == "qml" else f"module:{name}"
    return GraphEdge(
        edge_id=f"treesitter:{path}:instantiates:{line}:{index}:{name}",
        source=file_node_id,
        target=target,
        relation="instantiates",
        layer="STRUCTURAL",
        confidence="EXTRACTED",
        weight=1.0,
        metadata={
            "lineno": line,
            "source_file": path,
            "type_name": name,
            "component_path": resolved,
            "component_kind": component_kind,
        },
    )


def extract_treesitter_edges(
    path: str,
    content: str,
    known_paths: set[str],
    connect_names: list[str] | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in _LANGUAGE_MODULES:
        return [], []

    source = content.encode("utf-8", errors="replace")
    parser = _parser_for_language(_language_for_suffix(suffix))
    tree = parser.parse(source)
    root = tree.root_node
    if root.has_error:
        raise ValueError(f"tree-sitter parse error in {path}")

    file_node_id = f"file:{path}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_symbols: set[str] = set()
    qml_object_index = 0

    if suffix == ".qml":
        _qml_component_node(path, content, source, root, file_node_id, nodes, edges, seen_symbols)

    for node in _iter_nodes(root):
        if suffix == ".qml" and node.type == "ui_object_definition":
            qml_object_index += 1
            edge = _qml_instantiates_edge(path, node, source, known_paths, file_node_id, qml_object_index)
            if edge is not None:
                edges.append(edge)
            continue
        if node.type in _IMPORT_TYPES:
            if node.type == "call" and not _signature(node, source).startswith(("require", "require_relative", "load")):
                continue
            target = _import_target(node, source)
            resolved = regex_backend.resolve_local_import(target, known_paths)
            line = node.start_point[0] + 1
            edges.append(
                GraphEdge(
                    edge_id=f"treesitter:{path}:import:{line}:{target}",
                    source=file_node_id,
                    target=f"file:{resolved}" if resolved else f"module:{target or 'unknown'}",
                    relation="imports",
                    layer="STRUCTURAL",
                    confidence="EXTRACTED",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )
        if node.type not in _DEF_TYPES:
            continue
        if node.type in _BODY_REQUIRED_TYPES and node.child_by_field_name("body") is None:
            continue
        qualifier: str | None = None
        member_name = ""
        if suffix in _CPP_SUFFIXES and node.type == "function_definition":
            qualifier, member_name = _cpp_member_for_definition(node, source)
        name = member_name or _name_for_node(node, source)
        if not name or name in seen_symbols:
            continue
        seen_symbols.add(name)
        line = node.start_point[0] + 1
        symbol_id = f"symbol:{path}:{name}"
        metadata: dict[str, Any] = {"lineno": line}
        if qualifier:
            metadata["qualifier"] = qualifier
        nodes.append(
            GraphNode(
                node_id=symbol_id,
                kind=_DEF_TYPES[node.type],
                label=name.split(".")[-1].split("::")[-1],
                source_ref=path,
                granularity="symbol",
                signature=_signature(node, source),
                span_start=line,
                span_end=node.end_point[0] + 1,
                metadata=metadata,
            )
        )
        edges.append(
            GraphEdge(
                edge_id=f"treesitter:{path}:contains:{name}",
                source=file_node_id,
                target=symbol_id,
                relation="contains",
                layer="STRUCTURAL",
                confidence="EXTRACTED",
                weight=1.0,
                metadata={"lineno": line, "source_file": path},
            )
        )
        if suffix in _CPP_SUFFIXES and node.type in {"class_specifier", "struct_specifier"}:
            _extract_cpp_inheritance_edges(path, node, source, symbol_id, name, edges)

    if suffix in _CPP_SUFFIXES:
        regex_backend._extract_qt_cpp_edges(path, content, file_node_id, nodes, edges, seen_symbols, connect_names)
    if suffix == ".qml":
        # The QML grammar's node types aren't mapped in _DEF_TYPES for a
        # "signal" declaration, so tree-sitter alone would silently drop
        # `signal foo(...)` as a symbol -- reuse the regex-based extraction
        # (P0-4) so the Qt-tagged signal index (graph.py::QtSymbolIndex) sees
        # the same symbols regardless of backend, exactly like the C++ Qt
        # edges above already do.
        regex_backend._extract_qml_signal_symbols(path, content, file_node_id, nodes, edges, seen_symbols)
        regex_backend._extract_qml_handlers(path, content, known_paths, file_node_id, nodes, edges)

    return nodes, edges
