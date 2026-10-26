#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
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
    from cortex.store import GRAPH_FORMAT_VERSION, default_db_path

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
        try:
            version_row = connection.execute(
                "SELECT graph_version FROM repos WHERE repo_path = ?",
                (repo_key,),
            ).fetchone()
        except sqlite3.OperationalError:
            version_row = None  # read-only open of a pre-0.10.0 database: no column yet
    finally:
        connection.close()

    stored_fingerprint = "" if stored_row is None else str(stored_row[0] or "")
    file_count = int(count_row[0] if count_row is not None else 0)
    graph_version = int(version_row[0] or 0) if version_row is not None else 0
    if graph_version < GRAPH_FORMAT_VERSION:
        return ("outdated", file_count)
    from cortex.ingest import compute_repo_fingerprint  # not needed (or imported) for an outdated index

    current_fingerprint = compute_repo_fingerprint(repo_root)
    return ("fresh" if stored_fingerprint == current_fingerprint else "stale", file_count)


def _ingest_lock_held(repo_root: Path) -> bool:
    """True when another process holds the ingest lock (a non-blocking probe, released at once).

    Anything unexpected counts as held: the caller then skips the background rebuild.
    """
    import fcntl

    from cortex.store import ingest_lock_path

    try:
        fd = os.open(ingest_lock_path(repo_root), os.O_RDWR)
    except FileNotFoundError:
        return False  # nothing has ever taken the lock
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _start_background_rebuild(repo_root: Path) -> str:
    """Spawn `cortex refresh --full` detached and return at once: "started", "running" or "skipped".

    "running" means the ingest lock is held, so another process is already rebuilding; "skipped" covers
    the opt-out, Windows and a missing launcher.

    The hook never runs ingest itself (it has a 5 s timeout). The launcher sets up the parser runtime first,
    so the rebuilt index is not silently a regex-fallback one. CORTEX_CLI_LAUNCHER is the test seam.
    """
    if os.environ.get("CORTEX_SESSION_START_REBUILD") == "0" or os.name == "nt":
        return "skipped"
    launcher = Path(os.environ.get("CORTEX_CLI_LAUNCHER") or Path(__file__).resolve().parent.parent / "bin" / "cortex-cli.py")
    if not launcher.is_file():
        return "skipped"
    if _ingest_lock_held(repo_root):
        return "running"
    subprocess.Popen(
        [sys.executable, str(launcher), "refresh", "--full", str(repo_root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return "started"


def _try_background_rebuild(repo_root: Path) -> str:
    try:
        return _start_background_rebuild(repo_root)
    except Exception:
        return "skipped"  # fail open: the message then tells the model to run cortex_refresh


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

        agent_hint = " Multi-step exploration can go to the cortex-explorer agent."
        rebuild = _try_background_rebuild(cwd) if freshness == "outdated" else "skipped"
        if rebuild == "started":
            context = (
                f"Cortex index was built by an older Cortex ({file_count} indexed files); a full rebuild has "
                "started in the background (about a minute on a large repository). Until it finishes, tools "
                "answer from the old index with stale: true."
            )
        elif rebuild == "running":
            context = (
                f"Cortex index was built by an older Cortex ({file_count} indexed files); a rebuild is already "
                "running in another process. Until it finishes, tools answer from the old index with "
                "stale: true; no cortex_refresh call is needed."
            )
        elif freshness == "outdated":
            context = (
                f"Cortex index was built by an older Cortex ({file_count} indexed files) and is answered "
                "as-is until rebuilt; run cortex_refresh (a full rebuild, about a minute on a large repository)."
            )
        elif freshness == "stale":
            context = (
                f"Cortex index is stale ({file_count} indexed files); read/query tools refresh it "
                "incrementally, cortex_refresh forces it."
            )
        else:
            context = f"Cortex index is fresh ({file_count} indexed files)."
        context += agent_hint
        _emit(context + runtime_warning)
        return 0
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
