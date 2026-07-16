from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ..bundle import _tokenize_query, generate_bundle
from ..gitutils import discover_repo_root
from ..impact import UnknownPathError, rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
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
            "Returns symbol references from graph edges plus repo grep, bucketed by file type. Use for cross-language blast radius; use cortex_relations for parsed-only edges. Example: {\"symbol\":\"_ensure_fresh\"}."
        ),
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
        "name": "cortex_search_text",
        "description": "Full-text body search (FTS5 BM25) across indexed file contents, with line-anchored snippets -- a grep replacement that reads from the index instead of the tree. Use for string literals, error messages, comments, or prose that cortex_search_symbols (name/signature only, not body text) can't find. Returns empty results with fts_available:false if this Python's sqlite3 build lacks FTS5. Example: {\"query\":\"device offline retry\",\"limit\":10}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
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


# P1-5: fields status carries purely to build `_meta` -- these are excluded
# from the flat top-level merge in `_format_payload`'s "detailed" branch so
# they only ever appear inside `_meta`, not duplicated at both levels.
_META_ONLY_STATUS_KEYS = {"indexed_at", "index_age_seconds"}


def _build_meta(status: dict[str, Any], *, cached: bool = False) -> dict[str, Any]:
    """Assemble the P1-5 `_meta` envelope's base fields (everything except
    `saved_tokens`, which is folded in later by `_record_tool_usage` once
    the P0-1 ledger computation is available -- see that function's
    docstring for why it isn't computed here).

    `index_age_seconds`/`indexed_at` come from `status`, which every caller
    obtains via `_ensure_fresh`/`_staleness` *at call time* -- including on
    a P1-3 cache hit, where the cached bare payload never carries its own
    copy of `status`. That's what keeps a cache hit's `_meta` honest: this
    function is called fresh, after cache retrieval, every single call.
    """
    meta: dict[str, Any] = {
        "index_age_seconds": status.get("index_age_seconds", 0),
        "indexed_at": status.get("indexed_at", 0),
        "fingerprint_fresh": not bool(status.get("stale")),
        # Always present as a bool so callers can read `_meta["cached"]`
        # unconditionally; `_meta_noteworthy` keys off its truthiness, so
        # `cached: False` still leaves a concise response non-noteworthy.
        "cached": cached,
    }
    if "auto_refreshed" in status:
        meta["auto_refreshed"] = status["auto_refreshed"]
    return meta


def _without_meta(payload: dict[str, Any]) -> dict[str, Any]:
    """Shallow-copy `payload` without its `_meta` key.

    Used everywhere a token count is meant to price the tool's actual
    retrieval DATA (P0-1's baseline/response accounting, the P1-4
    detailed-rendering baseline) -- `_meta` is bookkeeping *about* the
    response, not content an agent reads, and including it would make the
    savings numbers self-referential (a `saved_tokens` field whose own size
    nudges the very number it reports).
    """
    return {key: value for key, value in payload.items() if key != "_meta"}


