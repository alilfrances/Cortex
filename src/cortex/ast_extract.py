from __future__ import annotations

import ast
from pathlib import PurePosixPath

from .models import GraphEdge, GraphNode


def _resolve_relative_import(file_path: str, module: str, known_paths: set[str]) -> str | None:
    if not module:
        return None
    base = PurePosixPath(file_path).parent
    candidate = str(base / module.replace(".", "/")) + ".py"
    if candidate in known_paths:
        return f"file:{candidate}"
    return None


def _resolve_absolute_import(module: str, known_paths: set[str]) -> str | None:
    if not module:
        return None
    as_path = module.replace(".", "/")
    for candidate in (f"{as_path}.py", f"{as_path}/__init__.py"):
        if candidate in known_paths:
            return f"file:{candidate}"
    return None


class _Scope:
    """Static facts about the lexical scope a call site sits in (the module,
    a class body, or a function): the `self`/`cls` parameter name, the
    nearest enclosing class qualname, local names (which shadow imports) and
    the declared/constructed type of local variables."""

    __slots__ = ("self_name", "enclosing_class", "local_names", "var_types", "is_class")

    def __init__(
        self,
        self_name: str | None = None,
        enclosing_class: str | None = None,
        local_names: set[str] | None = None,
        var_types: dict[str, str] | None = None,
        is_class: bool = False,
    ) -> None:
        self.is_class = is_class
        self.self_name = self_name
        self.enclosing_class = enclosing_class
        self.local_names = local_names if local_names is not None else set()
        self.var_types = var_types if var_types is not None else {}


_RECEIVER_TEXT_LIMIT = 80
_SCOPE_BOUNDARY = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _annotation_type(node: ast.expr | None) -> str | None:
    """Bare class name of a simple annotation (`Foo`, `mod.Foo`, `"Foo"`,
    `Optional[Foo]`, `Foo | None`); None for anything not statically a name."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.strip().rsplit(".", 1)[-1]
        return text if text.isidentifier() else None
    if isinstance(node, ast.Subscript):
        base = node.value
        base_name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else None
        if base_name == "Optional":
            return _annotation_type(node.slice)
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            if isinstance(side, ast.Constant) and side.value is None:
                continue
            return _annotation_type(side)
    return None


def _attr_chain(node: ast.expr) -> list[str] | None:
    """`a.b.c` -> ["a", "b", "c"] when every link is a plain name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _constructed_type(value: ast.expr | None) -> str | None:
    """`Foo(...)` / `mod.Foo(...)` where the callee looks like a class."""
    if isinstance(value, ast.Call):
        name = value.func.id if isinstance(value.func, ast.Name) else value.func.attr if isinstance(value.func, ast.Attribute) else None
        if name and name[:1].isupper():
            return name
    return None


def _scan_locals(body: list[ast.stmt], scope: _Scope) -> None:
    """Record local names and declared/constructed variable types assigned
    directly in `body` (not inside nested def/class scopes)."""

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARY):
                scope.local_names.add(child.name)
                continue
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                scope.local_names.add(child.target.id)
                declared = _annotation_type(child.annotation) or _constructed_type(child.value)
                if declared:
                    scope.var_types[child.target.id] = declared
            elif isinstance(child, ast.Assign):
                constructed = _constructed_type(child.value)
                for target in child.targets:
                    for name in _target_names(target):
                        scope.local_names.add(name)
                        if constructed and isinstance(target, ast.Name):
                            scope.var_types[name] = constructed
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                scope.local_names.update(_target_names(child.target))
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        scope.local_names.update(_target_names(item.optional_vars))
            walk(child)

    for stmt in body:
        if isinstance(stmt, _SCOPE_BOUNDARY):
            scope.local_names.add(stmt.name)
            continue
        walk_target = ast.Module(body=[stmt], type_ignores=[])
        walk(walk_target)


def _function_scope(node: ast.FunctionDef | ast.AsyncFunctionDef, parent: _Scope, parent_is_class: bool) -> _Scope:
    scope = _Scope(
        self_name=None if parent_is_class else parent.self_name,
        enclosing_class=parent.enclosing_class,
        local_names=set(parent.local_names) if not parent_is_class else set(),
        var_types=dict(parent.var_types) if not parent_is_class else {},
    )
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    if parent_is_class and positional:
        decorators = {d.id for d in node.decorator_list if isinstance(d, ast.Name)}
        if "staticmethod" not in decorators:
            scope.self_name = positional[0].arg
    for arg in [*positional, *args.kwonlyargs, *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]:
        scope.local_names.add(arg.arg)
        scope.var_types.pop(arg.arg, None)
        declared = _annotation_type(arg.annotation)
        if declared:
            scope.var_types[arg.arg] = declared
    if scope.self_name is not None and parent_is_class:
        scope.local_names.discard(scope.self_name)
    _scan_locals(node.body, scope)
    return scope


