from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..bundle import _tokenize_query, generate_bundle
from ..gitutils import discover_repo_root
from ..impact import UnknownPathError, rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
from ..pathfind import shortest_paths
from ..references import find_references
from ..report import generate_report
from ..store import CortexStore, default_db_path
from ..tokenizer import count_text_tokens, truncate_text_to_budget


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "cortex_query",
        "description": "Returns ranked files/snippets for a concrete coding task. Use before grep/Read to get graph-aware context; use cortex_search_symbols for a named function first. Example: {\"task\":\"fix stale auto refresh\",\"budget\":4000}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "task": {"type": "string"},
                "budget": {"type": "integer", "default": 4000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "cortex_overview",
        "description": "Returns repo graph size, communities, god nodes, and surprising links. Use for orientation before targeted tools; not for finding one symbol. Example: {\"repo_path\":\".\"}.",
        "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}},
    },
    {
        "name": "cortex_impact",
        "description": "Returns files structurally or historically coupled to one path. Use after editing/reading a file to assess blast radius; use cortex_references for symbol wiring. Example: {\"path\":\"src/cortex/store.py\"}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "cortex_search_symbols",
        "description": "Returns matching indexed symbols without file bodies. Use when you know a function/class/identifier, then call cortex_read_symbol or cortex_impact. Example: {\"query\":\"generate bundle\"}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "cortex_read_symbol",
        "description": "Returns numbered source lines for one symbol span from the index. Use after cortex_search_symbols, instead of reading a whole file. Example: {\"symbol\":\"symbol:src/cortex/bundle.py:generate_bundle\",\"budget\":2000}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "symbol": {"type": "string"},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cortex_relations",
        "description": (
            "Returns parsed graph edges like imports/inherits/calls/emits/connects. Use for structural symbol questions; use cortex_references when configs/docs/scripts may mention it. Example: {\"relation\":\"calls\",\"symbol\":\"generate_bundle\",\"direction\":\"out\"}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "relation": {
                    "type": "string",
                    "enum": ["contains", "imports", "inherits", "calls", "emits", "connects", "handles", "instantiates"],
                },
                "symbol": {"type": "string", "description": "substring match against endpoint node id or label"},
                "target": {"type": "string", "description": "alias for 'symbol'"},
                "direction": {"type": "string", "enum": ["out", "in", "both"], "default": "both"},
                "limit": {"type": "integer", "default": 50},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
        },
    },
    {
        "name": "cortex_references",
        "description": (
            "Returns symbol references from graph edges plus repo grep, bucketed by file type. Use for cross-language blast radius; use cortex_relations for parsed-only edges. Pass mode:\"writes\" to answer where a symbol is mutated. Example: {\"symbol\":\"_ensure_fresh\",\"mode\":\"writes\"}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "symbol": {"type": "string"},
                "budget": {"type": "integer", "default": 2000},
                "mode": {"type": "string", "enum": ["all", "writes"], "default": "all"},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cortex_path",
        "description": (
            "Returns up to 3 shortest graph paths between two symbols over parsed structural edges. Use to answer how A reaches B (calls/contains/connects wiring); use cortex_relations for one-hop neighbors. Example: {\"symbol_a\":\"generate_bundle\",\"symbol_b\":\"count_text_tokens\"}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "symbol_a": {"type": "string"},
                "symbol_b": {"type": "string"},
                "max_depth": {"type": "integer", "default": 6},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["symbol_a", "symbol_b"],
        },
    },
    {
        "name": "cortex_refresh",
        "description": "Re-ingest the repository into the local Cortex database. Incremental by default; pass mode=\"full\" to rebuild from scratch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "commits": {"type": "integer", "default": 1000},
                "mode": {"type": "string", "enum": ["incremental", "full"], "default": "incremental"},
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


def _response_format(arguments: dict[str, Any]) -> str:
    value = str(arguments.get("response_format", "concise"))
    return "detailed" if value == "detailed" else "concise"


def _round_floats(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_round_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    return value


def _concise_status(status: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"stale": bool(status.get("stale"))}
    if status.get("stale") and status.get("refresh_hint"):
        payload["refresh_hint"] = status["refresh_hint"]
    if "auto_refreshed" in status:
        payload["auto_refreshed"] = status["auto_refreshed"]
    return payload


def _format_payload(payload: dict[str, Any], status: dict[str, Any], response_format: str) -> dict[str, Any]:
    if response_format == "detailed":
        return _round_floats({**payload, **status})
    return _round_floats({**payload, **_concise_status(status)})


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
    node_id = str(item.get("metadata", {}).get("node_id", ""))
    symbol_name = node_id.split(":", 2)[-1] if node_id else ""
    haystack = f"{item.get('path', '')}\n{symbol_name}\n{item.get('content', '')}".lower()
    matched = sorted(term for term in terms if term in haystack)
    why: list[dict[str, Any]] = []
    if matched:
        entry: dict[str, Any] = {"type": "keyword", "terms": matched[:8], "path": item.get("path", "")}
        if node_id:
            entry["node_id"] = node_id
        why.append(entry)

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


def _compact_why(why: list[dict[str, Any]]) -> str:
    for entry in why:
        if entry.get("type") == "keyword":
            terms = ", ".join(entry.get("terms", [])[:4])
            return f"keyword: {terms}" if terms else "keyword match"
    for entry in why:
        if entry.get("type") == "graph_path":
            edges = [edge for edge in entry.get("edges", []) if edge.get("weight", 0.0) >= 0.3]
            if edges:
                edge = edges[0]
                return f"graph: {edge.get('relation')} from {edge.get('seed_path')}"
    for entry in why:
        if entry.get("type") == "seed_path":
            return f"path: {entry.get('path')}"
    return "ranked by Cortex"


def _concise_query_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "task": bundle.get("task", ""),
        "repo_path": bundle.get("repo_path", ""),
        "budget": bundle.get("budget", 0),
        "total_tokens": bundle.get("total_tokens", 0),
        "confidence_notes": bundle.get("confidence_notes", []),
        "open_questions": bundle.get("open_questions", []),
        "items": [],
    }
    for item in bundle.get("items", []):
        compact["items"].append(
            {
                "path": item.get("path", ""),
                "kind": item.get("kind", ""),
                "score": round(float(item.get("score", 0.0)), 2),
                "token_count": item.get("token_count", 0),
                "content": item.get("content", ""),
                "why": _compact_why(item.get("why", [])),
            }
        )
    return compact


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
    response_format = _response_format(arguments)
    payload = bundle if response_format == "detailed" else _concise_query_bundle(bundle)
    return _content(_format_payload(payload, status, response_format))


def _call_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    return _content(_format_payload({"repo_path": str(repo_root), "report": generate_report(repo_root)}, status, response_format))


def _call_impact(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    nodes, edges = store.fetch_graph(repo_root)
    response_format = _response_format(arguments)
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
            _format_payload(
                {
                    "error": "unknown_path",
                    "message": str(exc),
                    "hint": "Path must match a file node's repo-relative path as stored by cortex_refresh.",
                },
                status,
                response_format,
            ),
            is_error=True,
        )
    return _content(_format_payload(
        {"repo_path": str(repo_root), "items": items, "truncated": truncated, "returned_count": len(items)},
        status,
        response_format,
    ))


def _call_search(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    nodes = [node.to_dict() for node in store.search_nodes(repo_root, str(arguments.get("query", "")), int(arguments.get("limit", 20)))]
    for node in nodes:
        node["degree"] = node.get("metadata", {}).get("degree", 0)
        node["why"] = [{"type": "like_query", "query": str(arguments.get("query", ""))}]
    response_format = _response_format(arguments)
    if response_format == "concise":
        for node in nodes:
            node.pop("why", None)
    return _content(_format_payload({"repo_path": str(repo_root), "items": nodes}, status, response_format))


def _symbol_match_payload(node: Any) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "kind": node.kind,
        "label": node.label,
        "signature": node.signature,
        "source_ref": node.source_ref,
        "span_start": node.span_start,
        "span_end": node.span_end,
    }


def _resolve_symbol(store: CortexStore, repo_root: Path, symbol: str) -> tuple[Any | None, list[Any]]:
    exact = store.get_nodes(repo_root, [symbol]).get(symbol)
    if exact is not None:
        return exact, []
    matches = store.search_nodes(repo_root, symbol, limit=20)
    exact_label = [node for node in matches if node.label == symbol]
    if len(exact_label) == 1:
        return exact_label[0], []
    if len(matches) == 1:
        return matches[0], []
    return None, matches


def _numbered_span(content: str, start: int, end: int) -> str:
    lines = content.splitlines()
    selected = lines[start - 1:end]
    return "\n".join(f"{lineno}: {line}" for lineno, line in enumerate(selected, start=start))


def _call_read_symbol(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    symbol = str(arguments.get("symbol", ""))
    if not symbol:
        return _content({"error": "missing_symbol", "message": "symbol is required"}, is_error=True)
    node, matches = _resolve_symbol(store, repo_root, symbol)
    response_format = _response_format(arguments)
    if node is None:
        if matches:
            return _content(_format_payload(
                {
                    "repo_path": str(repo_root),
                    "hint": "call again with node_id",
                    "matches": [_symbol_match_payload(match) for match in matches],
                },
                status,
                response_format,
            ))
        return _content(
            {
                "error": "symbol_not_found",
                "message": f"No symbol matched {symbol!r}.",
                "hint": "try cortex_search_symbols",
                **(status if response_format == "detailed" else _concise_status(status)),
            },
            is_error=True,
        )
    if node.span_start is None or node.span_end is None:
        return _content({"error": "missing_span", "message": f"Symbol {node.node_id} has no stored span."}, is_error=True)
    content = store.fetch_source_content(repo_root, node.source_ref)
    if content is None:
        return _content({"error": "missing_source", "message": f"No stored source content for {node.source_ref}."}, is_error=True)
    body = _numbered_span(content, node.span_start, node.span_end)
    truncated = False
    budget = int(arguments.get("budget", 2000))
    if count_text_tokens(body) > budget:
        body = truncate_text_to_budget(body, budget)
        truncated = True
    payload = {
        "repo_path": str(repo_root),
        "node_id": node.node_id,
        "path": node.source_ref,
        "span_start": node.span_start,
        "span_end": node.span_end,
        "signature": node.signature,
        "body_format": "line_number: source",
        "body": body,
        "truncated": truncated,
    }
    return _content(_format_payload(payload, status, response_format))


def _unresolved_endpoint(node_id: str) -> str:
    if node_id.startswith("name:"):
        return node_id.removeprefix("name:") or node_id
    if node_id.startswith("symbol:"):
        return node_id.rsplit(":", 1)[-1] or node_id
    if node_id.startswith("file:"):
        return node_id.removeprefix("file:") or node_id
    return node_id


def _endpoint(node_id: str, nodes: dict[str, Any]) -> str:
    node = nodes.get(node_id)
    if node is None:
        return _unresolved_endpoint(node_id)
    if node.span_start is not None:
        return f"{node.label} @ {node.source_ref}:{node.span_start}"
    return f"{node.label} @ {node.source_ref}"


_ORIGIN_PREFIXES = {
    "regex:": "regex-parser",
    "treesitter:": "treesitter-parser",
    "ts:": "treesitter-parser",
    "ast:": "ast-parser",
    "cochange:": "git-history",
    "semantic:": "llm",
}


def _edge_origin(edge: Any) -> str:
    for prefix, origin in _ORIGIN_PREFIXES.items():
        if edge.edge_id.startswith(prefix):
            return origin
    if edge.layer == "COCHANGE":
        return "git-history"
    if edge.layer == "HEADING":
        return "markdown-parser"
    return "unknown"


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

    items: list[dict[str, Any]] = []
    budget = int(arguments.get("budget", 2000))
    truncated = False
    for edge in edges:
        item = {
            "relation": edge.relation,
            "source": _endpoint(edge.source, nodes),
            "target": _endpoint(edge.target, nodes),
            "layer": edge.layer,
            "confidence": edge.confidence,
            "origin": _edge_origin(edge),
        }
        if count_text_tokens(json.dumps([*items, item])) > budget:
            truncated = True
            break
        items.append(item)
    response_format = _response_format(arguments)
    return _content(_format_payload(
        {"repo_path": str(repo_root), "items": items, "truncated": truncated, "returned_count": len(items)},
        status,
        response_format,
    ))


def _call_path(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    symbol_a = str(arguments.get("symbol_a", ""))
    symbol_b = str(arguments.get("symbol_b", ""))
    if not symbol_a or not symbol_b:
        return _content({"error": "missing_symbol", "message": "symbol_a and symbol_b are required"}, is_error=True)

    resolved: dict[str, Any] = {}
    for key, symbol in (("symbol_a", symbol_a), ("symbol_b", symbol_b)):
        node, matches = _resolve_symbol(store, repo_root, symbol)
        if node is None:
            if matches:
                return _content(_format_payload(
                    {
                        "repo_path": str(repo_root),
                        "hint": "call again with node_id",
                        "ambiguous": key,
                        "matches": [_symbol_match_payload(match) for match in matches],
                    },
                    status,
                    response_format,
                ))
            return _content(
                {
                    "error": "symbol_not_found",
                    "message": f"No symbol matched {symbol!r}.",
                    "hint": "try cortex_search_symbols",
                    **(status if response_format == "detailed" else _concise_status(status)),
                },
                is_error=True,
            )
        resolved[key] = node

    graph_nodes, graph_edges = store.fetch_graph(repo_root)
    node_by_id = {node.node_id: node for node in graph_nodes}
    edge_by_id = {edge.edge_id: edge for edge in graph_edges}
    max_depth = int(arguments.get("max_depth", 6))
    raw_paths = shortest_paths(
        graph_nodes,
        graph_edges,
        resolved["symbol_a"].node_id,
        resolved["symbol_b"].node_id,
        max_depth=max_depth,
    )
    paths = [
        [
            {
                "node": _endpoint(hop["node"], node_by_id),
                "node_id": hop["node"],
                "relation": hop["relation"],
                "direction": hop["direction"],
                "layer": hop["layer"],
                "confidence": hop["confidence"],
                "origin": _edge_origin(edge_by_id[hop["edge_id"]]),
            }
            for hop in path
        ]
        for path in raw_paths
    ]
    payload: dict[str, Any] = {
        "repo_path": str(repo_root),
        "source": _endpoint(resolved["symbol_a"].node_id, node_by_id),
        "target": _endpoint(resolved["symbol_b"].node_id, node_by_id),
        "paths": paths,
        "returned_count": len(paths),
    }
    if not paths:
        payload["note"] = f"No path between the symbols within {max_depth} hops (COCHANGE edges and commit nodes excluded)."
    return _content(_format_payload(payload, status, response_format))


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
    result = find_references(
        store,
        repo_root,
        symbol,
        budget=int(arguments.get("budget", 2000)),
        mode=str(arguments.get("mode", "all")),
    )
    response_format = _response_format(arguments)
    return _content(_format_payload({"repo_path": str(repo_root), **result}, status, response_format))


def _call_refresh(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    mode = str(arguments.get("mode", "incremental"))
    # Incremental needs an existing database to diff against.
    incremental = mode != "full" and default_db_path(repo_root).exists()
    summary = ingest_repository(
        repo_root,
        commit_limit=int(arguments.get("commits", 1000)),
        incremental=incremental,
    )
    return _content({"summary": summary, "mode": "incremental" if incremental else "full", "stale": False})


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
        if name == "cortex_read_symbol":
            return _call_read_symbol(args)
        if name == "cortex_relations":
            return _call_relations(args)
        if name == "cortex_path":
            return _call_path(args)
        if name == "cortex_references":
            return _call_references(args)
        if name == "cortex_refresh":
            return _call_refresh(args)
        return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    except Exception as exc:
        return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)
