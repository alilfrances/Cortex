from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..bundle import _tokenize_query, generate_bundle
from ..gitutils import discover_repo_root
from ..impact import rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
from ..report import generate_report
from ..store import CortexStore, default_db_path


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
    bundle.update(_staleness(store, repo_root))
    return _content(bundle)


def _call_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    return _content({"repo_path": str(repo_root), "report": generate_report(repo_root), **_staleness(store, repo_root)})


def _call_impact(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    nodes, edges = store.fetch_graph(repo_root)
    items = rank_file_impact(str(arguments.get("path", "")), nodes, edges, limit=int(arguments.get("limit", 10)))
    return _content({"repo_path": str(repo_root), "items": items, **_staleness(store, repo_root)})


def _call_search(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    nodes = [node.to_dict() for node in store.search_nodes(repo_root, str(arguments.get("query", "")), int(arguments.get("limit", 20)))]
    for node in nodes:
        node["why"] = [{"type": "like_query", "query": str(arguments.get("query", ""))}]
    return _content({"repo_path": str(repo_root), "items": nodes, **_staleness(store, repo_root)})


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
        if name == "cortex_refresh":
            return _call_refresh(args)
        return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    except Exception as exc:
        return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)
