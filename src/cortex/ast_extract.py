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
                edges.append(
                    GraphEdge(
                        edge_id=f"ast:{path}:import:{alias.name}",
                        source=file_node_id,
                        target=f"module:{alias.name}",
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
                target_id = f"module:{module}" if module else "module:unknown"

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

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_node_id = f"func:{path}:{node.name}"
            nodes.append(
                GraphNode(
                    node_id=func_node_id,
                    kind="func",
                    label=node.name,
                    source_ref=path,
                    metadata={"lineno": node.lineno},
                )
            )
            edges.append(
                GraphEdge(
                    edge_id=f"ast:{path}:contains:func:{node.name}",
                    source=file_node_id,
                    target=func_node_id,
                    relation="contains",
                    layer="STRUCTURAL",
                    confidence="EXTRACTED",
                    weight=1.0,
                    metadata={},
                )
            )

        elif isinstance(node, ast.ClassDef):
            class_node_id = f"class:{path}:{node.name}"
            nodes.append(
                GraphNode(
                    node_id=class_node_id,
                    kind="class",
                    label=node.name,
                    source_ref=path,
                    metadata={"lineno": node.lineno},
                )
            )
            edges.append(
                GraphEdge(
                    edge_id=f"ast:{path}:contains:class:{node.name}",
                    source=file_node_id,
                    target=class_node_id,
                    relation="contains",
                    layer="STRUCTURAL",
                    confidence="EXTRACTED",
                    weight=1.0,
                    metadata={},
                )
            )
            for base in node.bases:
                if isinstance(base, ast.Name):
                    edges.append(
                        GraphEdge(
                            edge_id=f"ast:{path}:inherits:{node.name}:{base.id}",
                            source=class_node_id,
                            target=f"name:{base.id}",
                            relation="inherits",
                            layer="STRUCTURAL",
                            confidence="EXTRACTED",
                            weight=1.0,
                            metadata={"lineno": node.lineno},
                        )
                    )

    return nodes, edges
