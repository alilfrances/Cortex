from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.ingest import MAX_SOURCE_BYTES, _scan_sources, compute_repo_fingerprint


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_skips_build_dist_and_venv_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "src" / "mod.py", "def real(): pass\n")
    for junk_dir in ("build/lib", "dist", "dist-check", ".venv/lib", "venv/lib", ".tox/py311", ".eggs/pkg"):
        _write(repo / junk_dir / "junk.py", "def stale(): pass\n")

    paths = {source.path for source in _scan_sources(repo)}

    assert paths == {"src/mod.py"}


def test_scan_skips_gitignored_paths_in_git_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    _write(repo / ".gitignore", "generated/\nsecret.py\n")
    _write(repo / "app.py", "def main(): pass\n")
    _write(repo / "generated" / "gen.py", "def generated(): pass\n")
    _write(repo / "secret.py", "TOKEN = 'x'\n")

    paths = {source.path for source in _scan_sources(repo)}

    assert "app.py" in paths
    assert "generated/gen.py" not in paths
    assert "secret.py" not in paths


def test_scan_never_follows_symlinks_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    secret = tmp_path / "secret.py"
    _write(secret, "API_TOKEN = 'outside-secret'\n")
    link = repo / "linked.py"
    try:
        link.symlink_to(secret)
    except OSError:
        return
    subprocess.run(["git", "add", "linked.py"], cwd=repo, capture_output=True, check=True)

    before = compute_repo_fingerprint(repo)
    _write(secret, "API_TOKEN = 'rotated-secret'\n")

    assert _scan_sources(repo) == []
    assert compute_repo_fingerprint(repo) == before


def test_scan_skips_oversized_text_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "small.py", "print('ok')\n")
    _write(repo / "large.py", "x" * (MAX_SOURCE_BYTES + 1))

    paths = {source.path for source in _scan_sources(repo)}

    assert paths == {"small.py"}


def test_fingerprint_unchanged_by_excluded_dirs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "src" / "mod.py", "def real(): pass\n")

    before = compute_repo_fingerprint(repo)
    _write(repo / "build" / "lib" / "mod.py", "def stale(): pass\n")
    after = compute_repo_fingerprint(repo)

    assert before == after


def test_fingerprint_unchanged_by_gitignored_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    _write(repo / ".gitignore", "generated/\n")
    _write(repo / "app.py", "def main(): pass\n")

    before = compute_repo_fingerprint(repo)
    _write(repo / "generated" / "gen.py", "def generated(): pass\n")
    after = compute_repo_fingerprint(repo)

    assert before == after
