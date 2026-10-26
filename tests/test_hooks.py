from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cortex.ingest import compute_repo_fingerprint
from cortex.models import SourceRecord
from cortex.store import GRAPH_FORMAT_VERSION, CortexStore, default_db_path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "session-start.py"


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    return repo


def _seed_db(repo: Path, fingerprint: str | None = None, graph_version: int = GRAPH_FORMAT_VERSION) -> None:
    store = CortexStore(default_db_path(repo))
    stored_fingerprint = fingerprint if fingerprint is not None else compute_repo_fingerprint(repo)
    store.reset_repo(repo, fingerprint=stored_fingerprint)
    store.set_repo_graph_version(repo, graph_version)
    store.save_sources(
        repo,
        [
            SourceRecord(
                path="README.md",
                content="# Demo\n",
                kind="markdown",
                size_bytes=7,
                modified_at=1.0,
                content_hash="readme-hash",
            ),
            SourceRecord(
                path="app.py",
                content="def main():\n    return 'ok'\n",
                kind="code",
                size_bytes=28,
                modified_at=1.0,
                content_hash="app-hash",
            ),
        ],
    )
    store.connection.close()


def _run_hook(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=5,
    )


def _additional_context(stdout: str) -> str:
    payload = json.loads(stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    return payload["hookSpecificOutput"]["additionalContext"]


def _assert_outdated_text(context: str) -> None:
    # The old text promised that the next read call rebuilds an outdated index; it does not (a call deadline
    # answers a format-outdated index as-is), so the message must say what really happens.
    assert "older Cortex" in context
    assert "cortex_refresh" in context
    assert "answered as-is" in context
    assert "next read" not in context
    assert "rebuilds it in full" not in context


def test_session_start_hook_emits_fresh_context(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    _seed_db(repo)

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)

    assert "Cortex index is fresh" in context
    assert "2 indexed files" in context
    assert "cortex-explorer" in context
    context_without_runtime_warning = context.split(" Parser runtime", maxsplit=1)[0]
    assert len(context_without_runtime_warning) <= 200


def test_session_start_hook_emits_stale_context(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    _seed_db(repo, fingerprint="old-fingerprint")

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)
    assert "Cortex index is stale" in context
    assert "2 indexed files" in context
    assert "refresh it incrementally" in context
    assert "cortex-explorer" in context
    assert "cortex_refresh" in context


def test_session_start_hook_reports_an_index_from_an_older_graph_format(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    _seed_db(repo, graph_version=0)  # fingerprint matches; only the format is old

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)
    _assert_outdated_text(context)


def test_session_start_hook_reads_a_pre_010_database_without_the_column(tmp_path: Path) -> None:
    import sqlite3

    repo = _write_repo(tmp_path)
    db = default_db_path(repo)
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    connection.executescript(
        "CREATE TABLE repos (repo_path TEXT PRIMARY KEY, updated_at INTEGER NOT NULL, fingerprint TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE sources (repo_path TEXT NOT NULL, path TEXT NOT NULL);"
    )
    connection.execute("INSERT INTO repos VALUES (?, 0, ?)", (str(repo.resolve()), compute_repo_fingerprint(repo)))
    connection.commit()
    connection.close()

    context = _additional_context(_run_hook(repo).stdout)

    _assert_outdated_text(context)


def test_session_start_hook_matcher_excludes_compaction() -> None:
    hook_config = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

    assert hook_config["hooks"]["SessionStart"][0]["matcher"] == "startup|resume|clear"


def test_session_start_hook_emits_missing_db_context(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)
    assert "No Cortex index found" in context
    assert "cortex_refresh can build it" in context


def test_session_start_hook_silent_outside_git_repo(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    (plain_dir / "notes.md").write_text("# Notes\n", encoding="utf-8")

    result = _run_hook(plain_dir)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_session_start_hook_fails_open_for_malformed_db(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    db_path = default_db_path(repo)
    db_path.parent.mkdir(parents=True)
    db_path.write_text("not sqlite", encoding="utf-8")

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
