from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from ..bundle import _render_skeleton, _render_symbol_skeleton, _tokenize_query, generate_bundle
from ..deadcode import analyze_dead_code, truncate_dead_code_result
from ..gitutils import discover_repo_root
from ..impact import UnknownPathError, rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
from ..pathfind import shortest_paths
from ..references import find_references
from ..report import generate_report
from ..hotspots import top_hotspots
from ..risk import analyze_risk, truncate_risk_result
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
                "hotspot_boost": {"type": "boolean", "default": False, "description": "Opt-in churn×complexity ranking boost; default retrieval is unchanged."},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "cortex_overview",
        "description": "Returns repo graph size, communities, god nodes, top churn×complexity hotspots, and surprising links. Detailed responses also include no-network optional semantic model/index status. Use for orientation before targeted tools; not for finding one symbol. Example: {\"repo_path\":\".\"}.",
        "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}},
    },
    {
        "name": "cortex_context",
        "description": "Returns one compact triage card per path or symbol in a single call. Resolves exact file paths/node ids before symbol names, includes structural neighbors, co-change partners, hotspots, and Qt/QML wiring; optional include expansions are impact, cochange, and symbols. Use once before editing several files. Example: {\"targets\":[\"src/app.py\",\"symbol:src/app.py:run\"],\"budget\":2000}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "targets": {"type": "array", "items": {"type": "string"}, "description": "Repository-relative paths, file node ids, or symbol names/node ids."},
                "budget": {"type": "integer", "default": 2000},
                "include": {"type": "array", "items": {"type": "string", "enum": ["impact", "cochange", "symbols"]}, "description": "Optional detail expansions."},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["targets"],
        },
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
        "name": "cortex_risk",
        "description": "Analyzes a local git diff for deterministic 0–10 per-file risk and concise missing-context directives (co-change partners, tests, Qt wiring, and QML build references). No network. Defaults to HEAD~1..HEAD, or use staged=true for the index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "range": {"type": "string", "description": "Git revision range; defaults to HEAD~1..HEAD unless staged=true."},
                "staged": {"type": "boolean", "default": False},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
        },
    },
    {
        "name": "cortex_dead_code",
        "description": "Finds deterministic dead-code candidates from the persisted symbol graph plus local grep references, with conservative high/medium/low confidence tiers and Qt meta-object exclusions. No network. Use budget to cap the returned findings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
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
        "description": "Returns source for one symbol span from the index. Use after cortex_search_symbols, instead of reading a whole file. mode=\"full\" (default) returns numbered source lines; mode=\"skeleton\" returns the symbol's signature plus nested member signatures with bodies elided; mode=\"signature\" returns just the signature line and span metadata. Example: {\"symbol\":\"symbol:src/cortex/bundle.py:generate_bundle\",\"budget\":2000,\"mode\":\"skeleton\"}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "symbol": {"type": "string"},
                "mode": {"type": "string", "enum": ["full", "skeleton", "signature"], "default": "full"},
                "budget": {"type": "integer", "default": 2000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "cortex_read_file",
        "description": "Direct replacement for the built-in Read tool on an INDEXED source file. mode=\"skeleton\" (default) returns import/include lines plus every top-level symbol's signature with bodies elided -- use this instead of raw Read for orientation on a file you haven't inspected yet. mode=\"full\" returns the exact indexed file content. Falls back to full content when the file has no indexed symbols (e.g. prose). Example: {\"path\":\"src/cortex/bundle.py\",\"budget\":4000}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["skeleton", "full"], "default": "skeleton"},
                "budget": {"type": "integer", "default": 4000},
                "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"},
            },
            "required": ["path"],
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
                    "enum": ["contains", "imports", "inherits", "calls", "emits", "connects", "handles", "instantiates", "builds", "registers"],
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
        "token_stats": bundle.get("token_stats", {}),
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
            hotspot_boost=bool(arguments.get("hotspot_boost", False)),
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
        returned_tokens = int(bundle.get("total_tokens", 0))
        matched_tokens = sum(
            int(item.get("token_count", 0))
            for item in bundle.get("items", [])
            if any(entry.get("type") == "keyword" for entry in item.get("why", []))
        )
        bundle["token_stats"] = {
            "budget": int(bundle.get("budget", arguments.get("budget", 4000))),
            "returned_tokens": returned_tokens,
            "matched_tokens": matched_tokens,
            "matched_ratio": round(matched_tokens / returned_tokens, 2) if returned_tokens else 0.0,
        }
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
        # Older query-cache rows predate P1-2. Enrich them lazily rather than
        # hiding hotspots until the repository fingerprint changes.
        if "top_hotspots" not in payload:
            nodes, _edges = store.fetch_graph(repo_root)
            payload = {**payload, "top_hotspots": top_hotspots(nodes)}
            _cache_set(store, repo_root, cache_key, payload)
    else:
        nodes, _edges = store.fetch_graph(repo_root)
        payload = {
            "repo_path": str(repo_root),
            "report": generate_report(repo_root),
            "top_hotspots": top_hotspots(nodes),
        }
        _cache_set(store, repo_root, cache_key, payload)

    # P1-7 status is deliberately detailed-only: concise overview responses
    # remain byte-compatible with the stdlib/default path.  Refreshing this
    # additive field on every detailed cache hit also upgrades old cache rows
    # and reflects a newly completed setup without a repo fingerprint change.
    if response_format == "detailed":
        try:
            from ..semantic import semantic_status

            semantic_payload = semantic_status(store, repo_root)
        except Exception:
            semantic_payload = {
                "installed": False,
                "enabled": False,
                "active": False,
                "model_ready": False,
                "indexed_chunks": 0,
                "reason": "semantic status unavailable",
                "model_id": None,
                "model_version": None,
            }
        if payload.get("semantic") != semantic_payload:
            payload = {**payload, "semantic": semantic_payload}
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
        payload = cached_payload
        if any("hotspot" not in item for item in payload.get("items", [])):
            graph_nodes, _graph_edges = store.fetch_graph(repo_root)
            hotspot_by_id = {node.node_id: node.metadata.get("hotspot", {}) for node in graph_nodes}
            enriched_items = []
            for item in payload.get("items", []):
                if "hotspot" in item:
                    enriched_items.append(item)
                    continue
                raw = hotspot_by_id.get(item.get("node_id"), {})
                values = raw if isinstance(raw, dict) else {}
                churn = int(values.get("churn", 0))
                complexity = int(values.get("complexity", 0))
                enriched_items.append(
                    {**item, "hotspot": {
                        "churn": churn,
                        "complexity": complexity,
                        "score": int(values.get("score", churn * complexity)),
                    }}
                )
            payload = {**payload, "items": enriched_items}
            _cache_set(store, repo_root, cache_key, payload)
        return _content(_format_payload(payload, status, response_format, cached=True))
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


