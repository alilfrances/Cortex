from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..bundle import _render_skeleton, _render_symbol_skeleton, _tokenize_query, generate_bundle
from ..deadcode import analyze_dead_code, truncate_dead_code_result
from ..deadline import DeadlineExceeded, current_deadline, deadline_scope, resolve_budget_seconds
from ..gitutils import discover_repo_root
from ..impact import IMPACT_CACHE_VERSION, UnknownPathError, rank_file_impact
from ..ingest import compute_repo_fingerprint, ingest_repository
from ..pathfind import shortest_paths
from ..references import find_references
from ..report import build_report_data, render_report
from ..hotspots import top_hotspots
from ..risk import analyze_risk, truncate_risk_result
from ..store import GRAPH_FORMAT_VERSION, CortexStore, IngestLockTimeout, default_db_path, ingest_lock
from ..tokenizer import count_text_tokens, truncate_text_to_budget
from .status import REGISTRY


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "cortex_query",
        "description": "Ranked, graph-aware files and snippets for a concrete coding task. Use before broad search or reads.",
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
        "description": "Repository graph summary, communities, hotspots, and surprising links. Use for orientation before targeted tools.",
        "inputSchema": {"type": "object", "properties": {"repo_path": {"type": "string"}, "budget": {"type": "integer", "default": 2000}, "response_format": {"type": "string", "enum": ["concise", "detailed"], "default": "concise"}}},
    },
    {
        "name": "cortex_context",
        "description": "Compact triage cards for paths or symbols, including neighbors, co-change, hotspots, and Qt/QML wiring.",
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
        "description": "Files structurally or historically coupled to a path. Use to assess a file's blast radius.",
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
        "description": "Deterministic per-file risk for a local git diff, with missing-context guidance. Defaults to HEAD~1..HEAD.",
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
        "description": "Conservative dead-code candidates from the symbol graph and local references, with confidence tiers and Qt exclusions.",
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
        "description": "Matching indexed symbols without file bodies. Use when you know a function, class, or identifier.",
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
        "description": "Indexed source for one symbol span, available as full source, skeleton, or signature.",
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
        "description": "Indexed file content as a compact symbol skeleton or full source. Prefer it to raw reads.",
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
        "description": "Parsed structural graph edges for symbol relationships such as calls, imports, connects, reads, and writes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "relation": {
                    "type": "string",
                    "enum": ["contains", "imports", "inherits", "calls", "emits", "connects", "handles", "instantiates", "builds", "registers", "binds", "reads", "writes", "references", "aliases", "exports"],
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
        "description": "Cross-language symbol references from graph edges and repository search. Use writes mode to find mutations.",
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
        "description": "Full-text search across indexed file bodies. Use for literals, messages, comments, or prose rather than symbol names.",
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
        "description": "Up to three shortest structural graph paths between two symbols. Use to explain how one reaches another.",
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
        "description": "Refresh the local repository index incrementally, or rebuild it in full.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string"},
                "commits": {"type": "integer", "default": 1000},
                "mode": {"type": "string", "enum": ["incremental", "full"], "default": "incremental"},
            },
        },
    },
    {
        "name": "cortex_status",
        "description": (
            "Report this Cortex MCP server's in-flight requests and lifetime counters. Use it to tell "
            "whether a slow call is still making progress or the server is wedged, without attaching a "
            "debugger. Answers immediately even while another call is running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {"type": "string", "description": "Unused; accepted for call-shape consistency."}
            },
        },
    },
]

# MCP 2025-03-26+ `Tool.title` and `Tool.annotations`. Clients treat
# annotations as untrusted hints; several use `readOnlyHint` to auto-approve
# calls. Every tool except cortex_refresh only reads the repository. Read
# tools may update Cortex's own index (auto-refresh) and token ledger, but
# never the user's files or anything outside the local data dir, so they are
# read-only from the environment's point of view. Nothing reaches the
# network, so `openWorldHint` is false everywhere.
TOOL_TITLES: dict[str, str] = {
    "cortex_query": "Cortex: task context bundle",
    "cortex_overview": "Cortex: repository overview",
    "cortex_context": "Cortex: pre-edit context cards",
    "cortex_impact": "Cortex: file impact ranking",
    "cortex_risk": "Cortex: change risk review",
    "cortex_dead_code": "Cortex: dead code candidates",
    "cortex_search_symbols": "Cortex: search symbols",
    "cortex_read_symbol": "Cortex: read symbol source",
    "cortex_read_file": "Cortex: read indexed file",
    "cortex_relations": "Cortex: graph relations",
    "cortex_references": "Cortex: find references",
    "cortex_search_text": "Cortex: search indexed text",
    "cortex_path": "Cortex: path between symbols",
    "cortex_refresh": "Cortex: refresh index",
    "cortex_status": "Cortex: server status",
}
_NON_READ_ONLY_TOOLS = frozenset({"cortex_refresh"})


