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


def main() -> int:
    try:
        status = _repo_status(Path.cwd())
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
            "Prefer cortex_query, cortex_search_symbols, and cortex_impact MCP tools "
            "over raw Grep/Glob/Bash-grep exploration; call cortex_refresh to update a stale index."
        )
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
