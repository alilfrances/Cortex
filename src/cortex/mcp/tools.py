from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..bundle import _tokenize_query, generate_bundle
from ..gitutils import discover_repo_root
from ..impact import UnknownPathError, rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
from ..references import find_references
from ..report import generate_report
from ..store import CortexStore, default_db_path
from ..tokenizer import count_text_tokens


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "cortex_query",
        "description": "Generate a graph-aware, token-budgeted context bundle for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "task": {"type": "string"},
                "budget": {"type": "integer", "default": 4000},
            },
            "required": ["task"],
        },
    },
    {
        "name": "cortex_overview",
        "description": "Return a Cortex graph report summary for the repository.",
        "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}}},
    },
    {
        "name": "cortex_impact",
        "description": "Find co-change and structural neighbors for a file, ranked by edge weight.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "budget": {"type": "integer", "default": 2000},
            },
            "required": ["path"],
        },
    },
    {
        "name": "cortex_search_symbols",
        "description": "Search indexed symbols by label, source path, or signature.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cortex_relations",
        "description": (
            "Query graph edges filtered by relation type (contains, imports, inherits, "
            "emits, connects, handles), symbol-granularity. Use for 'who inherits X', "
            "'who emits signal Y', 'what connects to slot Z' questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "relation": {
                    "type": "string",
                    "enum": ["contains", "imports", "inherits", "emits", "connects", "handles"],
                },
                "symbol": {"type": "string", "description": "substring match against endpoint node id or label"},
                "target": {"type": "string", "description": "alias for 'symbol'"},
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "both"},
                "limit": {"type": "integer", "default": 50},
                "budget": {"type": "integer", "default": 2000},
            },
        },
    },
    {
        "name": "cortex_references",
        "description": (
            "Blast-radius query for a symbol: union of parsed graph edges and a raw grep "
            "across the repo (honoring ingest skip-dirs), bucketed by code/script/doc/config. "
            "Use for cross-language wiring the parser misses (CMakeLists.txt, shell scripts, "
            ".qrc, JSON configs, docs)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "symbol": {"type": "string"},
                "budget": {"type": "integer", "default": 2000},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cortex_refresh",
        "description": "Re-ingest the repository into the local Cortex database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "commits": {"type": "integer", "default": 50},
            },
        },
    },
]


def _content(payload: dict[str, Any], is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "isError": is_error,
    }


def _repo_root(arguments: dict[str, Any]) -> Path:
    raw_path = arguments.get("repo_path")
    return discover_repo_root(Path(raw_path) if raw_path else Path.cwd())


def _store_or_error(repo_root: Path) -> tuple[CortexStore | None, dict[str, Any] | None]:
    db_path = default_db_path(repo_root)
    if not db_path.exists():
        return None, {
            "error": "missing_db",
            "message": "Cortex database not found. Run cortex_refresh before querying this repository.",
            "repo_path": str(repo_root),
            "hint": "Call the cortex_refresh tool or run `cortex refresh .`.",
        }
    return CortexStore(db_path), None


def _staleness(store: CortexStore, repo_root: Path) -> dict[str, Any]:
    current = compute_repo_fingerprint(repo_root)
    stored = store.get_repo_fingerprint(repo_root)
    stale = bool(stored and current != stored)
    return {
        "stale": stale,
        "fingerprint": stored or "",
        "current_fingerprint": current,
        "refresh_hint": "Call cortex_refresh to update the index." if stale else "",
    }


def _ensure_fresh(store: CortexStore, repo_root: Path) -> dict[str, Any]:
    """Auto-refresh a stale index (incremental) before answering, unless disabled."""
    status = _staleness(store, repo_root)
    if not status["stale"] or os.environ.get("CORTEX_AUTO_REFRESH", "1") == "0":
        return status
    summary = ingest_repository(repo_root, incremental=True)
    status = _staleness(store, repo_root)
    status["auto_refreshed"] = {
        key: summary[key]
        for key in ("new_files", "updated_files", "deleted_files", "unchanged_files")
    }
    return status


def _bundle_why(
    item: dict[str, Any],
    terms: set[str],
    seed_paths: set[str],
    edges: list,
) -> list[dict[str, Any]]:
    matched = sorted(term for term in terms if term in f"{item.get('path', '')}\n{item.get('content', '')}".lower())
    why: list[dict[str, Any]] = []
    if matched:
        why.append({"type": "keyword", "terms": matched[:8], "path": item.get("path", "")})

    item_node = f"file:{item.get('path', '')}"
    seed_nodes = {f"file:{path}" for path in seed_paths}
    if item.get("path") in seed_paths:
        why.append({"type": "seed_path", "path": item.get("path", "")})

    contributing_edges = []
    for edge in edges:
        if edge.source == item_node and edge.target in seed_nodes:
            seed = edge.target.removeprefix("file:")
        elif edge.target == item_node and edge.source in seed_nodes:
            seed = edge.source.removeprefix("file:")
        else:
            continue
        contributing_edges.append(
            {
                "seed_path": seed,
                "edge_id": edge.edge_id,
                "layer": edge.layer,
                "relation": edge.relation,
                "weight": edge.weight,
            }
        )
    if contributing_edges:
        why.append({"type": "graph_path", "edges": sorted(contributing_edges, key=lambda e: (-e["weight"], e["edge_id"]))[:5]})

    graph_bonus = item.get("metadata", {}).get("graph_bonus", 0.0)
    if graph_bonus:
        why.append({"type": "graph", "score": graph_bonus})
    if not why:
        why.append({"type": "rank", "reason": "selected by Cortex ranking and budget packing"})
    return why