_CONTEXT_INCLUDES = ("impact", "cochange", "symbols")


def _context_resolve_file_target(
    target: str,
    repo_root: Path,
    file_nodes: dict[str, Any],
    files_by_path: dict[str, Any],
) -> Any | None:
    """Resolve a file target without letting symbol search reinterpret it.

    File paths are deliberately checked before ``_resolve_symbol``: the
    symbol search API only searches symbol-granularity rows, so a path such as
    ``src/app.py`` would otherwise become an ambiguous collection of symbols
    instead of the exact ``file:src/app.py`` node the caller supplied.
    """
    if target in file_nodes:
        return file_nodes[target]
    if target.startswith("file:") and target in file_nodes:
        return file_nodes[target]

    candidate = target.replace("\\", "/")
    if candidate.startswith("./"):
        candidate = candidate[2:]
    try:
        raw_path = Path(candidate)
        if raw_path.is_absolute():
            candidate = raw_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        # A symbol-like string, or an absolute path outside the repository, is
        # not a file target; let the normal symbol resolver handle it.
        pass
    return files_by_path.get(candidate) or file_nodes.get(f"file:{candidate}")


def _context_file_for_node(node: Any, files_by_path: dict[str, Any]) -> Any | None:
    if node is None:
        return None
    if node.kind == "file" or node.node_id.startswith("file:"):
        return node
    return files_by_path.get(node.source_ref)


def _context_span(node: Any, source: Any | None) -> dict[str, int | None]:
    if node is not None and node.granularity == "symbol":
        return {"start": node.span_start, "end": node.span_end}
    if source is not None:
        line_count = len(source.content.splitlines())
        return {"start": 1 if line_count else 0, "end": line_count}
    return {"start": node.span_start if node is not None else None, "end": node.span_end if node is not None else None}


