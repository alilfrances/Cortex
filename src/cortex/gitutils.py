from __future__ import annotations

import subprocess
from pathlib import Path

from .models import CommitRecord


def discover_repo_root(repo_path: Path) -> Path:
    candidate = repo_path.resolve()
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"{candidate} is not inside a git repository")
    return Path(result.stdout.strip())


def detect_repo_root(repo_path: Path | str) -> Path:
    return discover_repo_root(Path(repo_path))


def collect_recent_commits(repo_path: Path, limit: int) -> list[CommitRecord]:
    if limit <= 0:
        return []

    format_string = "%H%x1f%an%x1f%at%x1f%s"
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", f"-{limit}", f"--pretty=format:{format_string}", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []

    records: list[CommitRecord] = []
    blocks = result.stdout.strip().split("\n\n")
    for block in blocks:
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        sha, author, authored_at, summary = lines[0].split("\x1f", maxsplit=3)
        records.append(
            CommitRecord(
                sha=sha,
                author=author,
                authored_at=int(authored_at),
                summary=summary,
                files=lines[1:],
            )
        )
    return records
