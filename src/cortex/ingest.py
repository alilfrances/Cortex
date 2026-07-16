from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from .config import load_config
from .gitutils import collect_recent_commits, discover_repo_root
from .graph import annotate_degree, build_graph, resolve_connect_endpoints
from .models import SourceRecord
from .store import CortexStore, default_db_path, write_repo_meta

_T = TypeVar("_T")

_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".swift",
    ".java",
    ".rb",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cxx",
    ".hpp",
    ".hh",
    ".hxx",
    ".qml",
    ".go",
    ".rs",
    ".sh",
    ".cmake",
    ".qrc",
    ".ui",
    ".pro",
}

_SKIP_DIRS = {
    ".git",
    ".cortex",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
    "dist-check",
    ".venv",
    "venv",
    ".tox",
    ".eggs",
}


def _git_listed_files(repo_root: Path) -> list[Path] | None:
    """Tracked + untracked-but-not-gitignored files, or None outside a git repo."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [repo_root / rel for rel in result.stdout.split("\0") if rel]


def _iter_candidate_files(repo_root: Path, skip_dirs: set[str] | None = None) -> list[Path]:
    if skip_dirs is None:
        skip_dirs = _SKIP_DIRS | set(load_config(repo_root).skip_dirs)
    listed = _git_listed_files(repo_root)
    if listed is not None:
        return [
            path for path in listed
            if not set(path.relative_to(repo_root).parts[:-1]) & skip_dirs
        ]
    files: list[Path] = []
    for root, dirs, names in os.walk(repo_root):
        dirs[:] = [directory for directory in dirs if directory not in skip_dirs]
        files.extend(Path(root) / name for name in names)
    return files


def _classify_path(path: Path) -> str:
    if path.suffix == ".md":
        return "markdown"
    if path.name.lower() == "cmakelists.txt" or path.suffix in {".cmake", ".qrc"}:
        return "code"
    if path.suffix in {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".swift",
        ".java",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".cxx",
        ".hpp",
        ".hh",
        ".hxx",
        ".qml",
        ".go",
        ".rs",
        ".sh",
    }:
        return "code"
    return "text"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


def compute_repo_fingerprint(repo_root: Path, skip_dirs: set[str] | None = None) -> str:
    parts: list[str] = []
    for path in _iter_candidate_files(repo_root, skip_dirs):
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        parts.append(f"{rel_path}\0{stat.st_size}\0{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _scan_sources(repo_root: Path, skip_dirs: set[str] | None = None) -> list[SourceRecord]:
    sources: list[SourceRecord] = []
    for path in _iter_candidate_files(repo_root, skip_dirs):
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        stat = path.stat()
        sources.append(
            SourceRecord(
                path=rel_path,
                content=content,
                kind=_classify_path(path),
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
                content_hash=_content_hash(content),
            )
        )
    return sorted(sources, key=lambda item: item.path)


def _dedupe_by_id(items: list[_T], key: Callable[[_T], str]) -> list[_T]:
    seen: set[str] = set()
    unique: list[_T] = []
    for item in items:
        item_id = key(item)
        if item_id in seen:
            continue
        seen.add(item_id)
        unique.append(item)
    return unique


def ingest_repository(
    repo_path: Path,
    commit_limit: int = 1000,
    db_path: Path | None = None,
    incremental: bool = False,
) -> dict[str, int | bool | str]:
    repo_root = discover_repo_root(repo_path)
    config = load_config(repo_root)
    skip_dirs = _SKIP_DIRS | set(config.skip_dirs)
    store = CortexStore(db_path or default_db_path(repo_root))
    write_repo_meta(store.db_path, repo_root)
    all_sources = _scan_sources(repo_root, skip_dirs)
    fingerprint = compute_repo_fingerprint(repo_root, skip_dirs)
    commits = collect_recent_commits(repo_root, commit_limit)

    if incremental:
        existing = {s.path: s.content_hash for s in store.fetch_sources(repo_root)}
        current_paths = {s.path for s in all_sources}
        new_sources = [s for s in all_sources if s.path not in existing]
        changed_sources = [
            s for s in all_sources
            if s.path in existing and s.content_hash != existing[s.path]
        ]
        deleted_paths = sorted(set(existing) - current_paths)
        unchanged_count = len(all_sources) - len(new_sources) - len(changed_sources)

        sources_to_process = new_sources + changed_sources

        if sources_to_process or deleted_paths:
            existing_nodes, existing_edges = store.fetch_graph(repo_root)

            stale_paths = {s.path for s in changed_sources} | set(deleted_paths)
            # COCHANGE edges and commit nodes are fully rebuilt from `commits`
            # each run, so retained copies would duplicate on every refresh.
            filtered_nodes = [
                n for n in existing_nodes
                if n.source_ref not in stale_paths and n.kind != "commit"
            ]
            filtered_edges = [
                e for e in existing_edges
                if e.metadata.get("source_file") not in stale_paths
                and e.layer != "COCHANGE"
            ]

            new_nodes, new_edges = build_graph(
                sources_to_process,
                commits,
                all_paths=current_paths,
                connect_names=config.connect_functions,
            )

            merged_nodes = _dedupe_by_id(filtered_nodes + new_nodes, lambda n: n.node_id)
            merged_edges = _dedupe_by_id(filtered_edges + new_edges, lambda e: e.edge_id)
            merged_edges = resolve_connect_endpoints(merged_nodes, merged_edges)
            annotate_degree(merged_nodes, merged_edges)

            store.save_sources(repo_root, sources_to_process)
            store.delete_sources(repo_root, deleted_paths)
            store.save_commits(repo_root, commits)
            store.replace_graph(repo_root, merged_nodes, merged_edges)
        store.set_repo_fingerprint(repo_root, fingerprint)

        return {
            "repo_path": str(repo_root),
            "source_count": len(all_sources),
            "new_files": len(new_sources),
            "updated_files": len(changed_sources),
            "deleted_files": len(deleted_paths),
            "unchanged_files": unchanged_count,
            "commit_count": len(commits),
            "cochange_commits": len(commits),
        }

    nodes, edges = build_graph(all_sources, commits, connect_names=config.connect_functions)

    store.reset_repo(repo_root, fingerprint=fingerprint)
    store.save_sources(repo_root, all_sources)
    store.save_commits(repo_root, commits)
    store.save_graph(repo_root, nodes, edges)

    return {
        "repo_path": str(repo_root),
        "source_count": len(all_sources),
        "new_files": len(all_sources),
        "updated_files": 0,
        "deleted_files": 0,
        "unchanged_files": 0,
        "commit_count": len(commits),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "cochange_commits": len(commits),
    }
