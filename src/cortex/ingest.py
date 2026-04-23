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
    if path.suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".go", ".rs"}:
        return "code"
    return "text"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8', errors='replace')).hexdigest()


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
) -> dict[str, int | bool | str]:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    sources = _scan_sources(repo_root)
    commits = collect_recent_commits(repo_root, commit_limit)
    nodes, edges = build_graph(sources, commits)

    store.reset_repo(repo_root)
    store.save_sources(repo_root, sources)
    store.save_commits(repo_root, commits)
    store.save_graph(repo_root, nodes, edges)

    return {
        "repo_path": str(repo_root),
        "source_count": len(sources),
        "commit_count": len(commits),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "enrichment_enabled": enrich,
    }