def _module_scope(tree: ast.Module) -> _Scope:
    scope = _Scope()
    _scan_locals(tree.body, scope)
    return scope


def _class_scope(node: ast.ClassDef, qualname: str) -> _Scope:
    scope = _Scope(enclosing_class=qualname, is_class=True)
    _scan_locals(node.body, scope)
    return scope


class _ImportContext:
    """Per-file import aliases used to classify `mod.f()` / `f()` call sites.

    `modules`: local name -> absolute dotted target (`import a.b as x` -> a.b,
    `from a import b as y` -> a.b); `from_names`: local name -> (module,
    original name) for `from a import f as g`."""

    __slots__ = ("modules", "from_names")

    def __init__(self, path: str, tree: ast.AST) -> None:
        self.modules: dict[str, str] = {}
        self.from_names: dict[str, tuple[str, str]] = {}
        package = list(PurePosixPath(path).parent.parts)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.modules.setdefault(alias.asname, alias.name)
                    else:
                        root = alias.name.split(".", 1)[0]
                        self.modules.setdefault(root, root)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[: max(0, len(package) - (node.level - 1))]
                    module = ".".join([*base, *([node.module] if node.module else [])])
                else:
                    module = node.module or ""
                if not module:
                    continue
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    self.modules.setdefault(local, f"{module}.{alias.name}")
                    self.from_names.setdefault(local, (module, alias.name))


def _classify_call(call: ast.Call, scope: _Scope, imports: _ImportContext) -> dict[str, object] | None:
    """Call-site metadata for one `ast.Call` (the contract documented in
    `cortex.callresolve`): callee, receiver, receiver_kind, scope, receiver_type."""
    func = call.func
    info: dict[str, object] = {
        "callee": "",
        "receiver": None,
        "receiver_kind": "none",
        "scope": None,
        "receiver_type": None,
        "enclosing_class": scope.enclosing_class,
    }
    if isinstance(func, ast.Name):
        name = func.id
        imported = imports.from_names.get(name) if name not in scope.local_names else None
        if imported is not None:
            info["callee"] = imported[1]
            info["import_module"] = imported[0]
            if imported[1] != name:
                info["alias"] = name
        else:
            info["callee"] = name
        return info
    if not isinstance(func, ast.Attribute):
        return None
    info["callee"] = func.attr
    value = func.value
    if isinstance(value, ast.Call):
        inner = value.func
        if isinstance(inner, ast.Name) and inner.id == "super" and scope.enclosing_class:
            info.update(receiver="super()", receiver_kind="self", super=True)
            return info
        constructed = _constructed_type(value)
        text = ast.unparse(value)[:_RECEIVER_TEXT_LIMIT]
        if constructed:
            info.update(receiver=text, receiver_kind="new", receiver_type=constructed)
        else:
            info.update(receiver=text, receiver_kind="chain")
        return info
    chain = _attr_chain(value)
    if not chain:
        info.update(receiver=ast.unparse(value)[:_RECEIVER_TEXT_LIMIT], receiver_kind="chain")
        return info
    root = chain[0]
    if len(chain) == 1 and root == scope.self_name:
        info.update(receiver=root, receiver_kind="self")
    elif root in imports.modules and root not in scope.local_names and root != scope.self_name:
        dotted = ".".join([imports.modules[root], *chain[1:]])
        info.update(receiver=".".join(chain), receiver_kind="scoped", scope=".".join(chain), scope_module=dotted)
    elif len(chain) == 1:
        info.update(receiver=root, receiver_kind="var", receiver_type=scope.var_types.get(root))
    else:
        info.update(receiver=".".join(chain), receiver_kind="chain")
    return info


def _calls_in_scope(
    body: list[ast.stmt],
    scope: _Scope,
    imports: _ImportContext,
) -> list[tuple[int, dict[str, object]]]:
    """Call sites made directly in this scope's own statements, not inside a
    nested def/class (those get their own symbol and their own call edges).
    Returns (lineno, info) in first-seen order, deduped per (callee, receiver)."""
    found: list[tuple[int, dict[str, object]]] = []
    seen: set[tuple[object, ...]] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPE_BOUNDARY):
                continue  # scope boundary
            if isinstance(child, ast.Call):
                info = _classify_call(child, scope, imports)
                if info is not None and info["callee"]:
                    key = (info["callee"], info["receiver"], info["receiver_kind"], info.get("import_module"))
                    if key not in seen:
                        seen.add(key)
                        found.append((child.lineno, info))
            walk(child)

    for stmt in body:
        if isinstance(stmt, _SCOPE_BOUNDARY):
            continue
        walk(ast.Module(body=[stmt], type_ignores=[]))
    return found


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}:"


