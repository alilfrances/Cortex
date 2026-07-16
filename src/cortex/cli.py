from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path

from .benchmark import format_benchmark, run_benchmark
from .bundle import generate_bundle
from .export import export_graphml, export_json, export_obsidian
from .gitutils import discover_repo_root
from .ingest import compute_repo_fingerprint, ingest_repository
from .integrations import (
    claude_status,
    codex_status,
    git_hook_status,
    install_git_hooks,
    migrate,
    uninstall_claude,
    uninstall_codex,
    uninstall_git_hooks,
)
from .report import generate_report, write_report
from .savings import compute_savings, format_savings
from .store import CortexStore, data_root, default_db_path
from .viewer import write_html


def _fetch_repo_graph(repo_path: Path, db_path: Path | None = None):
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    nodes, edges = store.fetch_graph(repo_root)
    communities = store.fetch_communities(repo_root)
    community_by_node: dict[str, int] = {}
    for community in communities:
        for node_id in community.node_ids:
            community_by_node[node_id] = community.community_id
    return repo_root, nodes, edges, community_by_node


def gc_data_dirs(prune: bool = False) -> dict:
    """Classify central data dirs by whether their source repo still exists."""
    result: dict[str, list[dict[str, str | None]]] = {
        "active": [],
        "orphaned": [],
        "unknown": [],
        "pruned": [],
    }
    base = data_root()
    if not base.is_dir():
        return result
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            result["unknown"].append({"dir": str(entry), "repo_path": None})
            continue
        try:
            repo_path = json.loads(meta_path.read_text(encoding="utf-8")).get("repo_path")
        except (json.JSONDecodeError, OSError):
            result["unknown"].append({"dir": str(entry), "repo_path": None})
            continue
        record = {"dir": str(entry), "repo_path": repo_path}
        if repo_path and Path(repo_path).is_dir():
            result["active"].append(record)
        else:
            result["orphaned"].append(record)
            if prune:
                shutil.rmtree(entry)
                result["pruned"].append(record)
    return result


def prune_query_caches() -> dict:
    """Prune the P1-3 `query_cache` table for every active central data dir.

    Unlike orphan-dir deletion (which needs `--prune` since it destroys an
    entire repo's index), this always runs as part of `cortex gc`: pruning
    the cache is cheap, bounded, and never loses anything an agent can't
    just recompute -- see `CortexStore.prune_query_cache` for the default
    retention (30 days / 200 rows per repo). Best-effort per repo: a
    corrupt or locked db is skipped rather than failing the whole `gc` run.
    """
    result: dict[str, list[dict[str, str | int]]] = {"pruned": []}
    base = data_root()
    if not base.is_dir():
        return result
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        db_path = entry / "cortex.db"
        if not meta_path.exists() or not db_path.exists():
            continue
        try:
            repo_path = json.loads(meta_path.read_text(encoding="utf-8")).get("repo_path")
        except (json.JSONDecodeError, OSError):
            continue
        if not repo_path:
            continue
        try:
            store = CortexStore(db_path)
            deleted = store.prune_query_cache(Path(repo_path))
        except Exception:
            continue
        if deleted:
            result["pruned"].append({"repo_path": repo_path, "rows_deleted": deleted})
    return result


def _watch_root(repo_path: Path) -> Path:
    try:
        return discover_repo_root(repo_path)
    except ValueError:
        return repo_path.resolve()


def _watch_polling(
    repo_path: Path,
    interval: float,
    refresh: Callable[[Path], None],
    sleep: Callable[[float], None] = time.sleep,
    max_refreshes: int | None = None,
) -> None:
    repo_root = _watch_root(repo_path)
    last_fingerprint = compute_repo_fingerprint(repo_root)
    refresh_count = 0
    while True:
        sleep(interval)
        current = compute_repo_fingerprint(repo_root)
        if current == last_fingerprint:
            continue
        last_fingerprint = current
        refresh(repo_root)
        refresh_count += 1
        if max_refreshes is not None and refresh_count >= max_refreshes:
            return