def _format_payload(
    payload: dict[str, Any],
    status: dict[str, Any],
    response_format: str,
    *,
    cached: bool = False,
) -> dict[str, Any]:
    """Merge a tool's bare payload with staleness status and the P1-5
    `_meta` envelope. `_meta` is always attached here (base fields only);
    for a `concise` response with nothing noteworthy yet, `_record_tool_usage`
    strips it back out after folding in `saved_tokens` (see that function) --
    this keeps the "0 tokens added when nothing is noteworthy" contract
    without recomputing staleness/fingerprint a second time to decide.
    Always kept for `detailed` responses, per P1-5's acceptance criteria.
    """
    if response_format == "detailed":
        visible_status = {key: value for key, value in status.items() if key not in _META_ONLY_STATUS_KEYS}
        merged = {**payload, **visible_status}
    else:
        merged = {**payload, **_concise_status(status)}
    try:
        merged["_meta"] = _build_meta(status, cached=cached)
    except Exception:
        # P1-5 invariant: a failure assembling `_meta` must degrade to
        # omitting it, never to breaking the underlying tool response.
        merged.pop("_meta", None)
    return _round_floats(merged)


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
    # P1-5: a failure looking up the index timestamp must not take down
    # fingerprint-based staleness detection (which existed before this
    # feature and other logic like auto-refresh depends on) -- degrade
    # index_age_seconds/indexed_at to 0 ("unknown") instead of raising.
    try:
        indexed_at = store.get_repo_indexed_at(repo_root)
    except Exception:
        indexed_at = 0
    # index_age_seconds is derived from wall-clock time, not the
    # fingerprint, so it must be recomputed on every call (including cache
    # hits) rather than cached/frozen -- an unchanged fingerprint doesn't
    # mean an unchanged age.
    index_age_seconds = max(0, int(time.time()) - indexed_at) if indexed_at else 0
    return {
        "stale": stale,
        "fingerprint": stored or "",
        "current_fingerprint": current,
        "refresh_hint": "Call cortex_refresh to update the index." if stale else "",
        "indexed_at": indexed_at,
        "index_age_seconds": index_age_seconds,
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


def _query_cache_enabled() -> bool:
    """CORTEX_QUERY_CACHE=0 kill-switch for the P1-3 result cache -- disables
    both reads and writes, so a suspect cache can be turned off without
    restarting anything else."""
    return os.environ.get("CORTEX_QUERY_CACHE", "1") != "0"


def _cache_key(fingerprint: str, tool: str, arguments: dict[str, Any]) -> str:
    """cache_key = sha256(fingerprint + tool + canonical_json(args)) (P1-3).

    `repo_path` is excluded from the hashed args: it already selects which
    repo's cache rows a lookup can see (CortexStore.get/set_query_cache key
    on the resolved repo_path column), and its raw string form (relative
    vs. absolute, trailing slash, ...) isn't canonical -- keying on it would
    make equivalent calls miss each other for a reason unrelated to what
    they actually ask for. Every other argument is included, in particular
    `response_format`: it changes the payload shape, so concise and
    detailed calls must land on different keys.
    """
    cacheable_args = {key: value for key, value in arguments.items() if key != "repo_path"}
    canonical = json.dumps(cacheable_args, sort_keys=True, default=str)
    digest_input = f"{fingerprint}\x1e{tool}\x1e{canonical}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()


def _cache_get(store: CortexStore, repo_root: Path, cache_key: str) -> dict[str, Any] | None:
    """Read-through cache lookup. Returns the bare tool PAYLOAD (pre-status,
    pre-`_meta`) or None on a miss. Any failure (locked DB, corrupt JSON,
    ...) is treated as a miss -- a broken cache must never break a query.

    P1-5: callers must run the returned payload back through
    `_format_payload(..., cached=True)` using a *freshly computed* status
    (i.e. call `_ensure_fresh` first, as every `_call_*` already does)
    rather than reusing anything from write time -- the cache only ever
    stores payload data, never status/`_meta`, specifically so a hit can't
    replay a stale `auto_refreshed` block or a frozen index age (see
    IMPROVEMENT_PLAN.md P1-5 and the P1-3 caveat it fixes).
    """
    if not _query_cache_enabled():
        return None
    try:
        raw = store.get_query_cache(repo_root, cache_key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:
        return None


def _cache_set(store: CortexStore, repo_root: Path, cache_key: str, payload: dict[str, Any]) -> None:
    """Write-through cache store. Callers pass the bare tool PAYLOAD only --
    never a status/`_meta`-merged result -- so `_meta` can be rebuilt fresh
    on every read (see `_cache_get`). Callers must not invoke this on an
    error payload (none of the three cache-using tools do; error branches
    return before reaching their `_cache_set` call). A write failure is
    swallowed -- caching is an optimization, not a correctness requirement.
    """
    if not _query_cache_enabled():
        return
    try:
        store.set_query_cache(repo_root, cache_key, json.dumps(payload))
    except Exception:
        pass


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
    # P1-3: fingerprint the cache key on the fingerprint _ensure_fresh just
    # settled on (post auto-refresh), not the pre-refresh one -- otherwise a
    # stale-then-refreshed call would cache under a fingerprint that's
    # already wrong and never be hit again.
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    cache_key = _cache_key(status["current_fingerprint"], "cortex_query", arguments)
    cached_payload = _cache_get(store, repo_root, cache_key)
    cache_hit = cached_payload is not None
    if cache_hit:
        payload = cached_payload
    else:
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
        payload = bundle if response_format == "detailed" else _concise_query_bundle(bundle)
        # P1-5: cache the bare payload only, not status/`_meta` -- see
        # _cache_set. `_format_payload` (below) rebuilds `_meta` fresh on
        # every call, hit or miss.
        _cache_set(store, repo_root, cache_key, payload)
    return _content(_format_payload(payload, status, response_format, cached=cache_hit))


def _call_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    cache_key = _cache_key(status["current_fingerprint"], "cortex_overview", arguments)
    cached_payload = _cache_get(store, repo_root, cache_key)
    cache_hit = cached_payload is not None
    if cache_hit:
        payload = cached_payload
    else:
        payload = {"repo_path": str(repo_root), "report": generate_report(repo_root)}
        _cache_set(store, repo_root, cache_key, payload)
    return _content(_format_payload(payload, status, response_format, cached=cache_hit))


def _call_impact(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    cache_key = _cache_key(status["current_fingerprint"], "cortex_impact", arguments)
    cached_payload = _cache_get(store, repo_root, cache_key)
    if cached_payload is not None:
        return _content(_format_payload(cached_payload, status, response_format, cached=True))
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
        # Not cached: an unresolved path today may resolve after the next
        # refresh, and error responses are excluded from the cache anyway
        # (see _cache_set).
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
    payload = {"repo_path": str(repo_root), "items": items, "truncated": truncated, "returned_count": len(items)}
    _cache_set(store, repo_root, cache_key, payload)
    return _content(_format_payload(payload, status, response_format, cached=False))


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
            _format_payload(
                {
                    "error": "symbol_not_found",
                    "message": f"No symbol matched {symbol!r}.",
                    "hint": "try cortex_search_symbols",
                },
                status,
                response_format,
            ),
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
    # Symbol spans are always parsed structural code (functions/classes/etc.),
    # never markdown or plain text, so "code" is unconditionally correct here.
    if count_text_tokens(body, kind="code") > budget:
        body = truncate_text_to_budget(body, budget, kind="code")
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
    response_format = _response_format(arguments)
    return _content(_format_payload(
        {"repo_path": str(repo_root), "items": items, "truncated": truncated, "returned_count": len(items)},
        status,
        response_format,
    ))


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
    response_format = _response_format(arguments)
    return _content(_format_payload({"repo_path": str(repo_root), **result}, status, response_format))


def _call_search_text(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    query = str(arguments.get("query", ""))
    if not query:
        return _content({"error": "missing_query", "message": "query is required"}, is_error=True)
    response_format = _response_format(arguments)
    if not store.fts_enabled:
        payload = {
            "repo_path": str(repo_root),
            "items": [],
            "fts_available": False,
            "truncated": False,
            "returned_count": 0,
            "message": "FTS5 is unavailable in this Python's sqlite3 build; full-text body search is disabled.",
        }
        return _content(_format_payload(payload, status, response_format))
    limit = int(arguments.get("limit", 10))
    budget = int(arguments.get("budget", 2000))
    hits = store.search_fulltext(repo_root, query, limit=limit)
    items: list[dict[str, Any]] = []
    truncated = False
    for path, score, snippet in hits:
        item = {"path": path, "score": score, "snippet": snippet}
        if count_text_tokens(json.dumps([*items, item])) > budget:
            truncated = True
            break
        items.append(item)
    payload = {
        "repo_path": str(repo_root),
        "items": items,
        "fts_available": True,
        "truncated": truncated,
        "returned_count": len(items),
    }
    return _content(_format_payload(payload, status, response_format))


def _call_refresh(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    summary = ingest_repository(repo_root, commit_limit=int(arguments.get("commits", 50)))
    return _content({"summary": summary, "stale": False})


# Tools covered by the P0-1 token-savings ledger. cortex_refresh is excluded:
# it doesn't return retrievable content to compare against a raw-read baseline.
_LEDGER_TOOLS = {
    "cortex_query",
    "cortex_overview",
    "cortex_impact",
    "cortex_search_symbols",
    "cortex_read_symbol",
    "cortex_relations",
    "cortex_references",
    "cortex_search_text",
}

# Matches the "<label> @ <path>[:<line>]" endpoint rendering built by
# _call_relations' endpoint() closure, to recover referenced file paths from
# an already-formatted payload.
_RELATION_ENDPOINT_FILE_RE = re.compile(r" @ (.+?)(?::\d+)?$")
# Matches the "<path>:<line>" hit rendering built by references._graph_hits /
# _grep_hits, to recover referenced file paths from an already-formatted
# cortex_references payload.
_REFERENCE_LOCATION_RE = re.compile(r"^(.*):\d+$")


def _referenced_file_tokens(store: CortexStore, repo_root: Path, paths: set[str]) -> int:
    total = 0
    for path in paths:
        if not path:
            continue
        content = store.fetch_source_content(repo_root, path)
        if content:
            total += count_text_tokens(content)
    return total


def _detailed_rendering_tokens(tool: str, arguments: dict[str, Any]) -> int:
    """Re-run a structure-only tool with response_format=detailed and count it.

    Used only as an input to _estimate_baseline's policy for search/relations/
    overview (see its docstring). Re-dispatching is simple, deterministic, and
    cheap (local SQLite reads); any failure here is caught by the caller's
    non-fatal wrapper. This calls _call_* directly (not call_tool), so it
    never goes through the P0-1 ledger itself -- only the outer call does.

    P1-5: the detailed rendering always carries a `_meta` envelope, which is
    excluded here via `_without_meta` -- this baseline prices the *data*
    savings concise formatting provides, not the new metadata bookkeeping,
    and excluding it keeps the count independent of `_meta`'s own contents
    (e.g. whether saved_tokens ended up folded into a sibling call's meta).
    """
    detailed_args = {**arguments, "response_format": "detailed"}
    if tool == "cortex_search_symbols":
        detailed_result = _call_search(detailed_args)
    elif tool == "cortex_relations":
        detailed_result = _call_relations(detailed_args)
    else:
        detailed_result = _call_overview(detailed_args)
    detailed_payload = json.loads(detailed_result["content"][0]["text"])
    return count_text_tokens(json.dumps(_without_meta(detailed_payload)))


def _estimate_baseline(
    tool: str,
    arguments: dict[str, Any],
    payload: dict[str, Any],
    store: CortexStore,
    repo_root: Path,
) -> int:
    """Deterministic "what would an agent have spent without Cortex" baseline.

    Policy (kept in this one function so it stays auditable -- see P0-1 in
    IMPROVEMENT_PLAN.md):

    - File-returning tools (cortex_query, cortex_impact, cortex_read_symbol,
      cortex_references, cortex_search_text): baseline is the token cost of
      reading, in full and raw, every DISTINCT file referenced in the
      actual response -- the tokens an agent would have spent with plain
      Read/grep instead of this tool. Computed via store.fetch_source_content,
      so it reflects the exact indexed content Cortex itself read.
    - Structure-only tools (cortex_search_symbols, cortex_relations,
      cortex_overview): these return an index/graph view with no single
      "raw file" backing them, so there's no direct raw-read baseline. The
      baseline instead is the token cost of the `detailed` rendering of the
      same call -- the savings Cortex's concise response format already
      provides over its own verbose format. cortex_relations additionally
      folds in the referenced files' raw content, since each of its items
      points at a specific call site a raw-read comparison can still price.

    Caveat for reviewers: for cortex_search_symbols and cortex_overview this
    is a rough proxy on response-format savings only, not a true "agent
    avoided reading N files" figure -- there often isn't a raw-read
    equivalent for an index/graph summary. cortex_read_symbol's ambiguous
    "which symbol did you mean" response has no single resolved file either,
    so its baseline is 0 (no content was delivered to compare against).
    """
    if tool in ("cortex_query", "cortex_impact", "cortex_search_text"):
        paths = {str(item.get("path", "")) for item in payload.get("items", []) if item.get("path")}
        return _referenced_file_tokens(store, repo_root, paths)

    if tool == "cortex_read_symbol":
        path = payload.get("path")
        if not path:
            return 0
        return _referenced_file_tokens(store, repo_root, {str(path)})

    if tool == "cortex_references":
        paths: set[str] = set()
        for bucket in payload.get("items", {}).values():
            for entry in bucket:
                match = _REFERENCE_LOCATION_RE.match(str(entry))
                paths.add(match.group(1) if match else str(entry))
        return _referenced_file_tokens(store, repo_root, paths)

    if tool in ("cortex_search_symbols", "cortex_overview"):
        return _detailed_rendering_tokens(tool, arguments)

    if tool == "cortex_relations":
        detailed_tokens = _detailed_rendering_tokens(tool, arguments)
        paths = set()
        for item in payload.get("items", []):
            for key in ("source", "target"):
                match = _RELATION_ENDPOINT_FILE_RE.search(str(item.get(key, "")))
                if match:
                    paths.add(match.group(1))
        return detailed_tokens + _referenced_file_tokens(store, repo_root, paths)

    return 0


def _meta_noteworthy(meta: dict[str, Any]) -> bool:
    """Whether an already-built `_meta` dict is worth showing in a `concise`
    response (P1-5): stale index, a refresh that just ran, a cache hit, or
    positive `saved_tokens`. `detailed` responses always keep `_meta`
    regardless of this check -- see `_record_tool_usage`."""
    return (
        not meta.get("fingerprint_fresh", True)
        or bool(meta.get("auto_refreshed"))
        or bool(meta.get("cached"))
        or bool(meta.get("saved_tokens"))
    )


def _record_tool_usage(name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
    """Write one row to the P0-1 token-savings ledger, and fold the result
    into the response's `_meta` envelope (P1-5). Never raises: any failure
    degrades to skipping the ledger write and/or leaving `_meta` in its
    base (pre-savings) form, never to altering the tool's actual DATA or
    breaking the response.

    This is the single place `saved_tokens` is computed, and the number
    folded into `_meta.saved_tokens` is *exactly* `baseline_tokens -
    response_tokens` from the same `_estimate_baseline` call the ledger row
    records -- deliberately not a second, separate estimate (see P1-5 in
    IMPROVEMENT_PLAN.md: "reuse this exact computation, not a parallel
    one"). `_estimate_baseline`/`_detailed_rendering_tokens` may recurse
    into `_call_search`/`_call_relations`/`_call_overview` directly (never
    through `call_tool`), so those shadow calls never reach this function
    and never write a second ledger row for one real tool call.

    `_format_payload` always attaches a base `_meta` (index age, fingerprint
    freshness, auto_refreshed/cached when applicable) so this function never
    needs to recompute staleness from scratch just to assemble one. For a
    `concise` response where nothing turns out to be noteworthy -- not even
    the freshly computed `saved_tokens` -- `_meta` is removed here, which is
    what keeps a non-noteworthy concise response's token cost unchanged
    (P1-5's "0 tokens added" requirement). `detailed` responses always keep
    `_meta`.
    """
    if name not in _LEDGER_TOOLS:
        return
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:
        return
    meta = payload.get("_meta")
    saved_tokens: int | None = None
    if not result.get("isError"):
        try:
            repo_root = _repo_root(arguments)
            db_path = default_db_path(repo_root)
            if db_path.exists():
                store = CortexStore(db_path)
                response_tokens = count_text_tokens(json.dumps(_without_meta(payload), sort_keys=True))
                baseline_tokens = _estimate_baseline(name, arguments, payload, store, repo_root)
                store.record_tool_usage(repo_root, name, response_tokens, baseline_tokens)
                saved_tokens = baseline_tokens - response_tokens
        except Exception:
            saved_tokens = None
    if meta is None:
        return
    changed = False
    if saved_tokens is not None and saved_tokens > 0:
        meta["saved_tokens"] = saved_tokens
        changed = True
    if _response_format(arguments) == "concise" and not _meta_noteworthy(meta):
        del payload["_meta"]
        changed = True
    if changed:
        try:
            result["content"][0]["text"] = json.dumps(payload, sort_keys=True)
        except Exception:
            pass


def call_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    args = arguments or {}
    try:
        if name == "cortex_query":
            result = _call_query(args)
        elif name == "cortex_overview":
            result = _call_overview(args)
        elif name == "cortex_impact":
            result = _call_impact(args)
        elif name == "cortex_search_symbols":
            result = _call_search(args)
        elif name == "cortex_read_symbol":
            result = _call_read_symbol(args)
        elif name == "cortex_relations":
            result = _call_relations(args)
        elif name == "cortex_references":
            result = _call_references(args)
        elif name == "cortex_search_text":
            result = _call_search_text(args)
        elif name == "cortex_refresh":
            result = _call_refresh(args)
        else:
            return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    except Exception as exc:
        return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)

    _record_tool_usage(name, args, result)
    return result