def _class_signature(node: ast.ClassDef) -> str:
    if node.bases:
        bases = ", ".join(ast.unparse(base) for base in node.bases)
        return f"class {node.name}({bases}):"
    return f"class {node.name}:"


def _call_edge(path: str, owner_id: str, owner_label: str, lineno: int, info: dict[str, object]) -> GraphEdge:
    callee = str(info["callee"])
    receiver = info["receiver"]
    site = f"{receiver}.{callee}" if receiver else callee
    return GraphEdge(
        edge_id=f"ast:{path}:calls:{owner_label}:{site}:{lineno}",
        source=owner_id,
        target=f"name:{callee}",
        relation="calls",
        layer="STRUCTURAL",
        confidence="EXTRACTED",
        weight=1.0,
        metadata={"lineno": lineno, "source_file": path, **info},
    )


def _visit_symbols(
    path: str,
    parent_id: str,
    qual_prefix: str,
    body: list[ast.stmt],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    scope: _Scope,
    imports: _ImportContext,
) -> None:
    owner_label = qual_prefix or "<module>"
    for lineno, info in _calls_in_scope(body, scope, imports):
        edges.append(_call_edge(path, parent_id, owner_label, lineno, info))
    parent_is_class = scope.is_class
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        qualname = f"{qual_prefix}.{node.name}" if qual_prefix else node.name
        symbol_id = f"symbol:{path}:{qualname}"
        if isinstance(node, ast.ClassDef):
            kind = "class"
            signature = _class_signature(node)
        else:
            kind = "func"
            signature = _function_signature(node)
        nodes.append(
            GraphNode(
                node_id=symbol_id,
                kind=kind,
                label=node.name,
                source_ref=path,
                granularity="symbol",
                signature=signature,
                span_start=node.lineno,
                span_end=node.end_lineno,
                metadata={"lineno": node.lineno},
            )
        )
        edges.append(
            GraphEdge(
                edge_id=f"ast:{path}:contains:{qualname}",
                source=parent_id,
                target=symbol_id,
                relation="contains",
                layer="STRUCTURAL",
                confidence="EXTRACTED",
                weight=1.0,
                metadata={},
            )
        )
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if isinstance(base, ast.Name):
                    edges.append(
                        GraphEdge(
                            edge_id=f"ast:{path}:inherits:{qualname}:{base.id}",
                            source=symbol_id,
                            target=f"name:{base.id}",
                            relation="inherits",
                            layer="STRUCTURAL",
                            confidence="EXTRACTED",
                            weight=1.0,
                            metadata={"lineno": node.lineno},
                        )
                    )
            child_scope = _class_scope(node, qualname)
        else:
            child_scope = _function_scope(node, scope, parent_is_class)
        _visit_symbols(path, symbol_id, qualname, node.body, nodes, edges, child_scope, imports)


def extract_python_edges(
    path: str,
    content: str,
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Parse Python file with stdlib ast. Returns ([], []) on SyntaxError."""
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return [], []

    file_node_id = f"file:{path}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target_id = _resolve_absolute_import(alias.name, known_paths) or f"module:{alias.name}"
                edges.append(
                    GraphEdge(
                        edge_id=f"ast:{path}:import:{alias.name}",
                        source=file_node_id,
                        target=target_id,
                        relation="imports",
                        layer="STRUCTURAL",
                        confidence="EXTRACTED",
                        weight=1.0,
                        metadata={"lineno": node.lineno},
                    )
                )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            level = node.level
            if level > 0:
                resolved = _resolve_relative_import(path, module, known_paths)
                target_id = resolved or f"module:{module}"
            else:
                target_id = _resolve_absolute_import(module, known_paths) or (f"module:{module}" if module else "module:unknown")

            edges.append(
                GraphEdge(
                    edge_id=f"ast:{path}:from:{module or 'unknown'}",
                    source=file_node_id,
                    target=target_id,
                    relation="imports",
                    layer="STRUCTURAL",
                    confidence="EXTRACTED",
                    weight=1.0,
                    metadata={"lineno": node.lineno, "module": module, "level": level},
                )
            )

    _visit_symbols(path, file_node_id, "", tree.body, nodes, edges, _module_scope(tree), _ImportContext(path, tree))

    return nodes, edges