def _context_node_ref(node: Any | None, node_id: str | None = None) -> dict[str, Any]:
    if node is None:
        return {"node_id": node_id or "", "kind": "unknown", "label": node_id or "", "path": ""}
    return {
        "node_id": node.node_id,
        "path": node.source_ref,
    }


def _context_structural_neighbors(node: Any, nodes_by_id: dict[str, Any], edges: list[Any]) -> list[dict[str, Any]]:
    candidates: dict[str, tuple[Any, str]] = {}
    for edge in edges:
        if edge.layer != "STRUCTURAL":
            continue
        if edge.source == node.node_id:
            neighbor_id, direction = edge.target, "out"
        elif edge.target == node.node_id:
            neighbor_id, direction = edge.source, "in"
        else:
            continue
        if neighbor_id == node.node_id:
            continue
        previous = candidates.get(neighbor_id)
        if previous is None or (float(edge.weight), edge.relation, edge.edge_id) > (
            float(previous[0].weight), previous[0].relation, previous[0].edge_id
        ):
            candidates[neighbor_id] = (edge, direction)

    ranked = sorted(
        candidates.items(),
        key=lambda item: (-float(item[1][0].weight), item[1][0].relation, item[0], item[1][0].edge_id),
    )
    result: list[dict[str, Any]] = []
    for neighbor_id, (edge, _direction) in ranked[:3]:
        ref = _context_node_ref(nodes_by_id.get(neighbor_id), neighbor_id)
        ref.update({"relation": edge.relation, "weight": round(float(edge.weight), 3)})
        result.append(ref)
    return result


