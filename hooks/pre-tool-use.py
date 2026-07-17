#!/usr/bin/env python3
"""Fail-open Cortex advisory hook for Claude Code's built-in search tools.

This hook deliberately has a much smaller job than the MCP server.  It reads
only the event payload and indexed SQLite metadata, never refreshes the index
or reads a working-tree file.  A broken or unavailable index is equivalent to
having no hook at all.

Environment variables:

``CORTEX_HOOK_MODE``
    ``off``, ``advise`` (the default), or experimental ``enforce``.
``CORTEX_HOOK_READ_THRESHOLD_BYTES``
    Minimum indexed source size for a ``Read`` redirect (default: 512).
    ``CORTEX_HOOK_READ_THRESHOLD`` is accepted as a compatibility alias.
``CORTEX_HOOK_STALE_AFTER_SECONDS``
    Maximum age for enforce mode before it is downgraded to advice (default:
    86400 seconds).  ``CORTEX_HOOK_STALE_THRESHOLD_SECONDS`` and
    ``CORTEX_HOOK_MAX_INDEX_AGE_SECONDS`` are accepted aliases.

Decision records are best-effort JSONL rows beside the central per-repository
SQLite database (``<CORTEX_DATA_DIR>/<repo-hash>/usage.jsonl``).  They contain
metadata and estimates only; no source content or arbitrary regular
expressions are written.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote

# The plugin is intentionally runnable without an editable installation.  Keep
# this the same self-location pattern as hooks/session-start.py.
SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))

try:
    # Reuse the store's identifier semantics so a hook redirect and an MCP
    # search agree on camelCase, snake_case, and Qt names.
    from cortex.store import (  # type: ignore
        _normalized_identifier,
        _search_tokens,
        default_db_path,
        repo_data_dir,
    )
except Exception:  # pragma: no cover - exercised by the fail-open boundary
    _normalized_identifier = None  # type: ignore[assignment]
    _search_tokens = None  # type: ignore[assignment]
    default_db_path = None  # type: ignore[assignment]
    repo_data_dir = None  # type: ignore[assignment]


EVENT_NAME = "PreToolUse"
SUPPORTED_TOOLS = frozenset({"Read", "Grep", "Glob"})
DEFAULT_READ_THRESHOLD_BYTES = 512
DEFAULT_STALE_AFTER_SECONDS = 24 * 60 * 60
DB_TIMEOUT_SECONDS = 0.05
MAX_INPUT_TEXT = 4096
MAX_LOG_TARGET = 256

# Grep is intentionally restricted to a literal identifier.  In particular,
# a regex that happens to contain an identifier must pass through untouched:
# redirecting it to symbol search could lose matches.
_SIMPLE_IDENTIFIER = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*$"
)
_IDENTIFIER_RUN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GLOB_META = frozenset("*?")
_COMMON_GLOB_WORDS = frozenset(
    {
        "",
        "src",
        "lib",
        "include",
        "source",
        "sources",
        "test",
        "tests",
        "testdata",
        "fixture",
        "fixtures",
        "example",
        "examples",
        "app",
        "apps",
        "build",
        "dist",
        "debug",
        "release",
        "py",
        "js",
        "ts",
        "tsx",
        "jsx",
        "c",
        "cc",
        "cpp",
        "cxx",
        "h",
        "hh",
        "hpp",
        "hxx",
        "qml",
        "go",
        "rs",
        "java",
        "rb",
        "swift",
        "md",
        "txt",
        "json",
        "yaml",
        "yml",
        "toml",
        "sh",
    }
)


class SymbolMatch(NamedTuple):
    node_id: str
    label: str
    source_ref: str
    signature: str
    span_start: int | None = None
    span_end: int | None = None


class IndexInfo(NamedTuple):
    connection: sqlite3.Connection
    repo_root: Path
    freshness: str
    index_age_seconds: int


class SearchScope(NamedTuple):
    # ``path`` is repo-relative. An empty path is the repository root; ``exact``
    # distinguishes a file from a directory prefix. ``None`` at call sites
    # means that the tool supplied no path option at all.
    path: str
    exact: bool


class Decision(NamedTuple):
    tool: str
    target: str
    normalized_target: str
    action: str
    reason: str
    mode: str
    freshness: str
    index_age_seconds: int
    match_count: int
    estimated_tokens: int
    message: str = ""
    emit_output: bool = False
    enforce: bool = False


def _env_number(names: tuple[str, ...], default: float, *, minimum: float = 0.0) -> float:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value >= minimum:
            return value
    return default


def _hook_mode() -> str:
    value = os.environ.get("CORTEX_HOOK_MODE", "advise").strip().lower()
    return value if value in {"off", "advise", "enforce"} else "advise"


def _read_threshold_bytes() -> int:
    return int(
        _env_number(
            (
                "CORTEX_HOOK_READ_THRESHOLD_BYTES",
                "CORTEX_HOOK_READ_THRESHOLD",
                "CORTEX_HOOK_READ_MIN_BYTES",
                "CORTEX_HOOK_SIZE_THRESHOLD_BYTES",
            ),
            DEFAULT_READ_THRESHOLD_BYTES,
        )
    )


def _stale_after_seconds() -> float:
    return _env_number(
        (
            "CORTEX_HOOK_STALE_AFTER_SECONDS",
            "CORTEX_HOOK_STALE_AFTER",
            "CORTEX_HOOK_STALE_THRESHOLD_SECONDS",
            "CORTEX_HOOK_MAX_INDEX_AGE_SECONDS",
            "CORTEX_HOOK_STALE_INDEX_SECONDS",
            "CORTEX_HOOK_STALE_THRESHOLD",
            "CORTEX_HOOK_MAX_AGE_SECONDS",
        ),
        DEFAULT_STALE_AFTER_SECONDS,
    )


def _find_git_root(cwd_value: object) -> Path | None:
    """Find a repository marker without invoking git or reading source files."""

    if not isinstance(cwd_value, str) or not cwd_value.strip() or len(cwd_value) > MAX_INPUT_TEXT:
        return None
    try:
        candidate = Path(cwd_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None

    # Path.parents includes the root's ancestors.  ``.git`` may be a file in
    # a worktree, so existence rather than is_dir() is intentional.
    for directory in (candidate, *candidate.parents):
        try:
            if (directory / ".git").exists():
                return directory
        except OSError:
            return None
    return None


def _read_only_uri(db_path: Path) -> str:
    # Quote spaces, '#', and '?' so a repo path cannot change URI parameters.
    return "file:" + quote(str(db_path), safe="/:\\") + "?mode=ro"


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names are constants from _open_index, never event input.
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _open_index(repo_root: Path) -> IndexInfo | None:
    """Open and validate the existing index, read-only, or return None."""

    if default_db_path is None:
        return None
    try:
        db_path = default_db_path(repo_root)
        if not db_path.is_file():
            return None
        connection = sqlite3.connect(
            _read_only_uri(db_path),
            uri=True,
            timeout=DB_TIMEOUT_SECONDS,
        )
        connection.row_factory = sqlite3.Row
        # This is a connection-local guard in addition to mode=ro.  It does
        # not write a pragma to the database.
        connection.execute("PRAGMA query_only = ON")

        required = {
            "repos": {"repo_path", "updated_at"},
            "sources": {"repo_path", "path", "size_bytes"},
            "graph_nodes": {
                "repo_path",
                "node_id",
                "label",
                "source_ref",
                "granularity",
                "signature",
                "span_start",
                "span_end",
            },
        }
        for table, columns in required.items():
            if not columns <= _table_columns(connection, table):
                connection.close()
                return None

        repo_key = str(repo_root.resolve())
        row = connection.execute(
            "SELECT updated_at FROM repos WHERE repo_path = ?",
            (repo_key,),
        ).fetchone()
        if row is None:
            connection.close()
            return None
        updated_at = int(row["updated_at"])
        age = max(0, int(time.time()) - updated_at)
        freshness = "stale" if age > _stale_after_seconds() else "fresh"
        return IndexInfo(connection, repo_root, freshness, age)
    except Exception:
        # Close a connection if an error happened after connect.  The local
        # variable is deliberately handled without a second failure path.
        try:
            connection.close()  # type: ignore[name-defined]
        except Exception:
            pass
        return None


def _close_index(index: IndexInfo | None) -> None:
    if index is None:
        return
    try:
        index.connection.close()
    except Exception:
        pass


def _safe_text(value: object, *, limit: int = MAX_INPUT_TEXT) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value


def _simple_identifier(value: object) -> tuple[str, str] | None:
    raw = _safe_text(value)
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate or not _SIMPLE_IDENTIFIER.fullmatch(candidate):
        return None
    if _normalized_identifier is None or _search_tokens is None:
        return None
    try:
        normalized = _normalized_identifier(candidate)
        if not normalized or not _search_tokens(candidate):
            return None
    except Exception:
        return None
    return candidate, normalized


def _symbol_from_row(row: sqlite3.Row) -> SymbolMatch:
    def optional_int(value: object) -> int | None:
        try:
            return None if value is None else int(value)
        except (TypeError, ValueError):
            return None

    return SymbolMatch(
        node_id=str(row["node_id"] or ""),
        label=str(row["label"] or ""),
        source_ref=str(row["source_ref"] or ""),
        signature=str(row["signature"] or ""),
        span_start=optional_int(row["span_start"]),
        span_end=optional_int(row["span_end"]),
    )


def _matches_for_identifier(
    index: IndexInfo,
    identifier: str,
    normalized: str,
    scope: SearchScope | None = None,
) -> list[SymbolMatch]:
    """Find exact normalized symbol labels using DB metadata only."""

    if _search_tokens is None or _normalized_identifier is None:
        return []
    tokens = _search_tokens(identifier)
    if not tokens:
        return []
    clauses = " AND ".join("label LIKE ?" for _ in tokens)
    scope_clause, scope_params = _scope_filter(scope)
    params: list[object] = [str(index.repo_root.resolve()), *scope_params]
    params.extend(f"%{token}%" for token in tokens)
    rows = index.connection.execute(
        f"""
        SELECT node_id, label, source_ref, signature, span_start, span_end
        FROM graph_nodes
        WHERE repo_path = ? AND granularity = 'symbol'{scope_clause} AND {clauses}
        """,
        tuple(params),
    ).fetchall()
    matches: list[SymbolMatch] = []
    for row in rows:
        try:
            match = _symbol_from_row(row)
            if _normalized_identifier(match.label) == normalized:
                matches.append(match)
        except Exception:
            # One malformed metadata row must not turn a usable DB into a
            # blocking hook; it simply cannot be a redirect candidate.
            continue
    return sorted(matches, key=lambda item: (item.source_ref, item.span_start or 0, item.node_id))


def _symbols_for_path(index: IndexInfo, relative_path: str) -> list[SymbolMatch]:
    rows = index.connection.execute(
        """
        SELECT node_id, label, source_ref, signature, span_start, span_end
        FROM graph_nodes
        WHERE repo_path = ? AND source_ref = ? AND granularity = 'symbol'
        """,
        (str(index.repo_root.resolve()), relative_path),
    ).fetchall()
    result: list[SymbolMatch] = []
    for row in rows:
        try:
            result.append(_symbol_from_row(row))
        except Exception:
            continue
    return sorted(result, key=lambda item: (item.span_start is None, item.span_start or 0, item.span_end or 0, item.node_id))


def _estimate_symbol_tokens(matches: list[SymbolMatch]) -> int:
    """Estimate indexed symbol metadata at roughly four UTF-8 bytes/token."""

    total_bytes = 0
    for match in matches:
        # Labels/signatures/paths are all already stored metadata.  The small
        # fixed overhead represents the MCP result's location separators.
        total_bytes += len(match.label.encode("utf-8", errors="replace"))
        total_bytes += len(match.signature.encode("utf-8", errors="replace"))
        total_bytes += len(match.source_ref.encode("utf-8", errors="replace"))
        total_bytes += 16
    return max(1, math.ceil(total_bytes / 4)) if matches else 0


def _estimate_read_tokens(size_bytes: int, matches: list[SymbolMatch]) -> tuple[int, int, int]:
    """Return raw, skeleton, and savings estimates without source reads."""

    raw_tokens = max(1, math.ceil(max(0, size_bytes) / 4))
    skeleton_bytes = 0
    for match in matches:
        signature = match.signature or match.label
        # Include a modest line/marker allowance; no body or file content is
        # inspected, and this remains deterministic across Python versions.
        skeleton_bytes += len(signature.encode("utf-8", errors="replace")) + 12
    skeleton_tokens = max(1, math.ceil(skeleton_bytes / 4)) if matches else raw_tokens
    return raw_tokens, skeleton_tokens, max(0, raw_tokens - skeleton_tokens)


def _relative_target(repo_root: Path, raw_path: object, cwd: Path) -> str | None:
    value = _safe_text(raw_path)
    if value is None:
        return None
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            # Claude normally sends a path relative to cwd.  Also try the repo
            # root for callers that construct events from a repo-relative path.
            candidates = [(cwd / candidate).resolve(strict=False), (repo_root / candidate).resolve(strict=False)]
        else:
            candidates = [candidate.resolve(strict=False)]
        for resolved in candidates:
            try:
                relative = resolved.relative_to(repo_root.resolve(strict=False))
            except ValueError:
                continue
            text = relative.as_posix()
            if text and not text.startswith("../"):
                return text
    except (OSError, RuntimeError, ValueError):
        return None
    return None


def _search_scope(
    index: IndexInfo,
    tool_input: dict[str, Any],
    cwd: Path,
) -> tuple[SearchScope | None, bool]:
    """Resolve a Grep/Glob ``path`` against indexed source metadata.

    The hook does not stat or read the requested path. A supplied path is
    usable only when it is an indexed source file or a directory prefix with
    at least one indexed source below it. ``None`` means no path option was
    supplied; the boolean is false for outside/unindexed scopes.
    """

    if "path" not in tool_input:
        return None, True
    relative = _relative_target(index.repo_root, tool_input.get("path"), cwd)
    if relative is None:
        return None, False
    path = "" if relative in {"", "."} else relative.rstrip("/")
    repo_key = str(index.repo_root.resolve())
    if not path:
        row = index.connection.execute(
            "SELECT 1 FROM sources WHERE repo_path = ? LIMIT 1",
            (repo_key,),
        ).fetchone()
        return SearchScope("", False), row is not None

    exact = index.connection.execute(
        "SELECT 1 FROM sources WHERE repo_path = ? AND path = ? LIMIT 1",
        (repo_key, path),
    ).fetchone()
    if exact is not None:
        return SearchScope(path, True), True

    prefix = path + "/"
    directory = index.connection.execute(
        """
        SELECT 1 FROM sources
        WHERE repo_path = ? AND substr(path, 1, ?) = ?
        LIMIT 1
        """,
        (repo_key, len(prefix), prefix),
    ).fetchone()
    if directory is None:
        return None, False
    return SearchScope(path, False), True


def _scope_filter(scope: SearchScope | None, column: str = "source_ref") -> tuple[str, list[object]]:
    if scope is None or not scope.path:
        return "", []
    if scope.exact:
        return f" AND {column} = ?", [scope.path]
    prefix = scope.path + "/"
    return f" AND substr({column}, 1, ?) = ?", [len(prefix), prefix]


def _scope_description(scope: SearchScope | None) -> str:
    if scope is None:
        return ""
    return f" within indexed scope `{scope.path or '.'}`"


def _can_enforce_search(tool: str, tool_input: dict[str, Any], scope: SearchScope | None) -> bool:
    """Whether the Cortex replacement can preserve the raw tool options.

    Cortex MCP search calls have no file/path filter, so a scoped Grep/Glob
    can be advised after metadata filtering but must never be denied. Other
    options (glob/type/context/limits/etc.) are likewise not faithfully
    represented by the replacement call.
    """

    if tool in {"Grep", "Glob"}:
        return scope is None and set(tool_input) <= {"pattern"}
    return False


def _glob_stems(pattern: str) -> list[str]:
    """Extract identifier-like wildcard stems, excluding plain extensions."""

    stems: list[str] = []
    for match in _IDENTIFIER_RUN.finditer(pattern):
        value = match.group(0)
        lowered = value.lower()
        if lowered in _COMMON_GLOB_WORDS:
            continue
        left = pattern[match.start() - 1] if match.start() else ""
        right = pattern[match.end()] if match.end() < len(pattern) else ""
        # A literal directory such as ``src/**`` is not a symbol stem.  A
        # token adjacent to glob syntax, or a code-like CamelCase/underscore
        # token in a filename component, is.
        adjacent_to_meta = left in _GLOB_META or right in _GLOB_META
        code_like = any(character.isupper() for character in value) or "_" in value
        if not adjacent_to_meta and not code_like:
            continue
        if value not in stems:
            stems.append(value)
    return sorted(stems, key=lambda value: (-len(value), value.lower(), value))


def _json_call(tool: str, arguments: dict[str, object]) -> str:
    rendered = json.dumps(arguments, separators=(",", ":"), ensure_ascii=True)
    return tool + "(" + rendered + ")"


def _grep_decision(
    index: IndexInfo,
    tool_input: dict[str, Any],
    mode: str,
    scope: SearchScope | None,
    scope_valid: bool,
) -> Decision | None:
    literal = _simple_identifier(tool_input.get("pattern"))
    if literal is None:
        # A well-formed but regex-shaped Grep is an evaluated pass.  Keep the
        # log target blank so no arbitrary regular expression is persisted.
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern and len(pattern) <= MAX_INPUT_TEXT:
            return Decision(
                "Grep",
                "",
                "",
                "pass",
                "unsupported_pattern",
                mode,
                index.freshness,
                index.index_age_seconds,
                0,
                0,
                emit_output=False,
            )
        return None
    identifier, normalized = literal
    if not scope_valid:
        return Decision(
            "Grep",
            identifier,
            normalized,
            "pass",
            "unindexed_scope",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )
    matches = _matches_for_identifier(index, identifier, normalized, scope)
    if not matches:
        return Decision(
            "Grep",
            identifier,
            normalized,
            "pass",
            "unindexed_identifier" if scope is None else "unindexed_scope",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )

    estimate = _estimate_symbol_tokens(matches)
    limit = min(20, max(1, len(matches)))
    replacement_search = _json_call("cortex_search_symbols", {"query": identifier, "limit": limit})
    replacement_refs = _json_call("cortex_references", {"symbol": identifier, "budget": 2000})
    message = (
        f"Cortex indexed identifier `{identifier}` ({len(matches)} matching symbols)"
        f"{_scope_description(scope)}. "
        f"Use {replacement_search} and {replacement_refs} instead of Grep; "
        f"the indexed metadata estimate is ~{estimate} tokens."
    )
    enforce_safe = _can_enforce_search("Grep", tool_input, scope)
    if mode == "enforce" and index.freshness == "fresh" and enforce_safe:
        action = "deny"
        reason = "enforce_redirect"
        enforce = True
    elif mode == "enforce" and not enforce_safe:
        action = "advise"
        reason = "restrictive_options_no_enforce"
        enforce = False
        message = (
            "Cortex advisory only (the Grep scope/options cannot be represented exactly by an "
            "enforced replacement): " + message
        )
    elif mode == "enforce":
        action = "downgrade"
        reason = "stale_index_auto_downgrade"
        enforce = False
        message = (
            f"Cortex advisory (enforce downgraded because the index is {index.index_age_seconds}s old): "
            + message
        )
    else:
        action = "advise"
        reason = "indexed_identifier"
        enforce = False
    return Decision(
        "Grep",
        identifier,
        normalized,
        action,
        reason,
        mode,
        index.freshness,
        index.index_age_seconds,
        len(matches),
        estimate,
        message,
        emit_output=True,
        enforce=enforce,
    )


def _glob_decision(
    index: IndexInfo,
    tool_input: dict[str, Any],
    mode: str,
    scope: SearchScope | None,
    scope_valid: bool,
) -> Decision | None:
    pattern = _safe_text(tool_input.get("pattern"))
    if pattern is None:
        return None
    if not scope_valid:
        return Decision(
            "Glob",
            "",
            "",
            "pass",
            "unindexed_scope",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )
    if not any(marker in pattern for marker in _GLOB_META):
        return Decision(
            "Glob",
            "",
            "",
            "pass",
            "plain_glob",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )
    for stem in _glob_stems(pattern):
        literal = _simple_identifier(stem)
        if literal is None:
            continue
        identifier, normalized = literal
        matches = _matches_for_identifier(index, identifier, normalized, scope)
        if not matches:
            continue
        estimate = _estimate_symbol_tokens(matches)
        limit = min(20, max(1, len(matches)))
        replacement = _json_call("cortex_search_symbols", {"query": identifier, "limit": limit})
        message = (
            f"Cortex indexed wildcard stem `{identifier}` ({len(matches)} matching symbols)"
            f"{_scope_description(scope)}. "
            f"Use {replacement} instead of Glob; the indexed metadata estimate is ~{estimate} tokens."
        )
        enforce_safe = _can_enforce_search("Glob", tool_input, scope)
        if mode == "enforce" and index.freshness == "fresh" and enforce_safe:
            action = "deny"
            reason = "enforce_redirect"
            enforce = True
        elif mode == "enforce" and not enforce_safe:
            action = "advise"
            reason = "restrictive_options_no_enforce"
            enforce = False
            message = (
                "Cortex advisory only (the Glob scope/options cannot be represented exactly by an "
                "enforced replacement): " + message
            )
        elif mode == "enforce":
            action = "downgrade"
            reason = "stale_index_auto_downgrade"
            enforce = False
            message = (
                f"Cortex advisory (enforce downgraded because the index is {index.index_age_seconds}s old): "
                + message
            )
        else:
            action = "advise"
            reason = "indexed_wildcard_stem"
            enforce = False
        return Decision(
            "Glob",
            identifier,
            normalized,
            action,
            reason,
            mode,
            index.freshness,
            index.index_age_seconds,
            len(matches),
            estimate,
            message,
            emit_output=True,
            enforce=enforce,
        )
    return Decision(
        "Glob",
        "",
        "",
        "pass",
        "unindexed_glob_stem",
        mode,
        index.freshness,
        index.index_age_seconds,
        0,
        0,
        emit_output=False,
    )


def _read_decision(
    index: IndexInfo,
    tool_input: dict[str, Any],
    mode: str,
    repo_root: Path,
    cwd: Path,
) -> Decision | None:
    raw_path = tool_input.get("file_path")
    if raw_path is None:
        raw_path = tool_input.get("path")
    relative_path = _relative_target(repo_root, raw_path, cwd)
    if relative_path is None:
        return None
    row = index.connection.execute(
        "SELECT size_bytes FROM sources WHERE repo_path = ? AND path = ?",
        (str(repo_root.resolve()), relative_path),
    ).fetchone()
    if row is None:
        return Decision(
            "Read",
            "",
            "",
            "pass",
            "unindexed_read_path",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )
    try:
        size_bytes = int(row["size_bytes"])
    except (TypeError, ValueError):
        return None
    unsupported_options = set(tool_input) - {"file_path", "path", "offset", "limit"}
    if unsupported_options:
        return Decision(
            "Read",
            relative_path,
            relative_path,
            "pass",
            "restrictive_options_no_enforce",
            mode,
            index.freshness,
            index.index_age_seconds,
            0,
            0,
            emit_output=False,
        )
    symbols = _symbols_for_path(index, relative_path)
    threshold = _read_threshold_bytes()
    # Offset/limit reads are already targeted; a skeleton would not be an
    # equivalent replacement, so leave them alone even for a large source.
    has_window = "offset" in tool_input or "limit" in tool_input
    if size_bytes <= threshold or not symbols or has_window:
        reason = "read_below_threshold" if size_bytes <= threshold else (
            "read_without_indexed_symbols" if not symbols else "read_windowed"
        )
        return Decision(
            "Read",
            relative_path,
            relative_path,
            "pass",
            reason,
            mode,
            index.freshness,
            index.index_age_seconds,
            len(symbols),
            0,
            emit_output=True,
        )

    raw_tokens, skeleton_tokens, savings = _estimate_read_tokens(size_bytes, symbols)
    replacement_file = _json_call(
        "cortex_read_file",
        {"path": relative_path, "mode": "skeleton"},
    )
    first_symbol = symbols[0]
    replacement_symbol = _json_call(
        "cortex_read_symbol",
        {"symbol": first_symbol.node_id, "mode": "skeleton"},
    )
    message = (
        f"Cortex indexed `{relative_path}` ({size_bytes} bytes). Use {replacement_file} "
        f"for orientation, then {replacement_symbol} for a targeted symbol; "
        f"stored-metadata estimate: raw ~{raw_tokens} tokens vs skeleton ~{skeleton_tokens} "
        f"tokens (~{savings} fewer)."
    )
    if mode == "enforce" and index.freshness == "fresh":
        action = "deny"
        reason = "enforce_redirect"
        enforce = True
    elif mode == "enforce":
        action = "downgrade"
        reason = "stale_index_auto_downgrade"
        enforce = False
        message = (
            f"Cortex advisory (enforce downgraded because the index is {index.index_age_seconds}s old): "
            + message
        )
    else:
        action = "advise"
        reason = "indexed_large_read"
        enforce = False
    return Decision(
        "Read",
        relative_path,
        relative_path,
        action,
        reason,
        mode,
        index.freshness,
        index.index_age_seconds,
        len(symbols),
        savings,
        message,
        emit_output=True,
        enforce=enforce,
    )


def _append_decision(repo_root: Path, decision: Decision) -> None:
    """Append one metadata-only row; logging can never affect the hook."""

    if repo_data_dir is None:
        return
    try:
        target = decision.normalized_target[:MAX_LOG_TARGET]
        record = {
            "timestamp": int(time.time()),
            "repo": str(repo_root.resolve()),
            "tool": decision.tool,
            "normalized_target": target,
            "action": decision.action,
            "reason": decision.reason,
            "mode": decision.mode,
            "freshness": decision.freshness,
            "index_age_seconds": int(decision.index_age_seconds),
            "match_count": int(decision.match_count),
            "estimated_tokens": int(decision.estimated_tokens),
        }
        log_path = repo_data_dir(repo_root) / "usage.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True) + "\n")
    except Exception:
        return


def _output_for_decision(decision: Decision) -> dict[str, Any] | None:
    if not decision.emit_output:
        return None
    if decision.action == "deny" and decision.enforce:
        return {
            "hookSpecificOutput": {
                "hookEventName": EVENT_NAME,
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.message,
                "additionalContext": decision.message,
            }
        }
    if decision.action in {"advise", "downgrade"}:
        # An explicit allow keeps advice nonblocking while still giving Claude
        # the documented PreToolUse hook-specific fields.
        return {
            "hookSpecificOutput": {
                "hookEventName": EVENT_NAME,
                "permissionDecision": "allow",
                "permissionDecisionReason": "Cortex advisory only; the original tool call remains allowed.",
                "additionalContext": decision.message,
            }
        }
    # A valid indexed target with no redirect gets an explicit allow.  Unknown
    # or non-target inputs use emit_output=False and therefore stay completely
    # silent, which is the safest fail-open behavior for a hook.
    return {
        "hookSpecificOutput": {
            "hookEventName": EVENT_NAME,
            "permissionDecision": "allow",
            "permissionDecisionReason": "No Cortex redirect applies; continue with the requested tool.",
        }
    }


def process_event(event: object) -> dict[str, Any] | None:
    """Evaluate one decoded Claude event and return its JSON output object.

    This function is public primarily for warm-decision benchmarks and tests;
    the executable entrypoint below is the only place that reads stdin.
    """

    if not isinstance(event, dict):
        return None
    if event.get("hook_event_name", EVENT_NAME) != EVENT_NAME:
        return None
    tool = event.get("tool_name")
    if tool not in SUPPORTED_TOOLS:
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    mode = _hook_mode()
    if mode == "off":
        return None
    repo_root = _find_git_root(event.get("cwd"))
    if repo_root is None:
        return None
    try:
        cwd = Path(str(event["cwd"])).expanduser().resolve(strict=False)
    except (KeyError, OSError, RuntimeError, ValueError):
        return None

    index = _open_index(repo_root)
    if index is None:
        return None
    decision: Decision | None = None
    try:
        scope: SearchScope | None = None
        scope_valid = True
        if tool in {"Grep", "Glob"}:
            scope, scope_valid = _search_scope(index, tool_input, cwd)
        if tool == "Grep":
            decision = _grep_decision(index, tool_input, mode, scope, scope_valid)
        elif tool == "Glob":
            decision = _glob_decision(index, tool_input, mode, scope, scope_valid)
        elif tool == "Read":
            decision = _read_decision(index, tool_input, mode, repo_root, cwd)
        if decision is None:
            return None
        _append_decision(repo_root, decision)
        return _output_for_decision(decision)
    except Exception:
        return None
    finally:
        _close_index(index)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw or len(raw) > 1_000_000:
            return 0
        try:
            event = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0
        output = process_event(event)
        if output is not None:
            sys.stdout.write(json.dumps(output, separators=(",", ":"), ensure_ascii=True))
    except Exception:
        # PreToolUse hooks must never strand the original tool call.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
