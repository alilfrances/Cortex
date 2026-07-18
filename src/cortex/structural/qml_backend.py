"""Tree-sitter QML structural extraction.

The qmljs grammar is intentionally used as the declaration boundary.  A
small amount of semantic resolution is kept in :mod:`qml_resolver`; this
module only turns declarations, scopes and JavaScript expressions into stable
Cortex nodes and edges.  It is not a QML compiler and never evaluates code.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from ..models import GraphEdge, GraphNode
from . import regex_backend


_IDENT_TYPES = {"identifier", "property_identifier", "type_identifier", "shorthand_property_identifier_pattern", "variable_identifier"}
_RESERVED = {
    "true", "false", "null", "undefined", "this", "if", "else", "for", "while", "return", "var", "let", "const",
    "function", "new", "typeof", "void", "delete", "in", "instanceof", "import", "as", "pragma", "property",
}


def _language() -> Any:
    import tree_sitter_language_pack as pack

    cache = os.environ.get("CORTEX_PARSER_CACHE")
    if not cache:
        from ..runtime import configure_parser_environment
        verified = configure_parser_environment()
        cache = str(verified / "parser-cache") if verified else None
    if not cache:
        raise RuntimeError("managed parser runtime is not ready")
    pack.configure(pack.PackConfig(cache_dir=cache, languages=[]))
    return pack.get_language("qmljs")


def _parser() -> Any:
    from tree_sitter import Parser
    language = _language()
    try:
        return Parser(language)
    except TypeError:
        parser = Parser()
        parser.set_language(language)
        return parser


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _line(source: bytes, offset: int) -> int:
    return source[:offset].count(b"\n") + 1


def _signature(node: Any, source: bytes) -> str:
    return _text(node, source).splitlines()[0].strip()


def _first_identifier(node: Any, source: bytes) -> str:
    if node.type in _IDENT_TYPES:
        return _text(node, source).strip()
    field = node.child_by_field_name("name") or node.child_by_field_name("type_name")
    if field is not None:
        return _text(field, source).strip()
    for child in node.children:
        if child.type in _IDENT_TYPES:
            return _text(child, source).strip()
    return ""


def _qualified_type(node: Any, source: bytes) -> str:
    field = node.child_by_field_name("type_name")
    if field is not None:
        return _text(field, source).strip()
    for child in node.children:
        if child.type in {"identifier", "qualified_identifier", "scoped_identifier", "type_identifier"}:
            return _text(child, source).strip()
    return _first_identifier(node, source)


def _node_id(path: str, owner: str, name: str) -> str:
    return f"symbol:{path}:{owner}.{name}"


def _add_edge(edges: list[GraphEdge], path: str, source: str, target: str, relation: str, node: Any, source_bytes: bytes, **metadata: Any) -> None:
    line = _line(source_bytes, node.start_byte) if node is not None else 1
    edge_id = f"qml:{path}:{relation}:{source}:{target}:{line}"
    if any(edge.edge_id == edge_id for edge in edges):
        return
    data = {"lineno": line, "source_file": path, **metadata}
    edges.append(GraphEdge(edge_id=edge_id, source=source, target=target, relation=relation, layer="STRUCTURAL", confidence="EXTRACTED", weight=1.0, metadata=data))


def _add_node(nodes: list[GraphNode], edges: list[GraphEdge], path: str, node_id: str, kind: str, label: str, node: Any, source: bytes, *, owner_id: str | None = None, metadata: dict[str, Any] | None = None, signature: str | None = None) -> GraphNode:
    if any(existing.node_id == node_id for existing in nodes):
        # Duplicate declarations are legal. Callers normally include an
        # ordinal in the id; a defensive suffix keeps extraction lossless.
        base = node_id
        ordinal = 2
        while any(existing.node_id == f"{base}#{ordinal}" for existing in nodes):
            ordinal += 1
        node_id = f"{base}#{ordinal}"
    start, end = node.start_byte, node.end_byte
    data = {"lineno": _line(source, start), "byte_start": start, "byte_end": end}
    if metadata:
        data.update(metadata)
    result = GraphNode(node_id=node_id, kind=kind, label=label, source_ref=path, granularity="symbol", signature=signature if signature is not None else _signature(node, source), span_start=_line(source, start), span_end=_line(source, end), metadata=data)
    nodes.append(result)
    _add_edge(edges, path, f"file:{path}", node_id, "contains", node, source)
    if owner_id and owner_id != f"file:{path}":
        _add_edge(edges, path, owner_id, node_id, "contains", node, source)
    return result


@dataclass
class _Scope:
    owner: str
    node_id: str
    type_name: str
    parent: "_Scope | None" = None
    ids: dict[str, str] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)
    signals: dict[str, str] = field(default_factory=dict)
    methods: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str) -> str | None:
        if name in self.ids:
            return self.ids[name]
        if name in self.members:
            return self.members[name]
        if name in self.signals:
            return self.signals[name]
        if name in self.methods:
            return self.methods[name]
        return self.parent.lookup(name) if self.parent else None


@dataclass
class _Context:
    path: str
    source: bytes
    known_paths: set[str]
    file_id: str
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    scopes: list[_Scope] = field(default_factory=list)
    root: _Scope | None = None
    imports: dict[str, dict[str, str]] = field(default_factory=dict)
    component_ids: dict[str, list[str]] = field(default_factory=dict)
    object_ordinals: dict[tuple[str, str], int] = field(default_factory=dict)

    def external(self, name: str, node: Any, *, member: str = "") -> str:
        target = f"external:qml:{self.path}:{name}{'.' + member if member else ''}"
        if not any(n.node_id == target for n in self.nodes):
            _add_node(self.nodes, self.edges, self.path, target, "external", member or name, node, self.source, metadata={"qml_kind": "external", "type_name": name, "member_name": member, "unverified": True, "uri": self.imports.get(name, {}).get("uri", "")})
        return target

    def object_id(self, parent: _Scope, type_name: str, node: Any, *, inline: bool = False) -> str:
        key = (parent.owner, type_name)
        ordinal = self.object_ordinals.get(key, 0) + 1
        self.object_ordinals[key] = ordinal
        suffix = "" if ordinal == 1 else f"#{ordinal}"
        return f"symbol:{self.path}:{parent.owner}.{type_name}{suffix}"


def _import_declarations(ctx: _Context, root: Any) -> None:
    # qmljs releases before annotation syntax was formalized recover the
    # marker as ERROR; retain it as a declaration-level annotation rather than
    # dropping it with the error subtree.
    for match in re.finditer(rb'(?m)^\s*@([A-Za-z_]\w*)', ctx.source):
        start, end = match.start(), match.end()
        class Annotation:
            start_byte = start
            end_byte = end
        annotation = Annotation()
        name = match.group(1).decode("utf-8", errors="replace")
        _add_node(ctx.nodes, ctx.edges, ctx.path, f"annotation:{ctx.path}:{name}:{start}", "annotation", name, annotation, ctx.source, metadata={"qml_kind": "annotation", "annotation": name})
    for node in root.children:
        if node.type == "ui_pragma":
            name = _first_identifier(node, ctx.source)
            _add_node(ctx.nodes, ctx.edges, ctx.path, f"pragma:{ctx.path}:{name}", "pragma", name, node, ctx.source, metadata={"qml_kind": "pragma"})
        if node.type != "ui_import":
            continue
        source_node = node.child_by_field_name("source")
        identifiers = [child for child in node.children if child.type in {"identifier", "dotted_name", "qualified_identifier"}]
        source = _text(source_node, ctx.source) if source_node is not None else (_text(identifiers[0], ctx.source) if identifiers else "")
        source = source.strip("\"'")
        version = next((_text(child, ctx.source) for child in node.children if child.type == "ui_version_specifier"), "")
        alias = ""
        for index, child in enumerate(node.children[:-1]):
            if _text(child, ctx.source) == "as":
                alias = _text(node.children[index + 1], ctx.source)
                break
        import_id = f"module:{source}"
        data = {"qml_kind": "import", "uri": source, "version": version, "alias": alias}
        _add_node(ctx.nodes, ctx.edges, ctx.path, f"import:{ctx.path}:{source}:{version}:{alias}", "module", source, node, ctx.source, metadata=data)
        ctx.imports[alias or source.rsplit(".", 1)[-1]] = {"uri": source, "version": version, "alias": alias}
        local_target = None
        if source in ctx.known_paths:
            local_target = source
        else:
            local_target = next((candidate for candidate in ctx.known_paths if candidate.endswith('/' + source) or candidate == source), None)
        _add_edge(ctx.edges, ctx.path, ctx.file_id, f"file:{local_target}" if local_target else import_id, "imports", node, ctx.source, uri=source, version=version, alias=alias, unverified=local_target is None)


def _parameter_declarations(
    ctx: _Context,
    owner_id: str,
    owner_name: str,
    parameter_node: Any,
) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for param in parameter_node.children:
        if param.type not in {"required_parameter", "optional_parameter", "ui_signal_parameter", "formal_parameter", "rest_pattern"}:
            continue
        name_node = param.child_by_field_name("pattern") or param.child_by_field_name("name")
        if name_node is None:
            name_node = next((c for c in param.children if c.type in _IDENT_TYPES), None)
        if name_node is None:
            continue
        name = _text(name_node, ctx.source)
        type_node = param.child_by_field_name("type")
        metadata = {"qml_kind": "parameter", "qml_owner": owner_name}
        if type_node is not None:
            metadata["type"] = _text(type_node, ctx.source).lstrip(": ")
        node_id = _node_id(ctx.path, owner_name, name)
        parameter = _add_node(ctx.nodes, ctx.edges, ctx.path, node_id, "parameter", name, param, ctx.source, owner_id=owner_id, metadata=metadata)
        parameters[name] = parameter.node_id
    return parameters


def _declaration_children(initializer: Any) -> list[Any]:
    return [child for child in initializer.children if child.type not in {"{", "}"}]


def _property(ctx: _Context, scope: _Scope, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, ctx.source)
    type_node = node.child_by_field_name("type")
    type_text = _text(type_node, ctx.source) if type_node is not None else "var"
    modifiers = [_text(c, ctx.source).strip() for c in node.children if c.type == "ui_property_modifier" or _text(c, ctx.source) in {"default", "final", "override", "readonly", "required", "virtual"}]
    is_alias = type_text == "alias"
    metadata: dict[str, Any] = {"qml_kind": "property", "qml_owner": scope.owner, "type": type_text, "modifiers": modifiers}
    if is_alias:
        metadata["alias"] = True
    value = node.child_by_field_name("value")
    if value is not None:
        metadata["has_binding"] = True
    node_id = _node_id(ctx.path, scope.owner, name)
    prop = _add_node(ctx.nodes, ctx.edges, ctx.path, node_id, "property", name, node, ctx.source, owner_id=scope.node_id, metadata=metadata)
    scope.members[name] = prop.node_id
    if is_alias and value is not None:
        prop.metadata["alias_target_text"] = _text(value, ctx.source).rstrip(";").strip()
    # Qt exposes a deterministic notify signal for every ordinary QML
    # property. It is searchable but marked implicit so analyses never call it
    # a handwritten declaration.
    if not is_alias:
        signal_name = f"{name}Changed"
        signal_id = _node_id(ctx.path, scope.owner, signal_name)
        implicit = _add_node(ctx.nodes, ctx.edges, ctx.path, signal_id, "func", signal_name, node, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "signal", "qt": "signal", "implicit": True, "qml_owner": scope.owner, "property_id": prop.node_id}, signature=f"signal {signal_name}()")
        scope.signals[signal_name] = implicit.node_id
        _add_edge(ctx.edges, ctx.path, prop.node_id, implicit.node_id, "references", node, ctx.source, implicit=True, notify=True)
    if value is not None:
        _binding(ctx, scope, name, value, node, alias=is_alias)


def _signal(ctx: _Context, scope: _Scope, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, ctx.source)
    node_id = _node_id(ctx.path, scope.owner, name)
    signal = _add_node(ctx.nodes, ctx.edges, ctx.path, node_id, "func", name, node, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "signal", "qt": "signal", "qml_owner": scope.owner})
    scope.signals[name] = signal.node_id
    parameters = node.child_by_field_name("parameters")
    if parameters is not None:
        _parameter_declarations(ctx, signal.node_id, f"{scope.owner}.{name}", parameters)
        signal.metadata["parameters"] = [_text(c, ctx.source).strip() for c in parameters.children if c.type.startswith("ui_signal_parameter")]


def _function(ctx: _Context, scope: _Scope, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, ctx.source)
    node_id = _node_id(ctx.path, scope.owner, name)
    is_connections_handler = scope.type_name == "Connections" and name.startswith("on") and len(name) > 2 and name[2].isupper()
    fn = _add_node(ctx.nodes, ctx.edges, ctx.path, node_id, "func", name, node, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "handler" if is_connections_handler else "method", "qt": "handler" if is_connections_handler else "method", "handler_name": name if is_connections_handler else "", "signal_name": name[2].lower() + name[3:] if is_connections_handler else "", "qml_owner": scope.owner, "generator": node.type == "generator_function_declaration"})
    scope.methods[name] = fn.node_id
    if is_connections_handler:
        _add_edge(ctx.edges, ctx.path, fn.node_id, f"module:{name[2].lower() + name[3:]}", "handles", node, ctx.source, signal_name=name[2].lower() + name[3:], handler_name=name, unverified=True)
    params = node.child_by_field_name("parameters")
    local_parameters = (
        _parameter_declarations(ctx, fn.node_id, f"{scope.owner}.{name}", params)
        if params is not None else {}
    )
    return_type = node.child_by_field_name("return_type")
    if return_type is not None:
        fn.metadata["return_type"] = _text(return_type, ctx.source).lstrip(": ")
    body = node.child_by_field_name("body")
    if body is not None:
        _javascript_edges(ctx, scope, body, fn.node_id, exclude={name}, initial_locals=local_parameters)


def _enum(ctx: _Context, scope: _Scope, node: Any) -> None:
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = _text(name_node, ctx.source)
    enum_id = _node_id(ctx.path, scope.owner, name)
    enum = _add_node(ctx.nodes, ctx.edges, ctx.path, enum_id, "enum", name, node, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "enum", "qml_owner": scope.owner})
    body = node.child_by_field_name("body")
    if body is None:
        return
    for member in body.children:
        if member.type not in {"identifier", "enum_assignment"}:
            continue
        member_name = _first_identifier(member, ctx.source)
        if not member_name:
            continue
        member_id = f"{enum_id}.{member_name}"
        metadata = {"qml_kind": "enum_member", "qml_owner": scope.owner, "enum_id": enum.node_id}
        value = member.child_by_field_name("value")
        if value is not None:
            metadata["value"] = _text(value, ctx.source)
        _add_node(ctx.nodes, ctx.edges, ctx.path, member_id, "enum_member", member_name, member, ctx.source, owner_id=enum.node_id, metadata=metadata)


def _binding(ctx: _Context, scope: _Scope, name: str, value: Any, declaration: Any, *, alias: bool = False) -> None:
    if name == "id":
        raw = _text(value, ctx.source).rstrip(";").strip()
        if not raw:
            return
        id_node = _add_node(ctx.nodes, ctx.edges, ctx.path, _node_id(ctx.path, scope.owner, raw), "id", raw, declaration, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "id", "qml_owner": scope.owner, "id_name": raw})
        scope.ids[raw] = id_node.node_id
        ctx.component_ids.setdefault(raw, []).append(id_node.node_id)
        return
    if name.startswith("on") and len(name) > 2 and name[2].isupper():
        handler_id = _node_id(ctx.path, scope.owner, name)
        component_path = None
        try:
            component_path = regex_backend.resolve_qml_component(scope.type_name, ctx.known_paths)
        except Exception:
            component_path = None
        handler = _add_node(ctx.nodes, ctx.edges, ctx.path, handler_id, "func", name, declaration, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "handler", "qt": "handler", "handler_name": name, "signal_name": name[2].lower() + name[3:], "qml_owner": scope.owner, "component_path": component_path or ""})
        signal_name = handler.metadata["signal_name"]
        target = scope.signals.get(signal_name)
        if target is None and component_path and scope.parent is not None:
            target = scope.parent.signals.get(signal_name)
        if target is None:
            target = f"module:{signal_name}"
        _add_edge(ctx.edges, ctx.path, handler.node_id, target, "handles", declaration, ctx.source, signal_name=signal_name, handler_name=name, component_path=component_path or "", unverified=target.startswith("module:"))
        _javascript_edges(ctx, scope, value, handler.node_id, exclude={name})
        return
    binding_id = f"binding:{ctx.path}:{scope.owner}.{name}"
    binding = _add_node(ctx.nodes, ctx.edges, ctx.path, binding_id, "binding", name, declaration, ctx.source, owner_id=scope.node_id, metadata={"qml_kind": "binding", "qml_owner": scope.owner, "bound_name": name, "alias": alias})
    target = scope.members.get(name)
    if target is None:
        target = f"external:qml:{scope.type_name}.{name}"
        ctx.external(scope.type_name, declaration, member=name)
    _add_edge(ctx.edges, ctx.path, binding.node_id, target, "binds", declaration, ctx.source, bound_name=name)
    _javascript_edges(ctx, scope, value, binding.node_id, exclude={name})
    if alias:
        text = _text(value, ctx.source).rstrip(";").strip()
        target_alias = _resolve_text_target(ctx, scope, text, declaration)
        if target_alias:
            _add_edge(ctx.edges, ctx.path, binding.node_id, target_alias, "aliases", declaration, ctx.source, alias=True)


def _resolve_text_target(ctx: _Context, scope: _Scope, text: str, node: Any) -> str | None:
    parts = [x for x in re.split(r"\.", text.strip()) if x]
    if not parts:
        return None
    target = scope.lookup(parts[0])
    if target is None:
        id_matches = ctx.component_ids.get(parts[0], [])
        target = id_matches[0] if len(id_matches) == 1 else None
    if target is None:
        return ctx.external(parts[0], node, member=parts[-1] if len(parts) > 1 else "")
    if len(parts) == 1:
        return target
    # Convert root/id.member to the corresponding member declaration where
    # one exists. The id node is retained as the conservative fallback.
    member = parts[-1]
    id_owner = next((candidate.metadata.get("qml_owner") for candidate in ctx.nodes if candidate.node_id == target and candidate.metadata.get("qml_kind") == "id"), scope.owner)
    for candidate in ctx.nodes:
        if candidate.label == member and candidate.metadata.get("qml_owner") == id_owner and candidate.metadata.get("qml_kind") in {"property", "signal", "method"}:
            return candidate.node_id
    return target


def _identifier_candidates(node: Any) -> list[Any]:
    if node.type in _IDENT_TYPES:
        return [node]
    result: list[Any] = []
    for child in node.children:
        result.extend(_identifier_candidates(child))
    return result


def _within_field(node: Any, ancestor_type: str, field: str) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type == ancestor_type:
            field_node = parent.child_by_field_name(field)
            if field_node is not None and field_node.start_byte <= node.start_byte and node.end_byte <= field_node.end_byte:
                return True
        parent = parent.parent
    return False


def _javascript_edges(
    ctx: _Context,
    scope: _Scope,
    node: Any,
    source_id: str,
    *,
    exclude: set[str] | None = None,
    initial_locals: dict[str, str] | None = None,
) -> None:
    exclude = set(exclude or set())
    seen: set[tuple[str, str]] = set()
    local_targets: dict[str, str] = dict(initial_locals or {})
    # JavaScript locals shadow QML members. Keep them as first-class, bounded
    # declarations so a local `value` never resolves to an enclosing property.
    for declaration in _walk(node):
        if declaration.type not in {"variable_declarator", "variable_declaration", "lexical_declaration"}:
            continue
        name_node = declaration.child_by_field_name("name")
        if name_node is None:
            name_node = next((item for item in declaration.children if item.type in _IDENT_TYPES), None)
        if name_node is None:
            continue
        local_name = _text(name_node, ctx.source)
        local_id = f"symbol:{ctx.path}:{source_id.rsplit(':', 1)[-1]}.{local_name}"
        local_node = _add_node(ctx.nodes, ctx.edges, ctx.path, local_id, "variable", local_name, name_node, ctx.source, owner_id=source_id, metadata={"qml_kind": "js_local", "qml_owner": source_id})
        local_targets[local_name] = local_node.node_id
    # Member expressions get one useful edge to their base identifier and a
    # metadata member name; declarations/keys are excluded by their node
    # shape, so comments, strings and property keys cannot become references.
    for child in _walk(node):
        if child.type == "assignment_expression":
            left = child.child_by_field_name("left")
            if left is not None and left.type in {"member_expression", "optional_chain_expression"}:
                obj = left.child_by_field_name("object")
                prop = left.child_by_field_name("property")
                if obj is not None:
                    _reference(ctx, scope, obj, source_id, "writes", seen, exclude, member=_text(prop, ctx.source) if prop else "", local_targets=local_targets)
            elif left is not None:
                for ident in _identifier_candidates(left):
                    _reference(ctx, scope, ident, source_id, "writes", seen, exclude, local_targets=local_targets)
        elif child.type == "update_expression":
            arg = child.child_by_field_name("argument")
            if arg is not None and arg.type in {"member_expression", "optional_chain_expression"}:
                obj = arg.child_by_field_name("object")
                prop = arg.child_by_field_name("property")
                if obj is not None:
                    _reference(ctx, scope, obj, source_id, "writes", seen, exclude, member=_text(prop, ctx.source) if prop else "", local_targets=local_targets)
            elif arg is not None:
                for ident in _identifier_candidates(arg):
                    _reference(ctx, scope, ident, source_id, "writes", seen, exclude, local_targets=local_targets)
        elif child.type == "call_expression":
            function = child.child_by_field_name("function")
            if function is not None:
                if function.type in {"member_expression", "optional_chain_expression"}:
                    obj = function.child_by_field_name("object")
                    prop = function.child_by_field_name("property")
                    if obj is not None:
                        _reference(ctx, scope, obj, source_id, "calls", seen, exclude, member=_text(prop, ctx.source) if prop else "", local_targets=local_targets)
                else:
                    for ident in _identifier_candidates(function):
                        _reference(ctx, scope, ident, source_id, "calls", seen, exclude, local_targets=local_targets)
        elif child.type in {"member_expression", "optional_chain_expression"}:
            if _within_field(child, "assignment_expression", "left") or _within_field(child, "update_expression", "argument") or _within_field(child, "call_expression", "function"):
                continue
            obj = child.child_by_field_name("object")
            prop = child.child_by_field_name("property")
            if obj is not None:
                _reference(ctx, scope, obj, source_id, "reads", seen, exclude, member=_text(prop, ctx.source) if prop else "", local_targets=local_targets)
        elif child.type in {"identifier", "property_identifier", "variable_identifier"}:
            # A member's object/property is handled above; property identifiers
            # that are the right side of a member are declaration-like keys.
            parent = child.parent
            if (
                (parent is not None and parent.type == "pair" and parent.child_by_field_name("key") == child)
                or (parent is not None and parent.type == "variable_declarator" and parent.child_by_field_name("name") == child)
                or _within_field(child, "assignment_expression", "left")
                or _within_field(child, "update_expression", "argument")
                or _within_field(child, "call_expression", "function")
                or _within_field(child, "member_expression", "property")
            ):
                continue
            _reference(ctx, scope, child, source_id, "reads", seen, exclude, local_targets=local_targets)


def _reference(ctx: _Context, scope: _Scope, ident: Any, source_id: str, relation: str, seen: set[tuple[str, str]], exclude: set[str], *, member: str = "", local_targets: dict[str, str] | None = None) -> None:
    name = _text(ident, ctx.source)
    if not name or name in _RESERVED or name in exclude:
        return
    target = (local_targets or {}).get(name) or scope.lookup(name)
    if target is None:
        id_matches = ctx.component_ids.get(name, [])
        target = id_matches[0] if len(id_matches) == 1 else None
    if target is None and name in ctx.imports:
        uri = ctx.imports[name].get("uri", "")
        local = next((candidate for candidate in ctx.known_paths if candidate == uri or candidate.endswith("/" + uri)), None)
        target = f"file:{local}" if local else None
    if target is None:
        # Uppercase names and qualified members are retained as unverified
        # external placeholders; common JS globals are intentionally omitted.
        if name in {"console", "Math", "Qt", "JSON", "Date", "Array", "Object", "String", "Number", "Boolean"}:
            return
        target = ctx.external(name, ident, member=member)
    elif member:
        id_owner = next((candidate.metadata.get("qml_owner") for candidate in ctx.nodes if candidate.node_id == target and candidate.metadata.get("qml_kind") == "id"), None)
        if id_owner:
            member_node = next((candidate for candidate in ctx.nodes if candidate.label == member and candidate.metadata.get("qml_owner") == id_owner and candidate.metadata.get("qml_kind") in {"property", "signal", "method"}), None)
            if member_node is not None:
                target = member_node.node_id
    key = (relation, target)
    if key in seen or target == source_id:
        return
    seen.add(key)
    _add_edge(ctx.edges, ctx.path, source_id, target, relation, ident, ctx.source, member=member, unverified=target.startswith("external:"))
    if relation == "reads":
        _add_edge(ctx.edges, ctx.path, source_id, target, "references", ident, ctx.source, member=member, unverified=target.startswith("external:"))


def _walk(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _object(ctx: _Context, parent: _Scope | None, node: Any, *, root_component: bool = False, inline_name: str | None = None, binding_name: str | None = None, type_name_override: str | None = None) -> _Scope | None:
    type_name = type_name_override or _qualified_type(node, ctx.source) or "Object"
    if root_component:
        owner = PurePosixPath(ctx.path).stem
        owner_id = f"symbol:{ctx.path}:{owner}"
        metadata = {"qml_kind": "component", "qml_owner": owner, "qml_base_type": type_name, "byte_start": node.start_byte, "byte_end": node.end_byte}
        component = _add_node(ctx.nodes, ctx.edges, ctx.path, owner_id, "class", owner, node, ctx.source, metadata=metadata, signature=_signature(node, ctx.source))
        scope = _Scope(owner=owner, node_id=component.node_id, type_name=type_name, parent=None)
        ctx.root = scope
        ctx.scopes.append(scope)
        if type_name and type_name != owner:
            _add_edge(ctx.edges, ctx.path, component.node_id, f"module:{type_name}", "inherits", node, ctx.source, type_name=type_name, unverified=True)
    else:
        assert parent is not None
        owner_name = inline_name or type_name
        owner_id = ctx.object_id(parent, owner_name, node, inline=bool(inline_name))
        qml_kind = "inline_component" if inline_name else ("grouped_property" if type_name and type_name[0].islower() else "object")
        metadata = {"qml_kind": qml_kind, "qml_owner": owner_id.rsplit(":", 1)[-1], "type_name": type_name, "parent_owner": parent.node_id}
        if binding_name:
            metadata["object_on"] = binding_name
        object_node = _add_node(ctx.nodes, ctx.edges, ctx.path, owner_id, "class" if inline_name else "object", owner_name, node, ctx.source, owner_id=parent.node_id, metadata=metadata)
        scope = _Scope(owner=owner_id.rsplit(":", 1)[-1], node_id=object_node.node_id, type_name=type_name, parent=parent)
        ctx.scopes.append(scope)
        if type_name and type_name[0].isupper():
            resolved = regex_backend.resolve_qml_component(type_name, ctx.known_paths)
            if resolved and resolved != ctx.path:
                _add_edge(ctx.edges, ctx.path, ctx.file_id, f"file:{resolved}", "instantiates", node, ctx.source, type_name=type_name, component_kind="qml")
            else:
                _add_edge(ctx.edges, ctx.path, ctx.file_id, f"module:{type_name}", "instantiates", node, ctx.source, type_name=type_name, component_kind="external", unverified=True)
    initializer = node.child_by_field_name("initializer")
    if initializer is None:
        return scope
    for declaration in _declaration_children(initializer):
        if declaration.type == "ui_property":
            _property(ctx, scope, declaration)
        elif declaration.type == "ui_signal":
            _signal(ctx, scope, declaration)
        elif declaration.type in {"function_declaration", "generator_function_declaration"}:
            _function(ctx, scope, declaration)
        elif declaration.type == "enum_declaration":
            _enum(ctx, scope, declaration)
        elif declaration.type == "ui_inline_component":
            name = _first_identifier(declaration, ctx.source)
            component = next((c for c in declaration.children if c.type == "ui_object_definition"), None)
            if component is not None:
                _object(ctx, scope, component, inline_name=name)
        elif declaration.type == "ui_object_definition":
            _object(ctx, scope, declaration)
        elif declaration.type == "ui_object_definition_binding":
            initializer_node = declaration.child_by_field_name("initializer")
            child = next((c for c in _walk(initializer_node) if c.type == "ui_object_definition"), None) if initializer_node is not None else None
            if child is not None:
                bind_name = _text(declaration.child_by_field_name("name"), ctx.source) if declaration.child_by_field_name("name") is not None else _first_identifier(declaration, ctx.source)
                binding_type = _text(declaration.child_by_field_name("type_name"), ctx.source) if declaration.child_by_field_name("type_name") is not None else None
                child_scope = _object(ctx, scope, child, binding_name=bind_name, type_name_override=binding_type)
                if child_scope and bind_name:
                    target = scope.members.get(bind_name) or f"external:qml:{scope.type_name}.{bind_name}"
                    _add_edge(ctx.edges, ctx.path, child_scope.node_id, target, "binds", declaration, ctx.source, bound_name=bind_name, object_on=True)
        elif declaration.type == "ui_binding":
            name_node = declaration.child_by_field_name("name")
            value = declaration.child_by_field_name("value")
            if name_node is not None and value is not None:
                _binding(ctx, scope, _text(name_node, ctx.source), value, declaration)
    return scope


def _resolve_late_aliases(ctx: _Context) -> None:
    """Resolve aliases after all object IDs have been declared."""
    for edge in ctx.edges:
        if edge.relation != "aliases" or not edge.target.startswith("external:"):
            continue
        source_node = next((node for node in ctx.nodes if node.node_id == edge.source), None)
        if source_node is None:
            continue
        text = ""
        # Binding ids carry the owner/name suffix; match their property by
        # owner and label without relying on declaration order.
        name = str(source_node.metadata.get("bound_name", ""))
        owner = str(source_node.metadata.get("qml_owner", ""))
        prop = next((node for node in ctx.nodes if node.metadata.get("qml_kind") == "property" and node.label == name and node.metadata.get("qml_owner") == owner), None)
        if prop is not None:
            text = str(prop.metadata.get("alias_target_text", ""))
        if not text:
            continue
        parts = [part for part in text.split(".") if part]
        if not parts:
            continue
        id_matches = ctx.component_ids.get(parts[0], [])
        if len(id_matches) != 1:
            continue
        id_node = next((node for node in ctx.nodes if node.node_id == id_matches[0]), None)
        if id_node is None:
            continue
        target = id_node.node_id
        if len(parts) > 1:
            id_owner = id_node.metadata.get("qml_owner")
            member = next((node for node in ctx.nodes if node.label == parts[-1] and node.metadata.get("qml_owner") == id_owner and node.metadata.get("qml_kind") in {"property", "signal", "method"}), None)
            if member is not None:
                target = member.node_id
        edge.target = target
        edge.metadata.pop("unverified", None)


def extract_qml_edges(path: str, content: str, known_paths: set[str] | None = None, connect_names: list[str] | None = None) -> tuple[list[GraphNode], list[GraphEdge], list[str]]:
    source = content.encode("utf-8", errors="replace")
    tree = _parser().parse(source)
    root = tree.root_node
    ctx = _Context(path=path, source=source, known_paths=known_paths or set(), file_id=f"file:{path}")
    _import_declarations(ctx, root)
    root_object = root.child_by_field_name("root") or next((c for c in _walk(root) if c.type == "ui_object_definition"), None)
    if root_object is not None:
        _object(ctx, None, root_object, root_component=True)
    _resolve_late_aliases(ctx)
    diagnostics: list[str] = []
    if root.has_error:
        diagnostics.append("recoverable Tree-sitter ERROR nodes present")
    return ctx.nodes, ctx.edges, diagnostics