def _annotate_tool_definition(definition: dict[str, Any]) -> dict[str, Any]:
    name = definition["name"]
    title = TOOL_TITLES[name]
    annotations = {
        "title": title,
        "readOnlyHint": name not in _NON_READ_ONLY_TOOLS,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    rest = {key: value for key, value in definition.items() if key != "name"}
    return {"name": name, "title": title, **rest, "annotations": annotations}


TOOL_DEFINITIONS = [_annotate_tool_definition(definition) for definition in TOOL_DEFINITIONS]


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


# Fingerprint TTL memo (0.10.0). `compute_repo_fingerprint` runs `git
# ls-files` plus a stat() of every candidate file; an agent typically fires
# several Cortex calls within a second or two, so the *working-tree*
# fingerprint is memoized per repo for a short TTL. Only the disk-side value is
# cached: the stored (indexed) fingerprint is always read from the database,
# so another process's ingest is noticed immediately. Edits made inside the
# TTL window are seen by the first call after it expires.
_DEFAULT_FINGERPRINT_TTL_SECONDS = 2.0
_FINGERPRINT_MEMO: dict[str, tuple[str, float]] = {}
_FINGERPRINT_MEMO_LOCK = threading.Lock()


def fingerprint_ttl_seconds(env: dict[str, str] | None = None) -> float:
    """`CORTEX_FINGERPRINT_TTL_SECONDS` (float, default 2.0; `0` disables).
    Unparseable or non-finite values fall back to the default; negative
    values disable the memo."""
    raw = (os.environ if env is None else env).get("CORTEX_FINGERPRINT_TTL_SECONDS")
    if raw is None or not raw.strip():
        return _DEFAULT_FINGERPRINT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FINGERPRINT_TTL_SECONDS
    if value != value or value in (float("inf"), float("-inf")):
        return _DEFAULT_FINGERPRINT_TTL_SECONDS
    return max(0.0, value)


def _memo_key(repo_root: Path) -> str:
    return str(repo_root.resolve())


def _current_fingerprint(repo_root: Path, *, bypass_ttl: bool = False) -> str:
    """Working-tree fingerprint, served from the TTL memo when fresh.

    `bypass_ttl=True` always recomputes (and refreshes the memo); it is used
    for the post-lock re-check in `_ensure_fresh`, where a value memoized
    before waiting on another process's ingest must not decide whether to
    ingest again."""
    ttl = fingerprint_ttl_seconds()
    key = _memo_key(repo_root)
    if ttl > 0 and not bypass_ttl:
        with _FINGERPRINT_MEMO_LOCK:
            entry = _FINGERPRINT_MEMO.get(key)
        if entry is not None and 0 <= time.monotonic() - entry[1] < ttl:
            return entry[0]
    current = compute_repo_fingerprint(repo_root)
    if ttl > 0:
        _remember_fingerprint(repo_root, current)
    return current


def _remember_fingerprint(repo_root: Path, fingerprint: str) -> None:
    if not fingerprint or fingerprint_ttl_seconds() <= 0:
        _forget_fingerprint(repo_root)
        return
    with _FINGERPRINT_MEMO_LOCK:
        _FINGERPRINT_MEMO[_memo_key(repo_root)] = (fingerprint, time.monotonic())


def _forget_fingerprint(repo_root: Path) -> None:
    with _FINGERPRINT_MEMO_LOCK:
        _FINGERPRINT_MEMO.pop(_memo_key(repo_root), None)


def _remember_ingested_fingerprint(store: CortexStore, repo_root: Path) -> None:
    """After this process ingested, the fingerprint the ingest just stored *is*
    the working-tree fingerprint it scanned: seed the memo with it instead of
    paying for another full scan. Any failure just drops the entry."""
    try:
        _remember_fingerprint(repo_root, store.get_repo_fingerprint(repo_root))
    except Exception:
        _forget_fingerprint(repo_root)


def _staleness(store: CortexStore, repo_root: Path, *, bypass_ttl: bool = False) -> dict[str, Any]:
    current = _current_fingerprint(repo_root, bypass_ttl=bypass_ttl)
    stored = store.get_repo_fingerprint(repo_root)
    stale = bool(stored and current != stored)
    if stored and not stale:
        # An index written by an older graph format is stale even when no file
        # changed: the refresh it triggers rebuilds it in full (ingest.py).
        try:
            stale = store.get_repo_graph_version(repo_root) < GRAPH_FORMAT_VERSION
        except Exception:
            stale = False
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
    try:
        with ingest_lock(repo_root):
            # Another Cortex process (or thread) may have refreshed this repo
            # while we waited for the lock: re-check before doing the work
            # again, and only report `auto_refreshed` when *this* call ingested.
            # The re-check bypasses the fingerprint TTL memo: the value
            # memoized before the wait must not decide whether to ingest.
            status = _staleness(store, repo_root, bypass_ttl=True)
            if not status["stale"]:
                return status
            # Re-entrant: ingest_repository takes the same lock again.
            _forget_fingerprint(repo_root)
            summary = ingest_repository(repo_root, incremental=True)
            _remember_ingested_fingerprint(store, repo_root)
    except IngestLockTimeout:
        # Another process is still ingesting; answer from the current index
        # and keep reporting it as stale rather than failing the call.
        return status
    status = _staleness(store, repo_root)
    status["auto_refreshed"] = {
        key: summary[key]
        for key in ("new_files", "updated_files", "deleted_files", "unchanged_files")
    }
    if summary.get("full_reindex_reason"):
        status["auto_refreshed"]["full_reindex_reason"] = summary["full_reindex_reason"]
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
    replay a stale `auto_refreshed` block or a frozen index age.
    """
    if not _query_cache_enabled():
        return None
    try:
        raw = store.get_query_cache(repo_root, cache_key)
        if raw is None:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
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


def _query_token_stats(
    payload: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, int | float]:
    returned_tokens = max(0, int(payload.get("total_tokens", 0)))

    def keyword_match(item: dict[str, Any]) -> bool:
        why = item.get("why", [])
        if isinstance(why, str):
            return why == "keyword match" or why.startswith("keyword:")
        return isinstance(why, list) and any(
            isinstance(entry, dict) and entry.get("type") == "keyword"
            for entry in why
        )

    items = payload.get("items", [])
    if not isinstance(items, list):
        items = []
    matched_tokens = min(
        returned_tokens,
        sum(
            max(0, int(item.get("token_count", 0)))
            for item in items
            if isinstance(item, dict) and keyword_match(item)
        ),
    )
    return {
        "budget": int(payload.get("budget", arguments.get("budget", 4000))),
        "returned_tokens": returned_tokens,
        "matched_tokens": matched_tokens,
        "matched_ratio": round(matched_tokens / returned_tokens, 2) if returned_tokens else 0.0,
    }


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
        token_stats = payload.get("token_stats")
        required_stats = {"budget", "returned_tokens", "matched_tokens", "matched_ratio"}
        if not isinstance(token_stats, dict) or not required_stats.issubset(token_stats):
            payload["token_stats"] = _query_token_stats(payload, arguments)
            _cache_set(store, repo_root, cache_key, payload)
    else:
        task = str(arguments.get("task", ""))
        # Load sources and the graph once: the bundle needs them, and so does the
        # `why` annotation below.
        all_sources = store.fetch_sources(repo_root)
        graph = store.fetch_graph(repo_root)
        bundle = generate_bundle(
            repo_path=repo_root,
            task=task,
            budget=int(arguments.get("budget", 4000)),
            output_format="json",
            hotspot_boost=bool(arguments.get("hotspot_boost", False)),
            sources=all_sources,
            graph=graph,
        )
        assert isinstance(bundle, dict)
        terms = _tokenize_query(task)
        seed_paths = {
            source.path
            for source in all_sources
            if any(term in f"{source.path}\n{source.content}".lower() for term in terms)
        }
        edges = graph[1]
        for item in bundle.get("items", []):
            item["why"] = _bundle_why(item, terms, seed_paths, edges)
        bundle["token_stats"] = _query_token_stats(bundle, arguments)
        payload = bundle if response_format == "detailed" else _concise_query_bundle(bundle)
        # P1-5: cache the bare payload only, not status/`_meta` -- see
        # _cache_set. `_format_payload` (below) rebuilds `_meta` fresh on
        # every call, hit or miss.
        _cache_set(store, repo_root, cache_key, payload)
    return _content(_format_payload(payload, status, response_format, cached=cache_hit))


# This is deliberately separate from the generic query-cache format.  The
# cached value is analysis, not an MCP response, and changing this version is
# an explicit invalidation when the report-data contract changes.
_OVERVIEW_ANALYSIS_CACHE_VERSION = 1
_OVERVIEW_DEFAULT_BUDGET = 2000
_CONFIDENCE_PRIORITY = {"high": 0, "medium": 1, "low": 2}


_MIN_FINDING_TOKENS = 8

_PARTIAL_ANALYSIS_NOTE = (
    "Analysis stopped at the time budget, so dead-code findings are incomplete. "
    "Set CORTEX_TOOL_TIMEOUT_SECONDS to raise it (0 disables the budget)."
)

# Tool execution is serial, so call_tool can publish its cancel event and a
# single-slot overview-analysis memo here instead of threading them through
# every helper. Both are reset when the outer call finishes.
_current_cancel: threading.Event | None = None
_in_tool_call = False
_analysis_memo: tuple[tuple[str, str], dict[str, Any]] | None = None


def _analysis_should_stop() -> Callable[[], bool]:
    """Build the stop predicate for one analysis: true once the request is
    cancelled or the call's deadline (if any) has passed. Unlike
    `check_deadline()` this stops softly, so the caller returns a partial
    result instead of a `deadline_exceeded` error."""
    cancel = _current_cancel
    deadline = current_deadline()

    def should_stop() -> bool:
        if cancel is not None and cancel.is_set():
            return True
        return deadline is not None and deadline.expired

    return should_stop


def _overview_analysis_cache_key(fingerprint: str) -> str:
    return _cache_key(
        fingerprint,
        "cortex_overview_analysis",
        {"version": _OVERVIEW_ANALYSIS_CACHE_VERSION},
    )


def _overview_dead_code_priority(finding: dict[str, Any]) -> tuple[int, str, str, int, str]:
    """Return the public confidence/file/symbol/line presentation order."""
    confidence = str(finding.get("confidence", "")).lower()
    try:
        line = int(finding.get("line", 0))
    except (TypeError, ValueError):
        line = 0
    return (
        _CONFIDENCE_PRIORITY.get(confidence, len(_CONFIDENCE_PRIORITY)),
        str(finding.get("file", "")),
        str(finding.get("symbol", "")),
        line,
        # Only break complete ties with a stable representation.
        json.dumps(finding, sort_keys=True, default=str),
    )


def _is_overview_analysis(value: Any) -> bool:
    """Reject malformed same-version cache values and recompute fail-open."""
    if not isinstance(value, dict):
        return False
    required_types = {
        "repo_name": str,
        "repo_path": str,
        "file_count": int,
        "node_count": int,
        "edge_count": int,
        "communities": list,
        "god_nodes": list,
        "hotspots": list,
        "surprising_connections": list,
        "dead_code": list,
    }
    return all(isinstance(value.get(key), value_type) for key, value_type in required_types.items())


def _overview_payload_for_report(
    repo_root: Path,
    analysis: dict[str, Any],
    dead_code: list[dict[str, Any]],
    omitted_count: int,
    budget: int,
    *,
    budget_feasible: bool,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    report = render_report(analysis, dead_code, omitted_count=omitted_count)
    payload: dict[str, Any] = {
        "repo_path": str(repo_root),
        "report": report,
        "top_hotspots": analysis.get("hotspots", []),
        "budget": budget,
        "truncated": omitted_count > 0 or not budget_feasible,
        "dead_code_total": len(dead_code) + omitted_count,
        "dead_code_returned": len(dead_code),
        "budget_feasible": budget_feasible,
        **(extra_fields or {}),
    }
    if not budget_feasible:
        payload["budget_note"] = "Budget is too small for the stable overview sections."
    return payload


def _fit_overview_report(
    repo_root: Path,
    analysis: dict[str, Any],
    budget: int,
    *,
    extra_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fit a confidence-ordered dead-code prefix without rerunning analysis.

    The base report is intentionally never dismantled.  If that stable minimum
    cannot fit, its shape is returned with ``budget_feasible=False`` rather
    than claiming a response met a physically impossible budget.
    """
    findings = [item for item in analysis.get("dead_code", []) if isinstance(item, dict)]
    findings.sort(key=_overview_dead_code_priority)
    minimum = _overview_payload_for_report(
        repo_root, analysis, [], len(findings), budget, budget_feasible=True, extra_fields=extra_fields
    )
    if count_text_tokens(json.dumps(minimum, sort_keys=True)) > budget:
        return _overview_payload_for_report(
            repo_root, analysis, [], len(findings), budget, budget_feasible=False, extra_fields=extra_fields
        )

    # The original loop grew `selected` one finding at a time and, for EVERY
    # candidate, re-ran `render_report` over the whole analysis (god nodes,
    # hotspots, communities, surprising connections -- none of which change
    # as findings are added) and then re-serialized/re-tokenized the result,
    # breaking at the first finding that didn't fit. That is O(findings) full
    # report renders.
    #
    # `findings` is fixed, sorted, and never reordered here, and the report
    # only ever *appends* one more "- `symbol` -- ..." line per included
    # finding (see render_report's "Dead Code Candidates" section) while the
    # "N additional candidate(s) omitted" line's count only shrinks -- so the
    # rendered/serialized size is monotonically non-decreasing in the number
    # of findings included. That means the first prefix length the original
    # loop fails on (its `break` point) is exactly the largest prefix length
    # that fits, which a binary search over prefix length finds directly,
    # using the SAME `_overview_payload_for_report` + `json.dumps` +
    # `count_text_tokens` measurement the original loop used -- just O(log
    # findings) times instead of O(findings) times.
    def _fits(count: int) -> bool:
        candidate = findings[:count]
        rendered = _overview_payload_for_report(
            repo_root, analysis, candidate, len(findings) - len(candidate), budget,
            budget_feasible=True, extra_fields=extra_fields,
        )
        return count_text_tokens(json.dumps(rendered, sort_keys=True)) <= budget

    # Every included finding renders to a report line of well over eight tokens,
    # so no more than budget // 8 can ever fit; bounding the search there keeps
    # the probes small when there are tens of thousands of findings.
    lo, hi, best = 0, min(len(findings), budget // _MIN_FINDING_TOKENS + 1), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if _fits(mid):
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    selected = findings[:best]
    return _overview_payload_for_report(
        repo_root, analysis, selected, len(findings) - len(selected), budget,
        budget_feasible=True, extra_fields=extra_fields,
    )


def _overview_detailed_capabilities(store: CortexStore, repo_root: Path) -> dict[str, Any]:
    """Collect volatile detailed-only capability data; never cache it."""
    try:
        from ..semantic import semantic_status
        semantic_payload = semantic_status(store, repo_root)
    except Exception:
        semantic_payload = {
            "installed": False, "enabled": False, "active": False,
            "model_ready": False, "indexed_chunks": 0,
            "reason": "semantic status unavailable", "model_id": None, "model_version": None,
        }
    try:
        from ..runtime import public_capability
        runtime_payload = public_capability()
        runtime_payload = {**runtime_payload, "backend_counts": store.parser_backend_counts(repo_root)}
    except Exception:
        runtime_payload = {"ready": False, "degraded_reason": "runtime status unavailable"}
    return {"semantic": semantic_payload, "language_runtime": runtime_payload}


def _call_overview(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    budget = max(0, int(arguments.get("budget", _OVERVIEW_DEFAULT_BUDGET)))
    cache_key = _overview_analysis_cache_key(status["current_fingerprint"])
    cached_analysis = _cache_get(store, repo_root, cache_key)
    cache_hit = (
        isinstance(cached_analysis, dict)
        and cached_analysis.get("version") == _OVERVIEW_ANALYSIS_CACHE_VERSION
        and _is_overview_analysis(cached_analysis.get("analysis"))
    )
    global _analysis_memo
    if cache_hit:
        analysis = cached_analysis["analysis"]
    else:
        memo_key = (str(repo_root.resolve()), status["current_fingerprint"])
        if _analysis_memo is not None and _analysis_memo[0] == memo_key:
            analysis = _analysis_memo[1]
        else:
            analysis = build_report_data(repo_root, should_stop=_analysis_should_stop())
            if _in_tool_call:
                _analysis_memo = (memo_key, analysis)
            if not analysis.get("dead_code_partial", False):
                _cache_set(
                    store, repo_root, cache_key, {"version": _OVERVIEW_ANALYSIS_CACHE_VERSION, "analysis": analysis}
                )

    # Capabilities describe current optional integrations, unlike graph
    # analysis. They count toward detailed content, while freshness/status
    # remains outside the budget and is merged only by `_format_payload`.
    extra_fields = _overview_detailed_capabilities(store, repo_root) if response_format == "detailed" else None
    if analysis.get("dead_code_partial", False):
        extra_fields = {**(extra_fields or {}), "dead_code_partial": True, "note": _PARTIAL_ANALYSIS_NOTE}
    payload = _fit_overview_report(repo_root, analysis, budget, extra_fields=extra_fields)
    return _content(_format_payload(payload, status, response_format, cached=cache_hit))


def _call_impact(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    response_format = _response_format(arguments)
    # The version is hashed with the arguments so rows written before symbol
    # edges were aggregated (unversioned) can never be served again.
    cache_key = _cache_key(
        status["current_fingerprint"], "cortex_impact", {**arguments, "impact_v": IMPACT_CACHE_VERSION}
    )
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


def _context_call_counts(node: Any, edges: list[Any]) -> dict[str, int]:
    """Distinct resolved callers/callees of a symbol (unresolved ``name:`` excluded)."""
    callers: set[str] = set()
    callees: set[str] = set()
    for edge in edges:
        if edge.relation != "calls" or edge.layer != "STRUCTURAL":
            continue
        if edge.target == node.node_id and edge.source != node.node_id:
            callers.add(edge.source)
        elif edge.source == node.node_id and edge.target != node.node_id and not edge.target.startswith("name:"):
            callees.add(edge.target)
    return {"callers": len(callers), "callees": len(callees)}


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

    qml_nodes = [item for item in source_nodes if item.metadata.get("qml_kind")]
    properties = sorted(item.label for item in qml_nodes if item.metadata.get("qml_kind") == "property")
    ids = sorted(item.label for item in qml_nodes if item.metadata.get("qml_kind") == "id")
    methods = sorted(item.label for item in qml_nodes if item.metadata.get("qml_kind") in {"method", "invokable"})
    enums = sorted(item.label for item in qml_nodes if item.metadata.get("qml_kind") in {"enum", "enum_member"})
    inline_components = sorted(item.label for item in qml_nodes if item.metadata.get("qml_kind") == "inline_component")
    bindings = []
    for edge in edges:
        if edge.relation not in {"binds", "aliases"} or edge.metadata.get("source_file") != file_path:
            continue
        bindings.append({"relation": edge.relation, "source": edge.source, "target": edge.target, "name": edge.metadata.get("bound_name", "")})
    bindings.sort(key=lambda item: (str(item["name"]), str(item["relation"]), str(item["source"])))
    imports = sorted({str(edge.metadata.get("uri", edge.target.removeprefix("module:"))) for edge in edges if edge.relation == "imports" and edge.metadata.get("source_file") == file_path})
    details = {
        "signals": [item.label for item in signal_nodes],
        "slots": [item.label for item in slot_nodes],
        "emits": sorted(emits),
        "connects": connects,
        "handlers": handlers,
        "instantiates": instantiates,
        "properties": properties,
        "ids": ids,
        "methods": methods,
        "enums": enums,
        "inline_components": inline_components,
        "bindings": bindings,
        "imports": imports,
        "modules": imports,
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
    response_format: str = "concise",
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
    if response_format == "detailed" and node.granularity == "symbol":
        # Detailed only: in concise form the counts would cost ~12 tokens per
        # card, over the 10-token allowance; ``neighbors`` already shows the
        # strongest caller/callee there.
        call_counts = _context_call_counts(node, edges)
        if call_counts["callers"] or call_counts["callees"]:
            card["calls"] = call_counts

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

    # The original `while` loop popped ONE item (from the end) off the first
    # non-empty list in `list_keys`, in round-robin order across repeated
    # passes, re-serializing/re-tokenizing the WHOLE card after every single
    # pop and stopping the instant it fit. That pop *order* depends only on
    # each list's starting length and `list_keys`'s fixed order -- never on
    # `allowance` or on measurement -- so it is entirely deterministic and
    # can be computed once, with no serialization at all (`_pop_counts`
    # below). And because every pop only ever removes content, json.dumps(card)
    # after `m` pops is monotonically non-increasing in `m`. So the smallest
    # `m` that fits (what the original loop's incremental checks converge
    # on) is found directly with a binary search over `m`, measured with the
    # SAME `count_text_tokens(json.dumps(card, sort_keys=True))` call the
    # original used -- O(log total_items) full-card serializations instead
    # of O(total_items).
    remaining = {key: len(card[key]) for key in list_keys if isinstance(card.get(key), list) and card[key]}
    total_items = sum(remaining.values())

    def _pop_counts(pops: int) -> dict[str, int]:
        """How many items are popped from each key within the first `pops`
        steps of the round-robin sequence (successive rounds over
        `list_keys`, skipping keys already exhausted) -- the same order the
        original nested while/for loop walked, computed analytically."""
        counts = {key: 0 for key in remaining}
        left = dict(remaining)
        done = 0
        while done < pops:
            progressed = False
            for key in list_keys:
                if done >= pops:
                    break
                if left.get(key, 0) > 0:
                    left[key] -= 1
                    counts[key] += 1
                    done += 1
                    progressed = True
            if not progressed:
                break
        return counts

    def _card_after_pops(pops: int) -> dict[str, Any]:
        popped = _pop_counts(pops)
        if not any(popped.values()):
            return card
        trial = dict(card)
        for key, count in popped.items():
            if count:
                trial[key] = card[key][: len(card[key]) - count]
        return trial

    if total_items:
        if count_text_tokens(json.dumps(_card_after_pops(total_items), sort_keys=True)) <= allowance:
            lo, hi, best = 0, total_items, total_items
            while lo <= hi:
                mid = (lo + hi) // 2
                if count_text_tokens(json.dumps(_card_after_pops(mid), sort_keys=True)) <= allowance:
                    best = mid
                    hi = mid - 1
                else:
                    lo = mid + 1
            popped = _pop_counts(best)
            for key, count in popped.items():
                if count:
                    card[key] = card[key][: len(card[key]) - count]
        else:
            # Even emptying every list in `list_keys` doesn't fit -- matches
            # the original loop running every round to exhaustion (all pops
            # applied) before it ever reaches the scalar-trimming phase.
            for key in remaining:
                card[key] = []

    if count_text_tokens(json.dumps(card, sort_keys=True)) > allowance:
        # Keep required scalar fields but shorten their human-readable parts.
        # ``target`` is the stable input-to-card mapping and must never be
        # altered, even when the requested budget is impossibly small.
        #
        # Bounded to exactly 4 fields (not input-sized), so -- unlike the
        # list-popping phase above -- checking after each one is already
        # O(1) and needs no further optimization; this loop is unchanged
        # from the original, just no longer re-entered via an outer `while`.
        for key in ("signature", "message", "hint", "path"):
            value = card.get(key)
            if isinstance(value, str) and value:
                shortened = truncate_text_to_budget(value, max(1, allowance // 4), kind="text")
                if shortened != value:
                    card[key] = shortened
                    if count_text_tokens(json.dumps(card, sort_keys=True)) <= allowance:
                        break

    if count_text_tokens(json.dumps(card, sort_keys=True)) > allowance:
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
        cards.append(_context_card(target, node, source, nodes, nodes_by_id, files_by_path, edges, includes, response_format))

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


_DEAD_CODE_ANALYSIS_CACHE_VERSION = 1


def _dead_code_analysis(store: CortexStore, repo_root: Path, fingerprint: str, budget: int) -> dict[str, Any]:
    """Dead-code findings for the current index, reusing earlier work.

    The overview analysis already runs `analyze_dead_code` at the default
    reference budget and caches it under the same fingerprint, so a dead-code
    call after an overview (or a repeat call) costs nothing. A partial result
    is never cached.
    """
    reference_budget = max(2000, budget)
    if reference_budget == 2000:
        cached = _cache_get(store, repo_root, _overview_analysis_cache_key(fingerprint))
        if (
            isinstance(cached, dict)
            and cached.get("version") == _OVERVIEW_ANALYSIS_CACHE_VERSION
            and _is_overview_analysis(cached.get("analysis"))
            and not cached["analysis"].get("dead_code_partial", False)
        ):
            return {"repo_path": str(repo_root), "findings": list(cached["analysis"]["dead_code"]), "partial": False}
    key = _cache_key(
        fingerprint, "cortex_dead_code_analysis", {"budget": reference_budget, "v": _DEAD_CODE_ANALYSIS_CACHE_VERSION}
    )
    cached = _cache_get(store, repo_root, key)
    if isinstance(cached, dict) and isinstance(cached.get("findings"), list):
        return {"repo_path": str(repo_root), "findings": cached["findings"], "partial": False}
    nodes, edges = store.fetch_graph(repo_root)
    result = analyze_dead_code(
        repo_root,
        db_path=store.db_path,
        store=store,
        nodes=nodes,
        edges=edges,
        budget=budget,
        should_stop=_analysis_should_stop(),
    )
    if not result.get("partial"):
        _cache_set(store, repo_root, key, {"findings": result["findings"]})
    return result


def _call_dead_code(arguments: dict[str, Any]) -> dict[str, Any]:
    repo_root = _repo_root(arguments)
    store, error = _store_or_error(repo_root)
    if error is not None:
        return _content(error, is_error=True)
    assert store is not None
    status = _ensure_fresh(store, repo_root)
    budget = int(arguments.get("budget", 2000))
    result = _dead_code_analysis(store, repo_root, status["current_fingerprint"], budget)
    result = truncate_dead_code_result(result, budget)
    if result.get("partial"):
        result["note"] = _PARTIAL_ANALYSIS_NOTE
    else:
        result.pop("partial", None)
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


def _prefix_fit_items(all_items: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], bool]:
    """Binary-search the largest prefix of `all_items` (already built, in a
    fixed order) whose `json.dumps` fits `budget`.

    Used by `_call_relations` and `_call_search_text`, which both used to
    grow a result list one item at a time and, for EVERY candidate item,
    rebuild and re-serialize/re-tokenize the WHOLE accumulated list via
    `count_text_tokens(json.dumps([*items, item]))`, stopping (without
    adding it) at the first item that didn't fit -- an O(n^2) pattern in the
    number of returned items.

    Because `all_items` is fixed and never reordered, `json.dumps(all_items[:k])`
    is monotonically non-decreasing in `k` (every larger `k` only appends
    more JSON). That makes the first prefix length the original per-item
    loop failed on -- its stopping point -- exactly the largest prefix
    length that fits, which this binary search finds directly using the
    SAME `count_text_tokens(json.dumps(...))` measurement the original loop
    used, in O(log n) calls instead of O(n).
    """
    lo, hi, best = 0, len(all_items), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if count_text_tokens(json.dumps(all_items[:mid])) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return all_items[:best], best < len(all_items)


def _edge_origin(edge: Any) -> str:
    for prefix, origin in _ORIGIN_PREFIXES.items():
        if edge.edge_id.startswith(prefix):
            return origin
    if edge.layer == "COCHANGE":
        return "git-history"
    if edge.layer == "HEADING":
        return "markdown-parser"
    return "unknown"


def _ambiguous_candidates(edge: Any) -> list[str]:
    """Sorted resolver candidates of an unresolved ambiguous ``calls`` edge."""
    metadata = edge.metadata if isinstance(getattr(edge, "metadata", None), dict) else {}
    if edge.relation != "calls" or not edge.target.startswith("name:") or not metadata.get("ambiguous"):
        return []
    raw = metadata.get("candidates")
    if not isinstance(raw, list):
        return []
    return sorted(str(item) for item in raw)


def _relation_item(edge: Any, nodes: dict[str, Any], response_format: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "relation": edge.relation,
        "source": _endpoint(edge.source, nodes),
        "target": _endpoint(edge.target, nodes),
        "layer": edge.layer,
        "confidence": edge.confidence,
        "origin": _edge_origin(edge),
    }
    candidates = _ambiguous_candidates(edge)
    if candidates:
        # Only ambiguous call targets change: name it as unresolved and say
        # how many symbols the resolver could not choose between.
        item["target"] = f"{edge.target} (ambiguous: {len(candidates)} candidates)"
        if response_format == "detailed":
            item["candidates"] = candidates
    return item


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

    response_format = _response_format(arguments)
    all_items: list[dict[str, Any]] = [_relation_item(edge, nodes, response_format) for edge in edges]
    budget = int(arguments.get("budget", 2000))
    items, truncated = _prefix_fit_items(all_items, budget)
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
    all_items: list[dict[str, Any]] = [
        {"path": path, "score": score, "snippet": snippet} for path, score, snippet in hits
    ]
    items, truncated = _prefix_fit_items(all_items, budget)
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
    _forget_fingerprint(repo_root)
    summary = ingest_repository(
        repo_root,
        commit_limit=int(arguments.get("commits", 1000)),
        incremental=incremental,
    )
    try:
        _remember_fingerprint(repo_root, CortexStore(default_db_path(repo_root)).get_repo_fingerprint(repo_root))
    except Exception:
        _forget_fingerprint(repo_root)
    return _content({"summary": summary, "mode": "incremental" if incremental else "full", "stale": False})


# Tools covered by the P0-1 token-savings ledger. cortex_refresh is excluded:
# it doesn't return retrievable content to compare against a raw-read baseline.
# Tools the token-savings ledger prices. `cortex_refresh` writes the index
# rather than answering from it, and `cortex_status` reports on the server
# process itself -- neither has a "tokens an agent would have spent reading
# files instead" counterfactual to measure against, so neither is priced.
_LEDGER_TOOLS = {
    "cortex_query",
    "cortex_overview",
    "cortex_context",
    "cortex_impact",
    "cortex_search_symbols",
    "cortex_read_symbol",
    "cortex_read_file",
    "cortex_relations",
    "cortex_path",
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


def build_status_result(**extra_fields: Any) -> dict[str, Any]:
    """Build the `cortex_status` tool result: the in-flight registry snapshot
    plus the active deadline budget. Touches no module state owned by a
    running `call_tool`, so the server's reader thread can call it while a
    tool is executing."""
    snapshot = REGISTRY.snapshot()
    snapshot["deadline_budget_seconds"] = resolve_budget_seconds()
    snapshot["fingerprint_ttl_seconds"] = fingerprint_ttl_seconds()
    snapshot.update(extra_fields)
    return _content(snapshot)


def _call_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Report what this server process is doing right now.

    The bug this answers (docs/fix-plan-mcp-concurrency-wedge.md) was
    diagnosed only by reading /proc and attempting ptrace, which the
    reporter's sandbox blocked. This exposes the same facts from inside the
    process: what is in flight, how long each call has been running, and
    lifetime per-tool counters including how many calls hit their deadline.

    Deliberately does not touch the index or the database -- it must stay
    answerable while another call is mid-scan, which is the exact moment it
    is most useful.
    """
    return build_status_result()


def _referenced_file_tokens(store: CortexStore, repo_root: Path, paths: set[str]) -> int:
    total = 0
    for path in paths:
        if not path:
            continue
        token_data = store.fetch_source_token_data(repo_root, path)
        if token_data is not None and token_data[0]:
            content, kind = token_data
            total += count_text_tokens(content, kind=kind)
    return total


def _detailed_rendering_tokens(tool: str, arguments: dict[str, Any]) -> int:
    """Re-run a structure-only tool with response_format=detailed and count it.

    Used only as an input to _estimate_baseline's policy for search/relations/
    path/overview (see its docstring). Re-dispatching is simple, deterministic, and
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
    elif tool == "cortex_path":
        detailed_result = _call_path(detailed_args)
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

    Policy is kept in this one function so it stays auditable:

    - File-returning tools (cortex_query, cortex_context, cortex_impact,
      cortex_read_symbol, cortex_read_file, cortex_references,
      cortex_search_text, cortex_risk, cortex_dead_code): baseline is
      the token cost of reading, in full and raw, every DISTINCT file
      referenced in the actual response -- the tokens an agent would have
      spent with plain Read/grep instead of this tool. Computed via
      the stored source content and kind, so it reflects the exact indexed
      input Cortex itself read. This applies uniformly across cortex_read_symbol's
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
      cortex_path, cortex_overview): these return an index/graph view with no
      single "raw file" backing them, so there's no direct raw-read baseline.
      The baseline instead is the token cost of the `detailed` rendering of
      the same call -- the savings Cortex's concise response format already
      provides over its own verbose format. cortex_relations and cortex_path
      additionally fold in the referenced files' raw content, since their
      items point at specific graph locations a raw-read comparison can price.

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

    if tool == "cortex_path":
        detailed_tokens = _detailed_rendering_tokens(tool, arguments)
        endpoints = [payload.get("source", ""), payload.get("target", "")]
        endpoints.extend(
            hop.get("node", "")
            for path in payload.get("paths", [])
            for hop in path
            if isinstance(hop, dict)
        )
        paths = {
            match.group(1)
            for endpoint in endpoints
            if (match := _RELATION_ENDPOINT_FILE_RE.search(str(endpoint)))
        }
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
    records -- deliberately not a second, separate estimate.
    `_estimate_baseline`/`_detailed_rendering_tokens` may recurse
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


def call_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    cancel: threading.Event | None = None,
    request_id: Any = None,
) -> dict[str, Any]:
    """Dispatch one MCP tool call under a wall-clock budget.

    Phase 2/4 of docs/fix-plan-mcp-concurrency-wedge.md: every call runs
    inside a `deadline_scope` so a pathological repository cannot burn
    minutes of CPU unnoticed (hot loops call `check_deadline()`), and inside
    `REGISTRY.track` so a concurrent `cortex_status` call can report what is
    in flight and for how long. A `DeadlineExceeded` is turned into a normal
    structured error payload rather than a crash -- the server must stay
    responsive for the rest of the batch.

    `cancel` is the per-request cancellation event. The globals reset in the
    `finally` cover `_record_tool_usage` too: the ledger's internal second
    `_call_overview` call must reuse the memoised analysis.
    """
    global _current_cancel, _in_tool_call, _analysis_memo
    _current_cancel = cancel
    _in_tool_call = True
    try:
        args = arguments or {}
        budget_seconds = resolve_budget_seconds()
        tracked_id = "" if request_id is None else str(request_id)
        try:
            with REGISTRY.track(name, tracked_id), deadline_scope(budget_seconds, tool_name=name):
                result = _dispatch_tool(name, args)
        except DeadlineExceeded as exc:
            return _content(
                {
                    "error": "deadline_exceeded",
                    "tool": name,
                    "message": str(exc),
                    "elapsed_seconds": round(exc.elapsed_seconds, 3),
                    "budget_seconds": exc.budget_seconds,
                    "hint": (
                        "This call exceeded its wall-clock budget and was stopped so it could not "
                        "block other requests. Narrow the request (a smaller budget or a more specific "
                        "target), or raise CORTEX_TOOL_TIMEOUT_SECONDS if this repository legitimately "
                        "needs longer."
                    ),
                },
                is_error=True,
            )
        except Exception as exc:
            return _content({"error": type(exc).__name__, "message": str(exc)}, is_error=True)

        _record_tool_usage(name, args, result)
        return result
    finally:
        _current_cancel = None
        _in_tool_call = False
        _analysis_memo = None


def _dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route one tool name to its handler. Split out of `call_tool` so the
    deadline/status wrappers stay one level above the routing table."""
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
    elif name == "cortex_status":
        result = _call_status(args)
    else:
        return _content({"error": "unknown_tool", "message": f"Unknown Cortex tool: {name}"}, is_error=True)
    return result
