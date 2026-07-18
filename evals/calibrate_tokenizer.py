#!/usr/bin/env python3
"""Dev-time tokenizer calibration (P1-4).

Compares Cortex's stdlib regex-segment token estimate
(`cortex.tokenizer.raw_segment_count`) against `tiktoken`'s o200k_base BPE
encoding across a repo's own sources, grouped by `SourceRecord.kind`
("code"/"markdown"/"text" -- the same classification `cortex.ingest` and
`cortex.bundle` use). Emits a per-kind ratio table; the printed "measured
factor" column is what should be pasted into `cortex.tokenizer.CALIBRATION`.

This is a DEV-TIME measurement only -- `tiktoken` is not a runtime
dependency of Cortex (see the optional `[tokens]` extra in pyproject.toml).
Install it just to run this script:

    pip install tiktoken
    python3 evals/calibrate_tokenizer.py [repo_path]

`repo_path` defaults to this repo's own root, since Cortex's own corpus
(Python source, Markdown docs, misc text/config) is a reasonable proxy for
the kind of repos Cortex is used against. Degrades with a clear message
(exit code 1) if `tiktoken` isn't installed -- it never silently falls back
to a fake ratio.

The measurement is deliberately taken against the stdlib segmenter
(`re`), not the optional third-party `regex` package (also a
`raw_segment_count` soft-import), even if `regex` happens to be installed
in the dev environment running this script: CALIBRATION must be accurate
for the dependency-free default install. The script enables tokenizer.py's
dev/eval-only stdlib-segmenter override for the measurement.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load_tiktoken():
    try:
        import tiktoken  # type: ignore
    except ImportError:
        print(
            "tiktoken is not installed. This is expected for Cortex's default\n"
            "(stdlib-only) install -- it is a dev-time-only measurement tool, not\n"
            "a runtime dependency. Install it to run this script:\n\n"
            "    pip install tiktoken\n\n"
            "or `pip install cortex-context-engine[tokens]` in an editable checkout.",
            file=sys.stderr,
        )
        sys.exit(1)
    return tiktoken.get_encoding("o200k_base")


def main() -> None:
    repo_path = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
    encoding = _load_tiktoken()

    from cortex import tokenizer as tokenizer_mod  # noqa: E402
    from cortex.ingest import _scan_sources  # noqa: E402

    tokenizer_mod._force_stdlib_segments = True
    raw_segment_count = tokenizer_mod.raw_segment_count

    sources = _scan_sources(repo_path)
    if not sources:
        print(f"No sources found under {repo_path}. Nothing to calibrate.", file=sys.stderr)
        sys.exit(1)

    per_kind_heuristic: dict[str, int] = defaultdict(int)
    per_kind_exact: dict[str, int] = defaultdict(int)
    per_kind_file_ratios: dict[str, list[float]] = defaultdict(list)
    per_kind_files: dict[str, int] = defaultdict(int)

    for source in sources:
        if not source.content:
            continue
        heuristic = raw_segment_count(source.content)
        exact = len(encoding.encode(source.content, disallowed_special=()))
        if heuristic <= 0:
            continue
        per_kind_heuristic[source.kind] += heuristic
        per_kind_exact[source.kind] += exact
        per_kind_file_ratios[source.kind].append(exact / heuristic)
        per_kind_files[source.kind] += 1

    print(f"Corpus: {repo_path}")
    print(f"Files scanned: {sum(per_kind_files.values())}\n")
    header = f"{'kind':<10}{'files':>7}{'raw_segments':>14}{'tiktoken':>10}{'factor':>9}{'mean_file_ratio':>17}{'error_%':>9}"
    print(header)
    print("-" * len(header))

    measured: dict[str, float] = {}
    for kind in sorted(per_kind_heuristic):
        heuristic_total = per_kind_heuristic[kind]
        exact_total = per_kind_exact[kind]
        factor = exact_total / heuristic_total if heuristic_total else 0.0
        mean_ratio = mean(per_kind_file_ratios[kind]) if per_kind_file_ratios[kind] else 0.0
        # Error of the *aggregate* factor vs. tiktoken ground truth, i.e. how
        # far off `round(heuristic_total * factor)` is from `exact_total` --
        # 0% by construction for the aggregate; the interesting number is the
        # per-file spread (mean_file_ratio vs factor) which the table also
        # reports so a caller can judge how noisy a single-file estimate is.
        per_file_spread_pct = (
            100.0 * (mean_ratio - factor) / factor if factor else 0.0
        )
        measured[kind] = round(factor, 2)
        print(
            f"{kind:<10}{per_kind_files[kind]:>7}{heuristic_total:>14}{exact_total:>10}"
            f"{factor:>9.3f}{mean_ratio:>17.3f}{per_file_spread_pct:>8.1f}%"
        )

    print(
        "\n'factor' = sum(tiktoken tokens) / sum(raw regex segments) per kind -- this is\n"
        "the CALIBRATION value: count_text_tokens ~= raw_segment_count(text) * factor.\n"
        "'mean_file_ratio' is the unweighted per-file average of the same ratio, shown\n"
        "to sanity-check the aggregate isn't skewed by one huge file; 'error_%' is how\n"
        "far the mean per-file ratio drifts from the aggregate factor."
    )
    print("\nCALIBRATION = {")
    for kind, factor in measured.items():
        print(f'    "{kind}": {factor},')
    print("}")


if __name__ == "__main__":
    main()
