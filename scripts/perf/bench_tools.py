#!/usr/bin/env python3
"""Time a full ingest, the overview analysis stages and the MCP tools on one repository.

Runs in-process with the per-call time budget disabled (as in field reports),
so every tool runs to completion. Use a fresh ``CORTEX_DATA_DIR`` so the
numbers are cold:

    CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --ingest
    CORTEX_DATA_DIR=/tmp/perf-data python scripts/perf/bench_tools.py /tmp/perf-repo --profile cortex_risk

``--profile TOOL`` prints a cProfile of one cold call instead of the table.
The repository is expected to come from ``scripts/perf/synthetic_repo.py``
(tool arguments name its symbols), but any indexed git repository works for
the ingest and analysis rows.
"""
from __future__ import annotations

import argparse
import cProfile
import json
import os
import pstats
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
os.environ["CORTEX_TOOL_TIMEOUT_SECONDS"] = "0"
if "--no-cache" in sys.argv:
    os.environ["CORTEX_QUERY_CACHE"] = "0"
    os.environ["CORTEX_GRAPH_CACHE"] = "0"

from cortex import community, deadcode, report  # noqa: E402
from cortex.ingest import ingest_repository  # noqa: E402
from cortex.store import CortexStore, default_db_path  # noqa: E402

TOOL_CALLS: list[tuple[str, dict]] = [
    ("cortex_overview", {}),
    ("cortex_overview", {}),
    ("cortex_dead_code", {}),
    ("cortex_dead_code", {"path": "src/mod17.cpp"}),
    ("cortex_query", {"task": "device state is wrong after reconnect in Mod17 hook handler"}),
    ("cortex_risk", {"range": "HEAD~3..HEAD"}),
    ("cortex_impact", {"path": "src/mod17.cpp"}),
    ("cortex_search_symbols", {"query": "Mod17"}),
    ("cortex_relations", {"symbol": "symbol:src/mod17.cpp:m3", "relation": "calls"}),
    ("cortex_references", {"symbol": "helper17"}),
    ("cortex_path", {"symbol_a": "symbol:src/mod17.cpp:m3", "symbol_b": "symbol:src/base1.cpp:hook1"}),
    ("cortex_read_symbol", {"symbol": "DefinitelyNotHelperXyz"}),
]


def _timed(label: str, fn):
    started = time.perf_counter()
    result = fn()
    print(f"{label:58s} {time.perf_counter() - started:8.2f}s", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--ingest", action="store_true", help="run a full ingest first")
    parser.add_argument("--profile", metavar="TOOL", help="cProfile one cold call of TOOL")
    parser.add_argument("--no-cache", action="store_true", help="disable the query and graph caches")
    parser.add_argument("--qt-edit", action="store_true", help="commit an edit to src/qt/widget4.h, then time cortex_risk on it")
    args = parser.parse_args()
    repo = args.repo.resolve()

    if args.ingest:
        _timed("ingest (full)", lambda: ingest_repository(repo))

    from cortex.mcp.tools import call_tool

    if args.profile:
        arguments = next((a for name, a in TOOL_CALLS if name == args.profile), {})
        profiler = cProfile.Profile()
        started = time.perf_counter()
        profiler.enable()
        call_tool(args.profile, {"repo_path": str(repo), **arguments})
        profiler.disable()
        print(f"{args.profile} {time.perf_counter() - started:.2f}s")
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(35)
        return

    if args.qt_edit:
        header = repo / "src" / "qt" / "widget4.h"
        lines = header.read_text().splitlines()
        lines[8] = lines[8] + "  // edited"
        header.write_text("\n".join(lines) + "\n")
        subprocess.run(["git", "commit", "-qam", "edit widget4"], cwd=repo, check=True, capture_output=True)
        TOOL_CALLS.append(("cortex_risk", {"range": "HEAD~1..HEAD"}))

    store = CortexStore(default_db_path(repo))
    nodes, edges = _timed("fetch_graph", lambda: store.fetch_graph(repo))
    relations = Counter(edge.relation for edge in edges).most_common(6)
    print(f"  nodes={len(nodes)} edges={len(edges)} {dict(relations)}")
    _timed("detect_communities", lambda: community.detect_communities(nodes, edges))
    _timed("analyze_dead_code (graph given)", lambda: deadcode.analyze_dead_code(repo, store=store, nodes=nodes, edges=edges))
    _timed("build_report_data", lambda: report.build_report_data(repo))
    for name, arguments in TOOL_CALLS:
        result = _timed(
            f"{name} {json.dumps(arguments)[:40]}",
            lambda: call_tool(name, {"repo_path": str(repo), **arguments}),
        )
        text = result["content"][0]["text"]
        if result.get("isError"):
            print(f"  error: {text[:160]}")


if __name__ == "__main__":
    main()
