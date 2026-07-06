from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .gitutils import collect_recent_commits, discover_repo_root
from .graph import build_graph
from .models import SourceRecord
from .store import CortexStore, default_db_path

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
    ".go",
    ".rs",
    ".sh",
}

_SKIP_DIRS = {".git", ".cortex", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules"}


def _classify_path(path: Path) -> str:
    if path.suffix == ".md":
        return "markdown"
    if path.suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".rb", ".go", ".rs", ".sh"}:
        return "code"
    return "text"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


def compute_repo_fingerprint(repo_root: Path) -> str:
    parts: list[str] = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [directory for directory in dirs if directory not in _SKIP_DIRS]
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            rel_path = path.relative_to(repo_root).as_posix()
            parts.append(f"{rel_path}\0{stat.st_size}\0{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _scan_sources(repo_root: Path) -> list[SourceRecord]:
    sources: list[SourceRecord] = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [directory for directory in dirs if directory not in _SKIP_DIRS]
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            rel_path = str(path.relative_to(repo_root))
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


def ingest_repository(
    repo_path: Path,
    commit_limit: int = 50,
    db_path: Path | None = None,
    enrich: bool = False,
    incremental: bool = False,
) -> dict[str, int | bool | str]:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    all_sources = _scan_sources(repo_root)
    fingerprint = compute_repo_fingerprint(repo_root)
    commits = collect_recent_commits(repo_root, commit_limit)

    if incremental:
        existing = {s.path: s.content_hash for s in store.fetch_sources(repo_root)}
        new_sources = [s for s in all_sources if s.path not in existing]
        changed_sources = [
            s for s in all_sources
            if s.path in existing and s.content_hash != existing[s.path]
        ]
        unchanged_count = len(all_sources) - len(new_sources) - len(changed_sources)

        sources_to_process = new_sources + changed_sources

        if sources_to_process:
            existing_nodes, existing_edges = store.fetch_graph(repo_root)

            stale_paths = {s.path for s in changed_sources}
            filtered_nodes = [n for n in existing_nodes if n.source_ref not in stale_paths]
            filtered_edges = [
                e for e in existing_edges
                if e.metadata.get("source_file") not in stale_paths
            ]

            new_nodes, new_edges = build_graph(sources_to_process, commits)

            merged_nodes = filtered_nodes + new_nodes
            merged_edges = filtered_edges + new_edges

            store.save_sources(repo_root, sources_to_process)
            store.save_commits(repo_root, commits)
            store.save_graph(repo_root, merged_nodes, merged_edges)
        store.set_repo_fingerprint(repo_root, fingerprint)

        return {
            "repo_path": str(repo_root),
            "source_count": len(all_sources),
            "new_files": len(new_sources),
            "updated_files": len(changed_sources),
            "unchanged_files": unchanged_count,
            "commit_count": len(commits),
            "enrichment_enabled": enrich,
        }

    nodes, edges = build_graph(all_sources, commits)

    store.reset_repo(repo_root, fingerprint=fingerprint)
    store.save_sources(repo_root, all_sources)
    store.save_commits(repo_root, commits)
    store.save_graph(repo_root, nodes, edges)

    return {
        "repo_path": str(repo_root),
        "source_count": len(all_sources),
        "new_files": len(all_sources),
        "updated_files": 0,
        "unchanged_files": 0,
        "commit_count": len(commits),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "enrichment_enabled": enrich,
    }
