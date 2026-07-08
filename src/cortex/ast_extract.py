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


def _call_name(func: ast.expr) -> str | None:
    # foo(...) -> "foo"; obj.foo(...) -> "foo" (the attribute base is dynamic;
    # like inherits targets, callees stay name-based `name:` endpoints).
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_in_scope(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """Callee names invoked directly in this function's own body, not inside a
    nested def/class (those get their own symbol and their own call edges).
    Returns (lineno, callee) in first-seen order, deduped per callee."""
    found: list[tuple[int, str]] = []
    seen: set[str] = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # scope boundary
            if isinstance(child, ast.Call):
                name = _call_name(child.func)
                if name and name not in seen:
                    seen.add(name)
                    found.append((child.lineno, name))
            walk(child)

    for stmt in func_node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        walk(stmt)
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


def _visit_symbols(
    path: str,
    parent_id: str,
    qual_prefix: str,
    body: list[ast.stmt],
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> None:
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
        else:
            for lineno, callee in _calls_in_scope(node):
                edges.append(
                    GraphEdge(
                        edge_id=f"ast:{path}:calls:{qualname}:{callee}",
                        source=symbol_id,
                        target=f"name:{callee}",
                        relation="calls",
                        layer="STRUCTURAL",
                        confidence="EXTRACTED",
                        weight=1.0,
                        metadata={"lineno": lineno},
                    )
                )
        _visit_symbols(path, symbol_id, qualname, node.body, nodes, edges)


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

    _visit_symbols(path, file_node_id, "", tree.body, nodes, edges)

    return nodes, edges