def _watch_with_watchdog(repo_path: Path, interval: float, refresh: Callable[[Path], None]) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    repo_root = _watch_root(repo_path)
    last_event = 0.0
    pending = False

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event) -> None:
            nonlocal last_event, pending
            if event.is_directory:
                return
            path = Path(event.src_path)
            if ".cortex" in path.parts or ".git" in path.parts:
                return
            last_event = time.monotonic()
            pending = True

    observer = Observer()
    observer.schedule(Handler(), str(repo_root), recursive=True)
    observer.start()
    print(f"Watching {repo_root} with watchdog. Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(min(interval, 0.5))
            if pending and time.monotonic() - last_event >= interval:
                pending = False
                refresh(repo_root)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        observer.stop()
        observer.join()


def watch_repository(repo_path: Path, interval: float = 30.0) -> None:
    def refresh(path: Path) -> None:
        summary = ingest_repository(path)
        print(json.dumps(summary, indent=2))

    try:
        import watchdog  # noqa: F401
    except Exception:
        print(f"Watching {_watch_root(repo_path)} by polling. Press Ctrl-C to stop.")
        try:
            _watch_polling(repo_path, interval, refresh)
        except KeyboardInterrupt:
            print("\nStopped.")
        return
    _watch_with_watchdog(repo_path, interval, refresh)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortex", description="Cortex context engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest a repository into Cortex state")
    ingest_parser.add_argument("repo_path", type=Path)
    ingest_parser.add_argument("--commits", type=int, default=50)
    ingest_parser.add_argument("--db", type=Path, default=None)
    ingest_parser.add_argument("--update", action="store_true", help="Incremental: only re-scan changed files")

    bundle_parser = subparsers.add_parser("bundle", help="Generate a token-budgeted retrieval bundle")
    bundle_parser.add_argument("repo_path", type=Path)
    bundle_parser.add_argument("--task", required=True)
    bundle_parser.add_argument("--budget", type=int, default=4000)
    bundle_parser.add_argument("--db", type=Path, default=None)
    bundle_parser.add_argument("--format", choices=("json", "md"), default="md")
    bundle_parser.add_argument("--rank", choices=("pagerank", "bfs"), default="pagerank")
    bundle_parser.add_argument("--hotspot-boost", action="store_true", help="Opt into churn×complexity ranking boost")

    report_parser = subparsers.add_parser("report", help="Generate a graph report")
    report_parser.add_argument("repo_path", type=Path)
    report_parser.add_argument("--db", type=Path, default=None)
    report_parser.add_argument("--out", type=Path, default=None)
    report_parser.add_argument("--include-test-pairs", action="store_true")

    graph_parser = subparsers.add_parser("graph", help="Export or view the Cortex graph")
    graph_subparsers = graph_parser.add_subparsers(dest="graph_action", required=True)
    graph_export_parser = graph_subparsers.add_parser("export", help="Export a stored Cortex graph")
    graph_export_parser.add_argument("repo_path", type=Path)
    graph_export_parser.add_argument("--format", choices=("graphml", "json", "obsidian"), required=True)
    graph_export_parser.add_argument("--out", type=Path, required=True)
    graph_export_parser.add_argument("--db", type=Path, default=None)
    graph_view_parser = graph_subparsers.add_parser("view", help="Write a self-contained HTML graph viewer")
    graph_view_parser.add_argument("repo_path", type=Path)
    graph_view_parser.add_argument("--out", type=Path, default=Path("cortex-graph.html"))
    graph_view_parser.add_argument("--db", type=Path, default=None)

    refresh_parser = subparsers.add_parser("refresh", help="Refresh Cortex state and write the default report")
    refresh_parser.add_argument("repo_path", type=Path, nargs="?", default=Path("."))
    refresh_parser.add_argument("--commits", type=int, default=50)
    refresh_parser.add_argument("--db", type=Path, default=None)

    gc_parser = subparsers.add_parser(
        "gc", help="List/prune orphaned data dirs and prune the per-repo query cache"
    )
    gc_parser.add_argument("--prune", action="store_true", help="Delete orphaned data dirs")

    benchmark_parser = subparsers.add_parser("benchmark", help="Measure token reduction against full-corpus reading")
    benchmark_parser.add_argument("repo_path", type=Path, nargs="?", default=Path("."))
    benchmark_parser.add_argument("--commits", type=int, default=50)
    benchmark_parser.add_argument("--budget", type=int, default=4000)
    benchmark_parser.add_argument("--db", type=Path, default=None)
    benchmark_parser.add_argument("--format", choices=("text", "json"), default="text")

    saved_parser = subparsers.add_parser("saved", help="Show token savings recorded from MCP tool usage")
    saved_parser.add_argument("repo_path", type=Path, nargs="?", default=Path("."))
    saved_parser.add_argument("--daily", action="store_true", help="Include a day-by-day rollup")
    saved_parser.add_argument("--format", choices=("text", "json"), default="text")
    saved_parser.add_argument("--db", type=Path, default=None)
    saved_parser.add_argument(
        "--price-per-mtok",
        default=None,
        help="Render dollar figures: '<input $/Mtok>,<output $/Mtok>' (no prices are hardcoded)",
    )

    subparsers.add_parser("mcp", help="Run the Cortex stdio MCP server")

    watch_parser = subparsers.add_parser("watch", help="Watch a repository and refresh Cortex on changes")
    watch_parser.add_argument("repo_path", type=Path)
    watch_parser.add_argument("--interval", type=float, default=30.0)

    enrich_parser = subparsers.add_parser("enrich", help="Run LLM semantic enrichment (requires cortex-context-engine[llm])")
    enrich_parser.add_argument("repo_path", type=Path)
    enrich_parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    enrich_parser.add_argument("--force", action="store_true", help="Re-extract all files, ignore cache")
    enrich_parser.add_argument("--db", type=Path, default=None)

    migrate_parser = subparsers.add_parser("migrate", help="Remove old Cortex injected agent guidance")
    migrate_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."))

    codex_parser = subparsers.add_parser("codex", help="Manage project-local Codex integration")
    codex_subparsers = codex_parser.add_subparsers(dest="codex_action", required=True)
    for action in ("uninstall", "status"):
        action_parser = codex_subparsers.add_parser(action)
        action_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."))

    claude_parser = subparsers.add_parser("claude", help="Manage project-local Claude integration")
    claude_subparsers = claude_parser.add_subparsers(dest="claude_action", required=True)
    for action in ("uninstall", "status"):
        action_parser = claude_subparsers.add_parser(action)
        action_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."))

    hook_parser = subparsers.add_parser("hook", help="Manage Cortex git hooks")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_action", required=True)
    for action in ("install", "uninstall", "status"):
        action_parser = hook_subparsers.add_parser(action)
        action_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "ingest":
        summary = ingest_repository(
            repo_path=args.repo_path,
            commit_limit=args.commits,
            db_path=args.db,
            incremental=args.update,
        )
        print(json.dumps(summary, indent=2))
        return

    if args.command == "bundle":
        bundle = generate_bundle(
            repo_path=args.repo_path,
            task=args.task,
            budget=args.budget,
            db_path=args.db,
            output_format=args.format,
            rank=args.rank,
            hotspot_boost=args.hotspot_boost,
        )
        if args.format == "json":
            print(json.dumps(bundle, indent=2))
        else:
            print(bundle)
        return

    if args.command == "report":
        report = generate_report(
            repo_path=args.repo_path,
            db_path=args.db,
            out_dir=args.out,
            include_test_pairs=args.include_test_pairs,
        )
        print(report)
        return

    if args.command == "graph":
        _, nodes, edges, communities = _fetch_repo_graph(args.repo_path, args.db)
        if args.graph_action == "export":
            if args.format == "graphml":
                export_graphml(nodes, edges, communities, args.out)
            elif args.format == "json":
                export_json(nodes, edges, communities, args.out)
            else:
                export_obsidian(nodes, edges, communities, args.out)
            print(json.dumps({"path": str(args.out), "format": args.format}, indent=2))
            return
        write_html(nodes, edges, communities, args.out)
        print(json.dumps({"path": str(args.out), "format": "html"}, indent=2))
        return

    if args.command == "refresh":
        summary = ingest_repository(repo_path=args.repo_path, commit_limit=args.commits, db_path=args.db)
        report_path = write_report(repo_path=args.repo_path, db_path=args.db)
        print(json.dumps({**summary, "report_path": str(report_path)}, indent=2))
        return

    if args.command == "gc":
        output = gc_data_dirs(prune=args.prune)
        output["query_cache"] = prune_query_caches()
        print(json.dumps(output, indent=2))
        return

    if args.command == "benchmark":
        result = run_benchmark(
            repo_path=args.repo_path,
            commit_limit=args.commits,
            budget=args.budget,
            db_path=args.db,
        )
        print(format_benchmark(result, output_format=args.format))
        return

    if args.command == "saved":
        price = None
        if args.price_per_mtok:
            parts = args.price_per_mtok.split(",")
            if len(parts) != 2:
                parser.error("--price-per-mtok must be '<input>,<output>'")
            try:
                price = (float(parts[0]), float(parts[1]))
            except ValueError:
                parser.error("--price-per-mtok values must be numeric")
        summary = compute_savings(args.repo_path, db_path=args.db, price_per_mtok=price)
        print(format_savings(summary, output_format=args.format, daily=args.daily))
        return

    if args.command == "mcp":
        os.execv(sys.executable, [sys.executable, "-m", "cortex.mcp.server"])

    if args.command == "watch":
        watch_repository(args.repo_path, interval=args.interval)
        return

    if args.command == "enrich":
        from .enrich import enrich_repository

        try:
            result = enrich_repository(
                repo_path=args.repo_path,
                provider_name=args.provider,
                db_path=args.db,
                force=args.force,
            )
            print(json.dumps(result, indent=2))
        except RuntimeError as exc:
            print(f"Error: {exc}")
        return

    if args.command == "migrate":
        print(json.dumps(migrate(args.project_dir), indent=2))
        return

    if args.command == "codex":
        if args.codex_action == "uninstall":
            print(json.dumps(uninstall_codex(args.project_dir), indent=2))
            return
        print(json.dumps(codex_status(args.project_dir), indent=2))
        return

    if args.command == "claude":
        if args.claude_action == "uninstall":
            print(json.dumps(uninstall_claude(args.project_dir), indent=2))
            return
        print(json.dumps(claude_status(args.project_dir), indent=2))
        return

    if args.command == "hook":
        if args.hook_action == "install":
            print(json.dumps(install_git_hooks(args.project_dir), indent=2))
            return
        if args.hook_action == "uninstall":
            print(json.dumps(uninstall_git_hooks(args.project_dir), indent=2))
            return
        print(json.dumps(git_hook_status(args.project_dir), indent=2))
        return

    parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
