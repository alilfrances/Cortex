from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from ..models import GraphEdge, GraphNode
from . import call_sites, regex_backend

_PACK_NAMES = {
    ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".go": "go", ".rs": "rust", ".swift": "swift", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "cpp", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".qml": "qmljs",
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
    # The managed language pack is the single parser source of truth. It
    # contains every grammar Cortex dispatches, including separate TSX/QMLJS
    # variants. Parser loading is cache-only; failures select the regex
    # fallback at the structural dispatcher.
    import os
    import tree_sitter_language_pack as pack
    cache = os.environ.get("CORTEX_PARSER_CACHE")
    if not cache:
        from ..runtime import configure_parser_environment
        verified = configure_parser_environment()
        cache = str(verified / "parser-cache") if verified else None
    if not cache:
        raise RuntimeError("managed parser runtime is not ready")
    pack.configure(pack.PackConfig(cache_dir=cache, languages=[]))
    return pack.get_language(_PACK_NAMES[suffix])


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
        # ES2022 `#private` class members (`#add() {}`); without it the walk fell
        # through to the next identifier child and mislabeled the method after a
        # generic parameter / return type (sindresorhus/p-queue `async #add<T>()`).
        "private_property_identifier",
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


def _extract_ruby_method_symbols(
    path: str,
    source: bytes,
    root: Any,
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen_symbols: set[str],
    def_symbols: dict[tuple[int, int], str],
) -> None:
    """`def` / `def self.x` symbols (the generic table has no Ruby `method` node type).

    Runs after the class/module pass so it can only *add* symbols: a method whose bare name is
    already taken (by a class or by an earlier method) is not a new node, and ids keep the
    `symbol:<path>:<name>` scheme (same-named methods of different classes in one file share a
    node, like every other language).  Ownership is lexical: the class/module span contains it.
    """
    method_ids: set[str] = set()
    for node in _iter_nodes(root):
        if node.type not in {"method", "singleton_method"}:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None:
            continue
        name = _node_text(name_node, source)
        symbol_id = f"symbol:{path}:{name}"
        key = (node.start_byte, node.end_byte)
        if name in seen_symbols:
            if symbol_id in method_ids:
                def_symbols.setdefault(key, symbol_id)  # same-named method: calls attributed to the shared node
            continue
        seen_symbols.add(name)
        method_ids.add(symbol_id)
        def_symbols[key] = symbol_id
        line = node.start_point[0] + 1
        nodes.append(
            GraphNode(
                node_id=symbol_id,
                kind="func",
                label=name,
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


def extract_treesitter_edges(
    path: str,
    content: str,
    known_paths: set[str],
    connect_names: list[str] | None = None,
) -> tuple[list[GraphNode], list[GraphEdge]]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix not in _PACK_NAMES:
        return [], []

    source = content.encode("utf-8", errors="replace")
    parser = _parser_for_language(_language_for_suffix(suffix))
    tree = parser.parse(source)
    root = tree.root_node
    # Tree-sitter ERROR nodes are recoverable for supported languages. Keep
    # unaffected declarations; the dedicated QML backend exposes diagnostics
    # explicitly and this generic path remains compatible for direct callers.

    file_node_id = f"file:{path}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_symbols: set[str] = set()
    def_symbols: dict[tuple[int, int], str] = {}
    def_qualifiers: dict[tuple[int, int], str] = {}
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
        if qualifier is None and suffix in {".go", ".rs", ".swift"}:
            qualifier = call_sites.method_qualifier(suffix, node, source)
        name = member_name or _name_for_node(node, source)
        if name and name in seen_symbols:
            # Same-named definitions share one node (documented non-goal); calls
            # inside the duplicate are attributed to that node.
            def_symbols.setdefault((node.start_byte, node.end_byte), f"symbol:{path}:{name}")
            if qualifier:
                def_qualifiers[(node.start_byte, node.end_byte)] = qualifier
        if not name or name in seen_symbols:
            continue
        seen_symbols.add(name)
        line = node.start_point[0] + 1
        symbol_id = f"symbol:{path}:{name}"
        def_symbols[(node.start_byte, node.end_byte)] = symbol_id
        if qualifier:
            def_qualifiers[(node.start_byte, node.end_byte)] = qualifier
        metadata: dict[str, Any] = {"lineno": line}
        if qualifier:
            metadata["qualifier"] = qualifier
        if suffix in _CPP_SUFFIXES | {".c"} and node.type in {"class_specifier", "struct_specifier"}:
            member_types = call_sites.cpp_member_types(node, source)
            if member_types:
                # Declared member types let the call resolver type `m_x->f()` in an
                # out-of-line `Foo::bar()` whose class body lives in this file.
                metadata["member_types"] = member_types
        if suffix in {".js", ".jsx", ".ts", ".tsx", ".java", ".swift", ".rb"} and _DEF_TYPES[node.type] == "class":
            bases = call_sites.class_bases(suffix, node, source)
            if bases:
                # These extractors emit no `inherits` edges; the resolver reads bases here.
                metadata["bases"] = bases
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

    if suffix == ".rb":
        _extract_ruby_method_symbols(path, source, root, file_node_id, nodes, edges, seen_symbols, def_symbols)

    if call_sites.supports_calls(suffix):
        # Additive pass: describes call sites only; resolution is callresolve's job.
        # Runs before the Qt regex pass so `emit`/`connect` keep their own edges.
        edges.extend(
            call_sites.extract_calls(
                suffix,
                root,
                call_sites.CallContext(
                    path=path,
                    source=source,
                    file_node_id=file_node_id,
                    def_symbols=def_symbols,
                    def_qualifiers=def_qualifiers,
                    name_of=_name_for_node,
                    member_of=_cpp_member_for_definition,
                    known_paths=known_paths,
                ),
            )
        )

    if suffix in _CPP_SUFFIXES:
        regex_backend._extract_qt_cpp_edges(path, content, file_node_id, nodes, edges, seen_symbols, connect_names)
        regex_backend._extract_qml_cpp_registration(path, content, file_node_id, nodes, edges, seen_symbols)
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
