#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if SRC.is_dir():
    sys.path.insert(0, str(SRC))


def _emit(additional_context: str) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context,
                }
            },
            separators=(",", ":"),
        )
    )


def _repo_status(repo_root: Path) -> tuple[str, int] | None:
    from cortex.ingest import compute_repo_fingerprint
    from cortex.store import default_db_path

    db_path = default_db_path(repo_root)
    if not db_path.exists():
        return ("missing", 0)

    repo_key = str(repo_root.resolve())
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        stored_row = connection.execute(
            "SELECT fingerprint FROM repos WHERE repo_path = ?",
            (repo_key,),
        ).fetchone()
        count_row = connection.execute(
            "SELECT COUNT(*) FROM sources WHERE repo_path = ?",
            (repo_key,),
        ).fetchone()
    finally:
        connection.close()

    stored_fingerprint = "" if stored_row is None else str(stored_row[0] or "")
    file_count = int(count_row[0] if count_row is not None else 0)
    current_fingerprint = compute_repo_fingerprint(repo_root)
    return ("fresh" if stored_fingerprint == current_fingerprint else "stale", file_count)


def _inside_git_repo(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def main() -> int:
    try:
        cwd = Path.cwd()
        if not _inside_git_repo(cwd):
            return 0
        status = _repo_status(cwd)
        if status is None:
            return 0

        freshness, file_count = status
        if freshness == "missing":
            _emit("No Cortex index found for this project; cortex_refresh can build it.")
            return 0

        state = "fresh" if freshness == "fresh" else "stale"
        connector = "and is" if state == "fresh" else "but is"
        _emit(
            f"Cortex index exists {connector} {state} ({file_count} indexed files). "
            "Prefer Cortex MCP tools over raw Grep/Glob/Read exploration: "
            "cortex_context (batch all paths/symbols once before editing several files), "
            "cortex_query (task-focused context bundle), cortex_search_symbols (find a symbol by name), "
            "cortex_read_symbol (read one symbol's span; mode=skeleton/signature for cheaper partial reads), "
            "cortex_read_file (direct Read replacement for an indexed file; mode=skeleton by default -- "
            "imports/includes + top-level signatures, bodies elided), "
            "cortex_impact (co-change/structural neighbors before editing), "
            "cortex_relations (parsed graph edges — 'who inherits/emits/connects to X'), "
            "cortex_references (blast-radius — graph edges + cross-language grep for a symbol, "
            "including CMake/scripts/configs/docs the parser doesn't index), "
            "cortex_search_text (full-text body search over indexed file contents — string "
            "literals, error messages, comments, prose — a grep replacement that reads from "
            "the index), "
            "cortex_overview (repo orientation and top churn×complexity hotspots; cortex_query accepts "
            "hotspot_boost=true as an opt-in ranking signal). Optional local semantic retrieval is "
            "managed with `cortex semantic status`/`cortex semantic setup` and never downloads during "
            "ingest or query. Call cortex_refresh to update a stale index."
        )
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
