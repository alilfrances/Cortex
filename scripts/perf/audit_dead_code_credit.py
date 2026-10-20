#!/usr/bin/env python3
"""Sample symbols that are NOT dead-code candidates only because of a resolved call edge.

Since 0.10.0 resolved `calls` edges credit a symbol as used. If the resolver ever
links a call to the wrong symbol, that symbol silently disappears from dead-code
results. This prints a seeded random sample of symbols whose only incoming
edges are `calls`, with the call site, so a person can check the edge is real:

    CORTEX_DATA_DIR=... python scripts/perf/audit_dead_code_credit.py /path/to/indexed/repo --sample 50

No product code is changed; it only reads the index.
"""
from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cortex.references import _call_site  # noqa: E402
from cortex.store import CortexStore, default_db_path  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--sample", type=int, default=50)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    repo = args.repo.resolve()
    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)
    sources = {source.path: source.content.splitlines() for source in store.fetch_sources(repo)}
    by_id = {node.node_id: node for node in nodes}

    incoming: dict[str, list] = defaultdict(list)
    for edge in edges:
        if edge.relation in {"calls", "imports", "inherits", "references", "reads", "writes", "aliases", "exports"}:
            incoming[edge.target].append(edge)
    credited_only_by_calls = [
        node for node in nodes
        if node.granularity == "symbol"
        and incoming.get(node.node_id)
        and all(edge.relation == "calls" for edge in incoming[node.node_id])
    ]
    print(f"{len(credited_only_by_calls)} symbols are credited only by resolved call edges")
    random.Random(args.seed).shuffle(credited_only_by_calls)
    for node in credited_only_by_calls[: args.sample]:
        edge = incoming[node.node_id][0]
        caller = by_id.get(edge.source)
        site = _call_site(edge) or (caller.source_ref if caller else "?", None)
        text = ""
        if isinstance(site[1], int) and site[0] in sources and 0 < site[1] <= len(sources[site[0]]):
            text = sources[site[0]][site[1] - 1].strip()
        print(f"\n{node.source_ref}:{node.span_start} {node.label}  <-  {caller.label if caller else edge.source} @ {site[0]}:{site[1]}")
        print(f"    {text}")
        print(f"    resolution={edge.metadata.get('resolution')} confidence={edge.confidence}")


if __name__ == "__main__":
    main()
