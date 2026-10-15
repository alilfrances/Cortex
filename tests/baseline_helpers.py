"""Shared builders for the Task 0 baseline characterization tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from cortex.gitutils import collect_recent_commits
from cortex.ingest import _classify_path, ingest_repository
from cortex.mcp.tools import call_tool
from cortex.models import CommitRecord, SourceRecord

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_SKIP_PARTS = {".cortex", "__pycache__", ".git"}

FIXED_GIT_DATE = "2026-01-01T00:00:00+0000"


def fixed_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every git commit made by a builder reproducible (same dates)."""
    monkeypatch.setenv("GIT_AUTHOR_DATE", FIXED_GIT_DATE)
    monkeypatch.setenv("GIT_COMMITTER_DATE", FIXED_GIT_DATE)
    # Never let a developer's global git config (signing, hooks) change fixtures.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def init_git(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "baseline@example.test")
    git(repo, "config", "user.name", "Baseline")
    git(repo, "config", "commit.gpgsign", "false")


def commit_all(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def fixture_files(fixture_dir: Path) -> list[str]:
    """Repo-relative posix paths of every fixture file, sorted."""
    return sorted(
        path.relative_to(fixture_dir).as_posix()
        for path in fixture_dir.rglob("*")
        if path.is_file() and not (set(path.relative_to(fixture_dir).parts) & _SKIP_PARTS)
    )


def read_sources(root: Path, relpaths: list[str]) -> list[SourceRecord]:
    sources: list[SourceRecord] = []
    for rel in relpaths:
        path = root / rel
        content = path.read_text(encoding="utf-8")
        sources.append(
            SourceRecord(
                path=rel,
                content=content,
                kind=_classify_path(path),
                size_bytes=len(content.encode("utf-8")),
                modified_at=0.0,
            )
        )
    return sources


def normalised_commits(repo: Path, limit: int = 50) -> list[CommitRecord]:
    """Real history with run-specific shas/timestamps replaced by fixed values."""
    commits = collect_recent_commits(repo, limit)
    total = len(commits)
    return [
        CommitRecord(
            sha=f"{total - index:040x}",
            summary=commit.summary,
            author=commit.author,
            authored_at=1_700_000_000 + (total - index) * 60,
            files=list(commit.files),
        )
        for index, commit in enumerate(commits)
    ]


def copy_fixture_repo(fixture_dir: Path, dest: Path) -> Path:
    """Copy a checked-in fixture directory into ``dest`` as a one-commit git repo."""
    shutil.copytree(
        fixture_dir,
        dest,
        ignore=shutil.ignore_patterns(".cortex", "__pycache__"),
    )
    init_git(dest)
    commit_all(dest, "import fixture")
    return dest


def ingest(repo: Path, commit_limit: int = 20) -> dict[str, Any]:
    return ingest_repository(repo, commit_limit=commit_limit)


def tool(name: str, **arguments: Any) -> tuple[dict[str, Any], bool]:
    """Call an MCP tool in-process; returns ``(decoded payload, isError)``."""
    result = call_tool(name, arguments)
    return json.loads(result["content"][0]["text"]), bool(result["isError"])


def current_backend() -> str:
    """The structural backend the dispatcher will use for this process."""
    return "regex" if os.environ.get("CORTEX_FORCE_REGEX") == "1" else "treesitter"
