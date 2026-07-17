from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cortex.ingest import compute_repo_fingerprint
from cortex.models import SourceRecord
from cortex.store import CortexStore, default_db_path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "session-start.py"


def _write_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    (repo / "app.py").write_text("def main():\n    return 'ok'\n", encoding="utf-8")
    return repo


def _seed_db(repo: Path, fingerprint: str | None = None) -> None:
    store = CortexStore(default_db_path(repo))
    stored_fingerprint = fingerprint if fingerprint is not None else compute_repo_fingerprint(repo)
    store.reset_repo(repo, fingerprint=stored_fingerprint)
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


def test_session_start_hook_emits_fresh_context(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    _seed_db(repo)

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)
    assert "Cortex index exists and is fresh" in context
    assert "2 indexed files" in context
    assert "cortex_query" in context
    assert "cortex_search_symbols" in context
    assert "cortex_impact" in context
    assert "cortex-explorer" in context
    assert "single lookups direct" in context


def test_session_start_hook_emits_stale_context(tmp_path: Path) -> None:
    repo = _write_repo(tmp_path)
    _seed_db(repo, fingerprint="old-fingerprint")

    result = _run_hook(repo)

    assert result.returncode == 0
    assert result.stderr == ""
    context = _additional_context(result.stdout)
    assert "Cortex index exists but is stale" in context
    assert "2 indexed files" in context
    assert "cortex_refresh" in context


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
