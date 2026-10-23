#!/usr/bin/env python3
"""Freeze the resolved target of every contract call edge of the invariant fixtures.

    PYTHONPATH=/tmp/cortex-0121/src python scripts/perf/freeze_call_targets.py \
        tests/fixtures/calls_invariant/* --out tests/goldens/calls/invariant_0121.json

Writes ``{fixture: {"<source file>|<lineno>|<callee>": "<target>"}}``, sorted.  Run with the
0.12.1 sources (see plan rule 6) it produces the frozen reference that
``tests/test_call_target_invariant.py`` compares the current resolver against.  The module is
also imported by that test (``call_targets``), so both sides extract and resolve the same way.
Only the code on ``sys.path`` is used: the file that was imported is printed to stderr.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cortex.callresolve import is_contract_call, resolve_call_endpoints
from cortex.models import GraphNode
from cortex.structural import extract_structural_edges, supports_path

_SKIP_PARTS = {"__pycache__", ".git"}


def call_targets(fixture_dir: Path) -> dict[str, str]:
    """`{"<source>|<lineno>|<callee>": target}` for every contract call edge of one fixture."""
    paths = sorted(
        p.relative_to(fixture_dir).as_posix()
        for p in fixture_dir.rglob("*")
        if p.is_file() and not (set(p.relative_to(fixture_dir).parts) & _SKIP_PARTS)
    )
    known = set(paths)
    nodes: list[GraphNode] = []
    edges = []
    for path in paths:
        nodes.append(
            GraphNode(
                node_id=f"file:{path}", kind="file", label=path, source_ref=path,
                granularity="file", signature="", span_start=None, span_end=None, metadata={},
            )
        )
        if supports_path(path):
            result = extract_structural_edges(path, (fixture_dir / path).read_text(encoding="utf-8"), known)
            nodes.extend(result.nodes)
            edges.extend(result.edges)
    resolve_call_endpoints(nodes, edges)
    targets: dict[str, str] = {}
    for edge in edges:
        if not is_contract_call(edge):
            continue
        md = edge.metadata
        key = f"{md['source_file']}|{md['lineno']}|{md['callee']}"
        assert key not in targets, f"two call edges at one site in {fixture_dir.name}: {key}"
        targets[key] = edge.target
    return dict(sorted(targets.items()))


def main() -> int:
    import cortex

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixtures", nargs="+", type=Path, help="fixture directories")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(f"cortex imported from {cortex.__file__}", file=sys.stderr)
    frozen = {d.name: call_targets(d) for d in sorted(args.fixtures, key=lambda d: d.name) if d.is_dir()}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(frozen, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {sum(map(len, frozen.values()))} call targets for {len(frozen)} fixtures to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
