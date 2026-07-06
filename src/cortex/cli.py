from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmark import format_benchmark, run_benchmark
from .bundle import generate_bundle
from .ingest import ingest_repository
from .integrations import (
    claude_status,
    codex_status,
    git_hook_status,
    install_claude,
    install_codex,
    install_git_hooks,
    install_global_skill,
    uninstall_claude,
    uninstall_codex,
    uninstall_git_hooks,
)
from .report import generate_report, write_report


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

    report_parser = subparsers.add_parser("report", help="Generate a graph report")
    report_parser.add_argument("repo_path", type=Path)
    report_parser.add_argument("--db", type=Path, default=None)
    report_parser.add_argument("--out", type=Path, default=None)
    report_parser.add_argument("--include-test-pairs", action="store_true")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh Cortex state and write the default report")
    refresh_parser.add_argument("repo_path", type=Path, nargs="?", default=Path("."))
    refresh_parser.add_argument("--commits", type=int, default=50)
    refresh_parser.add_argument("--db", type=Path, default=None)

    benchmark_parser = subparsers.add_parser("benchmark", help="Measure token reduction against full-corpus reading")
    benchmark_parser.add_argument("repo_path", type=Path, nargs="?", default=Path("."))
    benchmark_parser.add_argument("--commits", type=int, default=50)
    benchmark_parser.add_argument("--budget", type=int, default=4000)
    benchmark_parser.add_argument("--db", type=Path, default=None)
    benchmark_parser.add_argument("--format", choices=("text", "json"), default="text")

    enrich_parser = subparsers.add_parser("enrich", help="Run LLM semantic enrichment (requires cortex-context-engine[llm])")
    enrich_parser.add_argument("repo_path", type=Path)
    enrich_parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    enrich_parser.add_argument("--force", action="store_true", help="Re-extract all files, ignore cache")
    enrich_parser.add_argument("--db", type=Path, default=None)

    install_parser = subparsers.add_parser("install", help="Install Cortex global skill files for an assistant")
    install_parser.add_argument("platform", choices=("codex", "claude"))

    codex_parser = subparsers.add_parser("codex", help="Manage project-local Codex integration")
    codex_subparsers = codex_parser.add_subparsers(dest="codex_action", required=True)
    for action in ("install", "uninstall", "status"):
        action_parser = codex_subparsers.add_parser(action)
        action_parser.add_argument("project_dir", type=Path, nargs="?", default=Path("."))

    claude_parser = subparsers.add_parser("claude", help="Manage project-local Claude integration")
    claude_subparsers = claude_parser.add_subparsers(dest="claude_action", required=True)
    for action in ("install", "uninstall", "status"):
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

    if args.command == "refresh":
        summary = ingest_repository(repo_path=args.repo_path, commit_limit=args.commits, db_path=args.db)
        report_path = write_report(repo_path=args.repo_path, db_path=args.db)
        print(json.dumps({**summary, "report_path": str(report_path)}, indent=2))
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

    if args.command == "install":
        print(json.dumps(install_global_skill(args.platform), indent=2))
        return

    if args.command == "codex":
        if args.codex_action == "install":
            print(json.dumps(install_codex(args.project_dir), indent=2))
            return
        if args.codex_action == "uninstall":
            print(json.dumps(uninstall_codex(args.project_dir), indent=2))
            return
        print(json.dumps(codex_status(args.project_dir), indent=2))
        return

    if args.command == "claude":
        if args.claude_action == "install":
            print(json.dumps(install_claude(args.project_dir), indent=2))
            return
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
