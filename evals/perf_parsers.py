#!/usr/bin/env python3
"""Small reproducible regex/Tree-sitter and QML parser benchmark."""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from cortex.structural import extract_structural_edges  # noqa: E402


SAMPLE = """import QtQuick 2.15\nItem {\n id: root\n required property string title\n property int count: 1\n signal changed(string value)\n function update(value) { count = value; changed(title) }\n Rectangle { id: child; width: root.count }\n onChanged: console.log(title)\n}\n"""


def _measure(backend: str, repeats: int) -> float:
    old = os.environ.get("CORTEX_FORCE_REGEX")
    if backend == "regex":
        os.environ["CORTEX_FORCE_REGEX"] = "1"
    else:
        os.environ.pop("CORTEX_FORCE_REGEX", None)
    try:
        started = time.perf_counter()
        for _ in range(repeats):
            extract_structural_edges("Bench.qml", SAMPLE, {"Bench.qml"})
        return (time.perf_counter() - started) / repeats
    finally:
        if old is None:
            os.environ.pop("CORTEX_FORCE_REGEX", None)
        else:
            os.environ["CORTEX_FORCE_REGEX"] = old


def run(repeats: int = 30) -> dict[str, object]:
    regex = _measure("regex", repeats)
    treesitter = _measure("treesitter", repeats)
    ratio = treesitter / regex if regex else 0.0
    # Cold setup is intentionally not included: runtime status/bootstrapping is
    # reported separately by the runtime CLI and is not query latency.
    return {
        "source_bytes": len(SAMPLE.encode()),
        "repeats": repeats,
        "regex_seconds": round(regex, 6),
        "treesitter_seconds": round(treesitter, 6),
        "treesitter_to_regex_ratio": round(ratio, 3),
        "cached_startup_seconds": None,
        "cold_bootstrap_separate": True,
        "passed": treesitter < max(0.01, regex * 2.0) or treesitter < 0.01,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assert", dest="assertions", action="store_true")
    parser.add_argument("--repeats", type=int, default=30)
    args = parser.parse_args()
    result = run(max(1, args.repeats))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not args.assertions or result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
