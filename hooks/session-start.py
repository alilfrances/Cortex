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

        runtime_warning = ""
        try:
            from cortex.runtime import status as runtime_status
            runtime = runtime_status()
            if not runtime.get("ready", False):
                runtime_warning = " Parser runtime degraded; QML and supported-language indexing is using the visible regex fallback. Run `cortex runtime setup` or `cortex runtime repair` when network/offline bundle access is available."
        except Exception:
            runtime_warning = " Parser runtime status unavailable; QML indexing may be degraded."

        freshness, file_count = status
        if freshness == "missing":
            _emit("No Cortex index found for this project; cortex_refresh can build it." + runtime_warning)
            return 0

        state = "fresh" if freshness == "fresh" else "stale"
        context = (
            f"Cortex index is {state} ({file_count} indexed files). Prefer Cortex MCP tools "
            "(cortex_query, cortex_context, cortex_search_symbols, cortex_read_file) over raw "
            "Grep/Glob/Read; delegate multi-step exploration to the cortex-explorer agent, keep "
            "single lookups direct."
        )
        if state == "stale":
            context += " Read/query tools auto-refresh incrementally; cortex_refresh forces it."
        _emit(context + runtime_warning)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