def _call_query(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    task = str(arguments.get("task", ""))
    bundle = generate_bundle(
        repo_path=repo_root,
        task=task,
        budget=int(arguments.get("budget", 4000)),
        output_format="json",
    )
    assert isinstance(bundle, dict)
    terms = _tokenize_query(task)
    seed_paths = {
        source.path
        for source in store.fetch_sources(repo_root)
        if any(term in f"{source.path}\n{source.content}".lower() for term in terms)
    }
    _nodes, edges = store.fetch_graph(repo_root)
    for item in bundle.get("items", []):
        item["why"] = _bundle_why(item, terms, seed_paths, edges)
    bundle.update(status)
    return _content(bundle)


def _call_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    return _content({"repo_path": str(repo_root), "report": generate_report(repo_root), **status})


def _call_impact(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    nodes, edges = store.fetch_graph(repo_root)
    try:
        items, truncated = rank_file_impact(
            str(arguments.get("path", "")),
            nodes,
            edges,
            limit=int(arguments.get("limit", 10)),
            budget=int(arguments.get("budget", 2000)),
        )
    except UnknownPathError as exc:
        return _content(
            {
                "error": "unknown_path",
                "message": str(exc),
                "hint": "Path must match a file node's repo-relative path as stored by cortex_refresh.",
                **status,
            },
            is_error=True,
        )
    return _content(
        {
            "repo_path": str(repo_root),
            "items": items,
            "truncated": truncated,
            "returned_count": len(items),
            **status,
        }
    )


def _call_search(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    nodes = [node.to_dict() for node in store.search_nodes(repo_root, str(arguments.get("query", "")), int(arguments.get("limit", 20)))]
    for node in nodes:
        node["why"] = [{"type": "like_query", "query": str(arguments.get("query", ""))}]
    return _content({"repo_path": str(repo_root), "items": nodes, **status})


def _call_relations(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    edges = store.query_edges(
        repo_root,
        relation=arguments.get("relation"),
        endpoint_substr=arguments.get("symbol") or arguments.get("target"),
        direction=str(arguments.get("direction", "both")),
        limit=int(arguments.get("limit", 50)),
    )
    node_ids = sorted({edge.source for edge in edges} | {edge.target for edge in edges})
    nodes = store.get_nodes(repo_root, node_ids)

    def unresolved_endpoint(node_id: str) -> str:
        if node_id.startswith("name:"):
            return node_id.removeprefix("name:") or node_id
        if node_id.startswith("symbol:"):
            return node_id.rsplit(":", 1)[-1] or node_id
        if node_id.startswith("file:"):
            return node_id.removeprefix("file:") or node_id
        return node_id

    def endpoint(node_id: str) -> str:
        node = nodes.get(node_id)
        if node is None:
            return unresolved_endpoint(node_id)
        if node.span_start is not None:
            return f"{node.label} @ {node.source_ref}:{node.span_start}"
        return f"{node.label} @ {node.source_ref}"

    items: list[dict[str, Any]] = []
    budget = int(arguments.get("budget", 2000))
    truncated = False
    for edge in edges:
        item = {
            "relation": edge.relation,
            "source": endpoint(edge.source),
            "target": endpoint(edge.target),
        }
        if count_text_tokens(json.dumps([*items, item])) > budget:
            truncated = True
            break
        items.append(item)
    return _content(
        {
            "repo_path": str(repo_root),
            "items": items,
            "truncated": truncated,
            "returned_count": len(items),
            **status,
        }
    )


def _call_references(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    symbol = str(arguments.get("symbol", ""))
    if not symbol:
        return _content({"error": "missing_symbol", "message": "symbol is required"}, is_error=True)
    result = find_references(store, repo_root, symbol, budget=int(arguments.get("budget", 2000)))
    return _content({"repo_path": str(repo_root), **result, **status})


def _call_refresh(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    summary = ingest_repository(repo_root, commit_limit=int(arguments.get("commits", 50)))
    return _content({"summary": summary, "stale": False})


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    try:
        if name == "cortex_query":
            return _call_query(args)
        if name == "cortex_overview":
            return _call_overview(args)
        if name == "cortex_impact":
            return _call_impact(args)
        if name == "cortex_search_symbols":
            return _call_search(args)
        if name == "cortex_relations":
            return _call_relations(args)
        if name == "cortex_references":
            return _call_references(args)
        if name == "cortex_refresh":
            return _call_refresh(args)
        return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    except Exception as exc:
        return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)
