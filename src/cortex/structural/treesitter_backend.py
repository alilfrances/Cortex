from __future__ import annotations

import importlib
from pathlib import PurePosixPath
from typing import Any

from ..models import GraphEdge, GraphNode

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
    "ui_object_definition": "class",
}

_BODY_REQUIRED_TYPES = {
    "struct_specifier",
    "class_specifier",
    "enum_specifier",
    "union_specifier",
    "namespace_definition",
}


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


def extract_treesitter_edges(
    path: str,
    content: str,
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    del known_paths
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

    for node in _iter_nodes(root):
        if node.type in _IMPORT_TYPES:
            if node.type == "call" and not _signature(node, source).startswith(("require", "require_relative", "load")):
                continue
            target = _import_target(node, source)
            line = node.start_point[0] + 1
            edges.append(
                GraphEdge(
                    edge_id=f"treesitter:{path}:import:{line}:{target}",
                    source=file_node_id,
                    target=f"module:{target or 'unknown'}",
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
        name = _name_for_node(node, source)
        if not name or name in seen_symbols:
            continue
        seen_symbols.add(name)
        line = node.start_point[0] + 1
        symbol_id = f"symbol:{path}:{name}"
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
                metadata={"lineno": line},
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

    return nodes, edges