def _context_cochange_partners(
    path: str,
    nodes: list[Any],
    edges: list[Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not path:
        return []
    cochange_edges = [
        edge for edge in edges if edge.layer == "COCHANGE" and edge.relation != "touches"
    ]
    try:
        ranked, _truncated = rank_file_impact(path, nodes, cochange_edges, limit=limit, budget=10**9)
    except UnknownPathError:
        return []
    return [
        {
            "path": item["path"],
            "weight": round(float(item.get("score", 0.0)), 3),
        }
        for item in ranked
    ]


def _context_hotspot(file_node: Any | None) -> dict[str, int]:
    values = file_node.metadata.get("hotspot", {}) if file_node is not None else {}
    if not isinstance(values, dict):
        values = {}
    churn = int(values.get("churn", 0))
    complexity = int(values.get("complexity", 0))
    return {"churn": churn, "complexity": complexity, "score": int(values.get("score", churn * complexity))}


def _context_qt_details(
    node: Any,
    file_path: str,
    nodes: list[Any],
    nodes_by_id: dict[str, Any],
    edges: list[Any],
) -> dict[str, Any]:
    """Collect compact Qt/QML details directly from the already-fetched graph."""
    source_nodes = [item for item in nodes if item.granularity == "symbol" and item.source_ref == file_path]
    signal_nodes = sorted(
        (item for item in source_nodes if item.metadata.get("qt") == "signal"),
        key=lambda item: (item.span_start or 0, item.node_id),
    )
    slot_nodes = sorted(
        (item for item in source_nodes if item.metadata.get("qt") == "slot"),
        key=lambda item: (item.span_start or 0, item.node_id),
    )
    handler_nodes = sorted(
        (item for item in source_nodes if item.metadata.get("qt") == "handler"),
        key=lambda item: (item.span_start or 0, item.node_id),
    )
    class_names = {node.label}
    if node.kind != "class":
        class_names = set()
    file_stem = Path(file_path).stem
    if node.kind == "class":
        class_names.add(file_stem)

    signal_ids = {item.node_id for item in signal_nodes}
    slot_ids = {item.node_id for item in slot_nodes}
    emits: set[str] = set()
    connects: list[dict[str, Any]] = []
    for edge in edges:
        if edge.relation == "emits":
            signal_name = str(edge.metadata.get("signal_name", ""))
            source_file = str(edge.metadata.get("source_file", ""))
            target_node = nodes_by_id.get(edge.target)
            if signal_name and (
                source_file == file_path
                or Path(source_file).stem == file_stem
                or edge.target in signal_ids
                or (target_node is not None and target_node.source_ref == file_path)
            ):
                emits.add(signal_name)
        elif edge.relation == "connects":
            sender_class = str(edge.metadata.get("sender_class", ""))
            receiver_class = str(edge.metadata.get("receiver_class", ""))
            source_node = nodes_by_id.get(edge.source)
            target_node = nodes_by_id.get(edge.target)
            relevant = bool(class_names & {sender_class, receiver_class})
            relevant = relevant or edge.source in signal_ids or edge.target in slot_ids
            relevant = relevant or (source_node is not None and source_node.source_ref == file_path)
            relevant = relevant or (target_node is not None and target_node.source_ref == file_path)
            if not relevant:
                continue
            connects.append(
                {
                    "signal": source_node.label if source_node is not None else str(edge.metadata.get("signal_name", "")),
                    "slot": target_node.label if target_node is not None else str(edge.metadata.get("slot_name", "")),
                    "source": edge.source,
                    "target": edge.target,
                }
            )
    connects.sort(key=lambda item: (str(item["signal"]), str(item["slot"]), json.dumps(item, sort_keys=True)))

    instantiates: list[dict[str, Any]] = []
    handlers = [item.label for item in handler_nodes]
    for edge in edges:
        if edge.relation != "instantiates" or edge.source != f"file:{file_path}":
            continue
        target_node = nodes_by_id.get(edge.target)
        ref = _context_node_ref(target_node, edge.target)
        if target_node is None:
            ref["label"] = str(edge.metadata.get("type_name", edge.target))
            ref["path"] = str(edge.metadata.get("component_path", ""))
        else:
            ref["label"] = target_node.label
            ref["kind"] = target_node.kind
        instantiates.append(ref)
    instantiates.sort(key=lambda item: (str(item.get("label", "")), str(item.get("node_id", ""))))

    details = {
        "signals": [item.label for item in signal_nodes],
        "slots": [item.label for item in slot_nodes],
        "emits": sorted(emits),
        "connects": connects,
        "handlers": handlers,
        "instantiates": instantiates,
    }
    has_qt_metadata = bool(node.metadata.get("qt")) or any(
        bool(item.metadata.get("qt")) for item in source_nodes
    )
    has_qt_edge = any(
        edge.relation in {"emits", "connects", "handles", "instantiates"}
        and (
            edge.metadata.get("source_file") == file_path
            or edge.source == node.node_id
            or edge.target == node.node_id
        )
        for edge in edges
    )
    if file_path.lower().endswith(".qml") or has_qt_metadata or has_qt_edge:
        # QML and actual Qt contexts keep the stable six-key shape, including
        # empty relation lists, so callers can inspect Qt cards uniformly.
        return details
    # Ordinary source and Markdown cards stay compact: do not pay for six
    # empty Qt arrays when no Qt metadata or relation exists.
    return {key: value for key, value in details.items() if value}


def _context_card(
    target: str,
    node: Any,
    source: Any | None,
    nodes: list[Any],
    nodes_by_id: dict[str, Any],
    files_by_path: dict[str, Any],
    edges: list[Any],
    includes: list[str],
) -> dict[str, Any]:
    file_node = _context_file_for_node(node, files_by_path)
    file_path = node.source_ref
    hotspot = _context_hotspot(file_node)
    span = _context_span(node, source)
    card: dict[str, Any] = {
        "target": target,
        "node_id": node.node_id,
        "kind": node.kind,
        "path": file_path,
        "span": span,
        "neighbors": _context_structural_neighbors(node, nodes_by_id, edges),
        "cochange": _context_cochange_partners(file_path, nodes, edges, limit=3),
        "hotspot": hotspot,
        "hotspot_bit": bool(hotspot.get("score", 0)),
        "truncated": False,
    }

    if node.granularity == "symbol":
        card["signature"] = node.signature or node.label
    elif source is not None and source.kind == "markdown":
        # Section graph nodes intentionally stay lightweight and do not carry
        # spans; read the already-fetched source lines here so heading order
        # remains source order even after ``# 9``/``# 10``.
        card["headings"] = [
            match.group(1).strip()
            for line in source.content.splitlines()
            if (match := re.match(r"^\s*#+\s+(.+?)\s*$", line)) is not None
        ]
    else:
        file_symbols = sorted(
            (
                candidate
                for candidate in nodes
                if candidate.granularity == "symbol"
                and candidate.source_ref == file_path
                and candidate.metadata.get("qt") != "handler"
            ),
            key=lambda item: (item.span_start or 0, item.node_id),
        )
        signature_limit = 1 if file_path.lower().endswith(".qml") else 3
        card["signatures"] = [item.signature or item.label for item in file_symbols[:signature_limit]]

    card.update(_context_qt_details(node, file_path, nodes, nodes_by_id, edges))

    if "impact" in includes:
        try:
            impact, _truncated = rank_file_impact(file_path, nodes, edges, limit=10, budget=10**9)
        except UnknownPathError:
            impact = []
        card["impact"] = impact
    if "cochange" in includes:
        # Keep the compact top-three field stable; the expansion is additive.
        card["cochange_detail"] = _context_cochange_partners(file_path, nodes, edges, limit=10)
    if "symbols" in includes:
        symbols = sorted(
            (candidate for candidate in nodes if candidate.granularity == "symbol" and candidate.source_ref == file_path),
            key=lambda item: (item.span_start or 0, item.node_id),
        )
        card["symbols"] = [_symbol_match_payload(item) for item in symbols]
    return card


def _context_problem_card(target: str, *, status: str, matches: list[Any] | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {
        "target": target,
        "node_id": None,
        "kind": None,
        "path": None,
        "span": {"start": None, "end": None},
        "status": status,
        "neighbors": [],
        "cochange": [],
        "hotspot": {"churn": 0, "complexity": 0, "score": 0},
        "hotspot_bit": False,
        "signature": "",
        "truncated": False,
    }
    if matches:
        card["hint"] = "call again with node_id"
        card["matches"] = [_symbol_match_payload(match) for match in matches]
    else:
        card["message"] = f"No target matched {target!r}."
        card["hint"] = "try cortex_search_symbols or an indexed repository-relative path"
    return card


def _context_fit_card(card: dict[str, Any], allowance: int) -> dict[str, Any]:
    """Trim optional/detail fields while keeping a card for every target."""
    if count_text_tokens(json.dumps(card, sort_keys=True)) <= allowance:
        return card
    card["truncated"] = True

    # Expansions are the first thing to trim.  The compact defaults (including
    # the three-neighbor/co-change caps and Qt fields) remain represented even
    # when a very small per-target share cannot retain every entry.
    for key in ("impact", "symbols", "cochange_detail"):
        if key in card:
            card[key] = []

    # Keep the compact Qt/QML facts ahead of optional list expansions: a
    # small budget may shorten the neighbor/co-change views, but must not
    # erase the signal/slot/handler/instantiation facts that make this tool
    # useful for Qt triage.
    list_keys = (
        "matches", "neighbors", "cochange", "cochange_detail", "signatures", "headings",
        "signals", "slots", "emits", "connects", "handlers", "instantiates",
    )
    while count_text_tokens(json.dumps(card, sort_keys=True)) > allowance:
        changed = False
        for key in list_keys:
            value = card.get(key)
            if isinstance(value, list) and value:
                value.pop()
                changed = True
                if count_text_tokens(json.dumps(card, sort_keys=True)) <= allowance:
                    break
        if changed:
            continue
        # Keep required scalar fields but shorten their human-readable parts.
        # ``target`` is the stable input-to-card mapping and must never be
        # altered, even when the requested budget is impossibly small.
        for key in ("signature", "message", "hint", "path"):
            value = card.get(key)
            if isinstance(value, str) and value:
                shortened = truncate_text_to_budget(value, max(1, allowance // 4), kind="text")
                if shortened != value:
                    card[key] = shortened
                    changed = True
                    break
        if changed:
            continue
        # At an impossibly small budget there is no way to encode every
        # required key and its JSON punctuation. Emptying variable fields is
        # the deterministic last resort; one card and its truncation signal
        # are still retained rather than silently dropping the target.
        for key in list_keys:
            if isinstance(card.get(key), list):
                card[key] = []
        for key in ("signature", "message", "hint", "path"):
            if isinstance(card.get(key), str):
                card[key] = ""
        break
    return card


def _call_context(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None

    raw_targets = arguments.get("targets")
    if not isinstance(raw_targets, list):
        return _content({"error": "missing_targets", "message": "targets must be a list of paths or symbols"}, is_error=True)
    targets = [str(target) for target in raw_targets]
    budget = max(0, int(arguments.get("budget", 2000)))
    requested_includes = arguments.get("include", [])
    if not isinstance(requested_includes, list):
        requested_includes = []
    includes = []
    for value in requested_includes:
        name = str(value)
        if name in _CONTEXT_INCLUDES and name not in includes:
            includes.append(name)
    response_format = _response_format(arguments)

    # Exactly one freshness check covers the complete batch. The graph is
    # fetched once and target source records are loaded lazily/deduplicated.
    status = _ensure_fresh(store, repo_root)
    nodes, edges = store.fetch_graph(repo_root)
    nodes_by_id = {node.node_id: node for node in nodes}
    file_nodes = {node.node_id: node for node in nodes if node.kind == "file" or node.node_id.startswith("file:")}
    files_by_path = {node.source_ref: node for node in file_nodes.values()}
    # Context cards only need source bodies for resolved target paths (Markdown
    # headings and file spans). Keep this cache local to the batch so duplicate
    # targets and a path+symbol pair perform one focused lookup, never a
    # full-corpus fetch.
    source_records: dict[str, Any | None] = {}

    def fetch_target_source(path: str) -> Any | None:
        if not path:
            return None
        if path not in source_records:
            source_records[path] = store.fetch_source_record(repo_root, path)
        return source_records[path]

    cards: list[dict[str, Any]] = []
    for target in targets:
        node = _context_resolve_file_target(target, repo_root, file_nodes, files_by_path)
        matches: list[Any] = []
        if node is None:
            # Exact node ids are resolved from the prefetched graph first; only
            # non-exact symbol names go through the established resolver.
            node = nodes_by_id.get(target)
            if node is None and "/" in target and ":" in target:
                # Accept the human-facing ``path:label`` spelling as an
                # exact symbol id as well as the stored ``symbol:path:label``
                # form, without broadening it into an ambiguous search.
                node = nodes_by_id.get(f"symbol:{target}")
        qualified_symbol = bool(
            ":" in target and re.fullmatch(r"[A-Za-z_]\w*", target.rsplit(":", 1)[-1])
        )
        if node is None and not (
            target.startswith(("file:", "symbol:"))
            or ("/" in target and not qualified_symbol)
            or (Path(target).suffix and not qualified_symbol)
        ):
            node, matches = _resolve_symbol(store, repo_root, target)
        if node is None:
            cards.append(_context_problem_card(target, status="ambiguous" if matches else "missing", matches=matches))
            continue
        # Symbol cards already carry their signature/span and graph metadata;
        # source bodies are needed only for file cards (notably Markdown
        # headings and file line spans).
        source = fetch_target_source(node.source_ref) if node.granularity != "symbol" else None
        cards.append(_context_card(target, node, source, nodes, nodes_by_id, files_by_path, edges, includes))

    if cards:
        base_share, remainder = divmod(budget, len(cards))
        for index, card in enumerate(cards):
            allowance = base_share + (1 if index < remainder else 0)
            _context_fit_card(card, max(1, allowance))
    total_tokens = count_text_tokens(json.dumps(cards, sort_keys=True))
    budget_feasible = total_tokens <= budget
    payload = {
        "repo_path": str(repo_root),
        "targets": targets,
        "budget": budget,
        "include": includes,
        "cards": cards,
        "total_tokens": total_tokens,
        "budget_feasible": budget_feasible,
    }
    if not budget_feasible:
        payload["budget_note"] = (
            "The minimum per-card mapping metadata exceeds the requested budget; "
            "all input targets are retained unchanged."
        )
    return _content(_format_payload(payload, status, response_format))


def _call_risk(arguments: dict[str, Any]) -> dict[str, Any]:
    """Run risk with one shared freshness check and no query cache."""
    repo_root = _repo_root(arguments)
    db_path = default_db_path(repo_root)
    store = CortexStore(db_path) if db_path.exists() else None
    if store is not None:
        # Exactly one freshness check for the complete risk request.  The risk
        # engine receives this graph snapshot and never performs its own check.
        status = _ensure_fresh(store, repo_root)
        nodes, edges = store.fetch_graph(repo_root)
        graph_args = {"nodes": nodes, "edges": edges}
    else:
        status = {
            "stale": False,
            "fingerprint": "",
            "current_fingerprint": compute_repo_fingerprint(repo_root),
            "refresh_hint": "Call cortex_refresh to build the index.",
            "indexed_at": 0,
            "index_age_seconds": 0,
        }
        graph_args = {"nodes": None, "edges": None}
    result = analyze_risk(
        repo_root,
        arguments.get("range"),
        staged=bool(arguments.get("staged", False)),
        db_path=db_path,
        **graph_args,
    )
    result = truncate_risk_result(result, int(arguments.get("budget", 2000)))
    response_format = _response_format(arguments)
    return _content(_format_payload(result, status, response_format), is_error=result.get("status") == "error")


def _call_dead_code(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    nodes, edges = store.fetch_graph(repo_root)
    result = analyze_dead_code(
        repo_root,
        db_path=store.db_path,
        store=store,
        nodes=nodes,
        edges=edges,
        budget=int(arguments.get("budget", 2000)),
    )
    result = truncate_dead_code_result(result, int(arguments.get("budget", 2000)))
    response_format = _response_format(arguments)
    return _content(_format_payload(result, status, response_format))


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

    # P1-6: mode="full" is the pre-existing, unmodified path below -- kept
    # byte-identical (no new "mode" key on the payload) so a caller that
    # omits `mode` sees exactly today's response.
    mode = str(arguments.get("mode", "full"))
    if mode not in ("full", "skeleton", "signature"):
        mode = "full"

    if mode == "signature":
        payload = {
            "repo_path": str(repo_root),
            "node_id": node.node_id,
            "path": node.source_ref,
            "span_start": node.span_start,
            "span_end": node.span_end,
            "signature": node.signature,
            "mode": "signature",
        }
        return _content(_format_payload(payload, status, response_format))

    content = store.fetch_source_content(repo_root, node.source_ref)
    if content is None:
        return _content({"error": "missing_source", "message": f"No stored source content for {node.source_ref}."}, is_error=True)

    budget = int(arguments.get("budget", 2000))
    if mode == "skeleton":
        # Scoped to the symbol's own children (P1-6): whole-file symbols are
        # passed so import-line detection and child discovery see the whole
        # file, but only `node`'s own entry is rendered.
        all_symbols = store.fetch_symbols_for_path(repo_root, node.source_ref)
        body = _render_symbol_skeleton(content, all_symbols, node)
        body_format = "skeleton"
    else:
        body = _numbered_span(content, node.span_start, node.span_end)
        body_format = "line_number: source"

    truncated = False
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
        "body_format": body_format,
        "body": body,
        "truncated": truncated,
    }
    if mode != "full":
        payload["mode"] = mode
    return _content(_format_payload(payload, status, response_format))


def _call_read_file(arguments: dict[str, Any]) -> dict[str, Any]:
    """P1-6: direct raw-Read replacement for an indexed file. mode="skeleton"
    (default) renders imports/includes + every top-level symbol's signature
    with bodies elided via the same `_render_skeleton` bundle packing already
    uses; mode="full" returns the exact indexed content. A file with no
    indexed symbols (prose, an unparsed language) has nothing to skeletonize,
    so skeleton mode transparently falls back to full content for it --
    `skeletonized` in the payload says which actually happened.
    """
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    path = str(arguments.get("path", ""))
    if not path:
        return _content({"error": "missing_path", "message": "path is required"}, is_error=True)
    source = store.fetch_source_record(repo_root, path)
    if source is None:
        return _content(
            _format_payload(
                {
                    "error": "missing_source",
                    "message": f"No indexed source content for {path!r}.",
                    "hint": "Path must match a file node's repo-relative path as stored by cortex_refresh. "
                    "Call cortex_search_symbols or cortex_overview to list indexed files.",
                },
                status,
                response_format,
            ),
            is_error=True,
        )

    mode = str(arguments.get("mode", "skeleton"))
    if mode not in ("skeleton", "full"):
        mode = "skeleton"
    budget = int(arguments.get("budget", 4000))

    symbols = store.fetch_symbols_for_path(repo_root, path)
    skeletonized = mode == "skeleton" and bool(symbols)
    body = _render_skeleton(source.content, symbols, set()) if skeletonized else source.content

    truncated = False
    tokens = count_text_tokens(body, kind=source.kind)
    if tokens > budget:
        body = truncate_text_to_budget(body, budget, kind=source.kind)
        truncated = True
        tokens = count_text_tokens(body, kind=source.kind)

    payload = {
        "repo_path": str(repo_root),
        "path": path,
        "kind": source.kind,
        "mode": mode,
        "skeletonized": skeletonized,
        "symbol_count": len(symbols),
        "token_count": tokens,
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
    if node_id.startswith("module:"):
        return node_id.removeprefix("module:") or node_id
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
    mode = str(arguments.get("mode", "incremental"))
    # Incremental needs an existing database to diff against.
    incremental = mode != "full" and default_db_path(repo_root).exists()
    summary = ingest_repository(
        repo_root,
        commit_limit=int(arguments.get("commits", 1000)),
        incremental=incremental,
    )
    return _content({"summary": summary, "mode": "incremental" if incremental else "full", "stale": False})


# Tools covered by the P0-1 token-savings ledger. cortex_refresh is excluded:
# it doesn't return retrievable content to compare against a raw-read baseline.
_LEDGER_TOOLS = {
    "cortex_query",
    "cortex_overview",
    "cortex_context",
    "cortex_impact",
    "cortex_search_symbols",
    "cortex_read_symbol",
    "cortex_read_file",
    "cortex_relations",
    "cortex_references",
    "cortex_search_text",
    "cortex_risk",
    "cortex_dead_code",
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

    - File-returning tools (cortex_query, cortex_context, cortex_impact,
      cortex_read_symbol, cortex_read_file, cortex_references,
      cortex_search_text, cortex_risk, cortex_dead_code): baseline is
      the token cost of reading, in full and raw, every DISTINCT file
      referenced in the actual response -- the tokens an agent would have
      spent with plain Read/grep instead of this tool. Computed via
      store.fetch_source_content, so it reflects the exact indexed content
      Cortex itself read. This applies uniformly across cortex_read_symbol's
      full/skeleton/signature modes and cortex_read_file's skeleton/full
      modes (P1-6): regardless of how much of the file the response actually
      returns, the counterfactual an agent is spared is always "open the
      whole file with Read", so the baseline is that file's full raw token
      count in every mode -- the mode only changes the numerator
      (response_tokens), which is what makes the ledger credit skeleton/
      signature reads as bigger savings than a full-span read of the same
      symbol.
    - `cortex_context` is file-returning for ledger purposes: its baseline is
      the full raw content of every distinct resolved target-card path (so a
      batch with the same file twice is priced once, and ambiguous/missing
      cards contribute zero).
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
    so its baseline is 0 (no content was delivered to compare against); the
    same applies to a cortex_read_file error payload (unindexed path).
    """
    if tool in ("cortex_query", "cortex_impact", "cortex_search_text"):
        paths = {str(item.get("path", "")) for item in payload.get("items", []) if item.get("path")}
        return _referenced_file_tokens(store, repo_root, paths)

    if tool == "cortex_context":
        paths = {
            str(card.get("path", ""))
            for card in payload.get("cards", [])
            if card.get("path")
        }
        return _referenced_file_tokens(store, repo_root, paths)

    if tool in ("cortex_read_symbol", "cortex_read_file"):
        path = payload.get("path")
        if not path:
            return 0
        return _referenced_file_tokens(store, repo_root, {str(path)})

    if tool == "cortex_references":
        paths: set[str] = set()
        for bucket in payload.get("items", {}).values():
            for entry in bucket:
                text = str(entry.get("text", "")) if isinstance(entry, dict) else str(entry)
                match = _REFERENCE_LOCATION_RE.match(text)
                paths.add(match.group(1) if match else text)
        return _referenced_file_tokens(store, repo_root, paths)

    if tool == "cortex_risk":
        paths = {str(item.get("path", "")) for item in payload.get("files", []) if item.get("path")}
        return _referenced_file_tokens(store, repo_root, paths)

    if tool == "cortex_dead_code":
        paths = {str(item.get("file", "")) for item in payload.get("findings", []) if item.get("file")}
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
        elif name == "cortex_context":
            result = _call_context(args)
        elif name == "cortex_impact":
            result = _call_impact(args)
        elif name == "cortex_search_symbols":
            result = _call_search(args)
        elif name == "cortex_read_symbol":
            result = _call_read_symbol(args)
        elif name == "cortex_read_file":
            result = _call_read_file(args)
        elif name == "cortex_relations":
            result = _call_relations(args)
        elif name == "cortex_path":
            result = _call_path(args)
        elif name == "cortex_references":
            result = _call_references(args)
        elif name == "cortex_search_text":
            result = _call_search_text(args)
        elif name == "cortex_risk":
            result = _call_risk(args)
        elif name == "cortex_dead_code":
            result = _call_dead_code(args)
        elif name == "cortex_refresh":
            result = _call_refresh(args)
        else:
            return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    except Exception as exc:
        return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)

    _record_tool_usage(name, args, result)
    return result
