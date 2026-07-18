from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from .config import load_config
from .gitutils import collect_recent_commits, discover_repo_root
from .graph import (
    annotate_degree,
    build_cochange_layer,
    build_file_layer,
    build_graph,
    resolve_connect_endpoints,
)
from .hotspots import annotate_file_nodes, compute_churn, compute_hotspots
from .models import CommitRecord, GraphNode, SourceRecord
from .store import CortexStore, default_db_path, write_repo_meta

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

# Bound source reads to keep one tracked/generated file from exhausting memory
# or bloating the local source-content database.
MAX_SOURCE_BYTES = 5 * 1024 * 1024

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


def _is_safe_candidate(repo_root: Path, path: Path) -> bool:
    """Only index regular files physically contained by the repository.

    Tracked and untracked symlinks are excluded so a repository cannot point a
    text-looking path at credentials elsewhere on the machine and make Cortex
    copy them into its database or an LLM enrichment request.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        path.resolve(strict=True).relative_to(repo_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _read_source_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


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
            and _is_safe_candidate(repo_root, path)
        ]
    files: list[Path] = []
    for root, dirs, names in os.walk(repo_root):
        dirs[:] = [directory for directory in dirs if directory not in skip_dirs]
        files.extend(
            path
            for name in names
            if _is_safe_candidate(repo_root, path := Path(root) / name)
        )
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
        if stat.st_size > MAX_SOURCE_BYTES:
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
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_SOURCE_BYTES:
            continue
        content = _read_source_text(path)
        if content is None:
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        sources.append(
            SourceRecord(
                path=rel_path,
                content=content,
                kind=_classify_path(path),
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
                content_hash=_content_hash(content),
                mtime_ns=stat.st_mtime_ns,
            )
        )
    return sorted(sources, key=lambda item: item.path)


class IncrementalScan(NamedTuple):
    new_sources: list[SourceRecord]
    changed_sources: list[SourceRecord]
    # Stat changed (e.g. touched by a build tool) but content_hash matched the
    # stored hash — content-identical, so no graph rebuild is needed, but the
    # new (size, mtime_ns) must still be persisted or every future run would
    # keep re-reading this file. Counted as "unchanged" in the public result.
    restat_sources: list[SourceRecord]
    unchanged_count: int
    current_paths: set[str]
    fingerprint: str


def _scan_sources_incremental(
    repo_root: Path,
    existing_stats: dict[str, tuple[int, int, str]],
) -> IncrementalScan:
    """Stat-first scan (P0-3): a file is only opened and hashed when its
    (size_bytes, mtime_ns) differs from the stored record in `existing_stats`
    (as returned by CortexStore.fetch_source_stats). This makes an incremental
    refresh with N changed files do O(N) reads instead of O(repo) reads,
    while still doing one O(repo) stat() pass — needed regardless, to detect
    deletions and compute the repo fingerprint.

    Caveat: a file rewritten with identical size within the same mtime_ns
    tick (same nanosecond) would be missed by this check. That race is
    accepted here (same tradeoff other stat-first tools such as git/watchman
    make) — a full (non-incremental) ingest always re-reads and re-hashes
    everything and is the escape hatch when exact correctness is required.
    """
    new_sources: list[SourceRecord] = []
    changed_sources: list[SourceRecord] = []
    restat_sources: list[SourceRecord] = []
    unchanged_count = 0
    current_paths: set[str] = set()
    fingerprint_parts: list[str] = []

    for path in _iter_candidate_files(repo_root):
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > MAX_SOURCE_BYTES:
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        current_paths.add(rel_path)
        fingerprint_parts.append(f"{rel_path}\0{stat.st_size}\0{stat.st_mtime_ns}")

        stored = existing_stats.get(rel_path)
        if stored is not None and stored[0] == stat.st_size and stored[1] == stat.st_mtime_ns:
            unchanged_count += 1
            continue

        content = _read_source_text(path)
        if content is None:
            continue
        content_hash = _content_hash(content)
        record = SourceRecord(
            path=rel_path,
            content=content,
            kind=_classify_path(path),
            size_bytes=stat.st_size,
            modified_at=stat.st_mtime,
            content_hash=content_hash,
            mtime_ns=stat.st_mtime_ns,
        )
        if stored is None:
            new_sources.append(record)
        elif stored[2] == content_hash:
            # Stat moved (e.g. a re-checkout or a touch) but bytes didn't.
            restat_sources.append(record)
            unchanged_count += 1
        else:
            changed_sources.append(record)

    fingerprint = hashlib.sha256("\n".join(sorted(fingerprint_parts)).encode("utf-8")).hexdigest()
    return IncrementalScan(
        new_sources=sorted(new_sources, key=lambda item: item.path),
        changed_sources=sorted(changed_sources, key=lambda item: item.path),
        restat_sources=sorted(restat_sources, key=lambda item: item.path),
        unchanged_count=unchanged_count,
        current_paths=current_paths,
        fingerprint=fingerprint,
    )


def _commits_changed(store: CortexStore, repo_root: Path, commits: list[CommitRecord]) -> bool:
    """True when the freshly-collected commit SHAs differ from what's stored
    — the signal the incremental path uses to decide whether the COCHANGE
    layer (which depends only on commit history, not file contents) needs a
    rebuild at all (P0-3 item 4)."""
    stored_shas = {c.sha for c in store.fetch_commits(repo_root)}
    current_shas = {c.sha for c in commits}
    return stored_shas != current_shas


def _sync_semantic_embeddings(
    store: CortexStore,
    repo_root: Path,
    sources: list[SourceRecord],
    nodes: list[GraphNode],
    replace_paths: set[str] | list[str] | tuple[str, ...] = (),
) -> None:
    """Best-effort P1-7 indexing, isolated from the default ingest path."""
    try:
        from .semantic import index_embeddings

        index_embeddings(store, repo_root, sources, nodes, replace_paths=replace_paths)
    except Exception:
        # Optional dependencies, a corrupt local model, or a provider failure
        # must never turn the normal graph ingest into an error.
        return


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

    if incremental:
        # Stat-first: only the changed/new files are opened and hashed. The
        # full stat() walk (cheap — no file content read) is unavoidable
        # since it's how deletions and the fingerprint are detected.
        existing_stats = store.fetch_source_stats(repo_root)
        scan = _scan_sources_incremental(repo_root, existing_stats)
        deleted_paths = sorted(set(existing_stats) - scan.current_paths)
        stale_paths = sorted({s.path for s in scan.changed_sources} | set(deleted_paths))
        sources_to_process = scan.new_sources + scan.changed_sources

        commits = collect_recent_commits(repo_root, commit_limit)
        commits_changed = _commits_changed(store, repo_root, commits)
        hotspot_overrides = compute_hotspots(sources_to_process, commits)

        # Delta graph writes: delete only the rows owned by changed/deleted
        # files, then append fresh rows for the changed/new files. Rows for
        # every untouched file are never read or rewritten (P0-3 items 1-2).
        if sources_to_process or stale_paths:
            # P0-4: a signal/slot this batch's emit/connect/handler sites
            # reference may be declared in a file that isn't part of this
            # batch (e.g. an unchanged header). Fetch the store's existing
            # Qt symbol index before the delta delete below removes rows for
            # `stale_paths`, then strip those paths back out -- otherwise a
            # changed or deleted file's *old* declarations could leak in as
            # a stale resolution target for this batch, or a deleted file's
            # declarations could resolve to a node id that's about to stop
            # existing. Paths that changed (not deleted) still resolve
            # correctly: their freshly parsed data lives in `new_nodes` and
            # wins the per-path merge in build_file_layer.
            existing_qt_index = None
            if sources_to_process:
                existing_qt_index = store.fetch_qt_symbol_index(repo_root).without_paths(set(stale_paths))
            new_nodes, new_edges = build_file_layer(
                sources_to_process,
                scan.current_paths,
                existing_qt_index=existing_qt_index,
                connect_names=config.connect_functions,
            )
            annotate_file_nodes(new_nodes, hotspot_overrides)
            if stale_paths:
                store.delete_graph_for_sources(repo_root, stale_paths)
            store.append_graph(repo_root, new_nodes, new_edges)

        if scan.restat_sources:
            # Content identical, but (size, mtime_ns) moved — persist the new
            # stat so future runs don't keep re-reading this file.
            store.save_sources(repo_root, scan.restat_sources)
        if sources_to_process:
            store.save_sources(repo_root, sources_to_process)
        if deleted_paths:
            store.delete_sources(repo_root, deleted_paths)

        # P1-7: changed/deleted files own their embedding rows just like
        # sources and graph rows.  The helper deletes those rows even when no
        # local model is available, then returns without slowing the default
        # path or attempting any network access.
        if sources_to_process or stale_paths:
            _sync_semantic_embeddings(
                store,
                repo_root,
                sources_to_process,
                new_nodes,
                replace_paths=stale_paths,
            )

        # COCHANGE correctness (P0-3 item 4): co-change edges come from commit
        # history, not file contents, so they're rebuilt only when commits
        # actually changed -- or when a file was deleted, since a deleted
        # file's cochange/touches edges would otherwise dangle forever.
        if commits_changed or deleted_paths:
            store.delete_cochange_layer(repo_root)
            cochange_nodes, cochange_edges = build_cochange_layer(
                commits, scan.current_paths, filter_cochange_pairs=True
            )
            store.append_graph(repo_root, cochange_nodes, cochange_edges)
        if commits_changed:
            store.save_commits(repo_root, commits)

        # Recompute churn for retained file nodes only when the commit window
        # changed. Source-only edits already annotated their replacement nodes,
        # and uncommitted deletions cannot change retained-file churn; avoiding
        # this call keeps the P0-3 delta path O(changed) for ordinary refreshes.
        # Legacy graph rows without hotspot metadata are backfilled here on the
        # next commit refresh (or by a full ingest), not with an O(all-files)
        # probe on every source-only refresh.
        if commits_changed:
            store.update_file_hotspots(
                repo_root,
                compute_churn(commits),
                {path: int(values["complexity"]) for path, values in hotspot_overrides.items()},
            )

        # A `name:Cls::member` connects endpoint from this batch may resolve
        # against a symbol declared in an unchanged file (and vice versa), and
        # degree metadata spans the whole graph -- re-run both over the merged
        # store so an incremental refresh converges to exactly what a full
        # ingest would write, persisting only the rows that actually changed.
        if sources_to_process or stale_paths or commits_changed:
            merged_nodes, merged_edges = store.fetch_graph(repo_root)
            endpoints_before = {e.edge_id: (e.source, e.target) for e in merged_edges}
            degrees_before = {n.node_id: n.metadata.get("degree") for n in merged_nodes}
            resolved_edges = resolve_connect_endpoints(merged_nodes, merged_edges)
            annotate_degree(merged_nodes, resolved_edges)
            if len(resolved_edges) != len(endpoints_before):
                # Resolution dropped self-loop artifacts; an upsert cannot
                # remove rows, so rewrite the graph outright.
                store.replace_graph(repo_root, merged_nodes, resolved_edges)
            else:
                changed_edges = [
                    e for e in resolved_edges
                    if endpoints_before[e.edge_id] != (e.source, e.target)
                ]
                changed_nodes = [
                    n for n in merged_nodes
                    if degrees_before[n.node_id] != n.metadata.get("degree")
                ]
                if changed_nodes or changed_edges:
                    store.save_graph(repo_root, changed_nodes, changed_edges)

        store.set_repo_fingerprint(repo_root, scan.fingerprint)

        return {
            "repo_path": str(repo_root),
            "source_count": len(scan.current_paths),
            "new_files": len(scan.new_sources),
            "updated_files": len(scan.changed_sources),
            "deleted_files": len(deleted_paths),
            "unchanged_files": scan.unchanged_count,
            "commit_count": len(commits),
            "cochange_commits": len(commits),
        }

    all_sources = _scan_sources(repo_root, skip_dirs)
    fingerprint = compute_repo_fingerprint(repo_root, skip_dirs)
    commits = collect_recent_commits(repo_root, commit_limit)
    nodes, edges = build_graph(all_sources, commits, connect_names=config.connect_functions)

    store.reset_repo(repo_root, fingerprint=fingerprint)
    store.save_sources(repo_root, all_sources)
    store.save_commits(repo_root, commits)
    store.save_graph(repo_root, nodes, edges)
    _sync_semantic_embeddings(store, repo_root, all_sources, nodes, replace_paths=[source.path for source in all_sources])

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
