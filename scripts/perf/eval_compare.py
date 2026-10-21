#!/usr/bin/env python3
"""Run the gold-task evals with fixed git dates and compare two runs.

    python scripts/perf/eval_compare.py before.json            # write a run
    python scripts/perf/eval_compare.py --compare before.json after.json

Eval gate used by the 0.12 plan: a ranking change is acceptable only if no row's
recall or file_recall decreases and each metric's mean is >= the baseline's.
Latency and token columns are ignored; they are environment-sensitive.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
METRICS = ("precision", "precision_at_3", "recall", "file_recall", "symbol_recall")
GATED = ("recall", "file_recall")


def run(out: Path) -> None:
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = "2024-01-01T00:00:00+00:00"
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    from evals.run_evals import run_evals

    with tempfile.TemporaryDirectory() as tmp:
        _, rows = run_evals(Path(tmp) / "RESULTS.md", stdlib_tokens=True, oss=False)
    data = {
        f"{row['task'].repo}|{row['task'].description}|{row['mode']}": {
            **{metric: row[metric] for metric in METRICS},
            "files": list(row["files"]),
        }
        for row in rows
    }
    out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def compare(before: Path, after: Path) -> int:
    a, b = json.loads(before.read_text()), json.loads(after.read_text())
    failed = False
    for key in sorted(set(a) | set(b)):
        if key not in a or key not in b:
            print(f"only in {'after' if key in b else 'before'}: {key}")
            continue
        changed = {m: (a[key][m], b[key][m]) for m in METRICS if a[key][m] != b[key][m]}
        if changed or a[key]["files"] != b[key]["files"]:
            print(f"changed: {key}\n    {changed or 'files only'}")
        failed |= any(b[key][m] < a[key][m] for m in GATED)
    common = sorted(set(a) & set(b))  # rows added or removed between the runs are listed above
    for metric in METRICS:
        mean_a = statistics.mean(a[key][metric] for key in common)
        mean_b = statistics.mean(b[key][metric] for key in common)
        print(f"mean {metric}: {mean_a:.4f} -> {mean_b:.4f}")
        failed |= metric in GATED and mean_b < mean_a
    print("EVAL GATE:", "FAIL" if failed else "pass")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    if args.compare:
        raise SystemExit(compare(*args.paths))
    run(args.paths[0])


if __name__ == "__main__":
    main()
