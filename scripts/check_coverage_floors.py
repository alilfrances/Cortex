#!/usr/bin/env python3
"""Fail when a module's line coverage drops below its pinned floor.

Reads a ``coverage.json`` produced by::

    python -m pytest tests/ -q --cov=cortex --cov-report=json:coverage.json

and the floors in ``tests/coverage_floors.toml`` (``[floors] "cortex.impact" = 98``).
Only the listed modules are checked.  Exit status:

* ``0`` -- every listed module meets its floor;
* ``1`` -- at least one module is below its floor, or is missing from the report
  (a module that silently disappears from coverage must not pass);
* ``2`` -- unusable input (missing/malformed coverage or floors file, bad floor value).

Standard library only (``tomllib`` needs Python 3.11+, the project minimum).
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COVERAGE = ROOT / "coverage.json"
DEFAULT_FLOORS = ROOT / "tests" / "coverage_floors.toml"


class InputError(Exception):
    """Unusable coverage/floors input (exit status 2)."""


def load_floors(path: Path) -> dict[str, int]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read floors file {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise InputError(f"malformed floors file {path}: {exc}") from exc
    floors = data.get("floors")
    if not isinstance(floors, dict) or not floors:
        raise InputError(f"{path} has no [floors] table")
    for module, value in floors.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise InputError(f"floor for {module!r} must be an integer 0..100, got {value!r}")
    return {str(module): int(value) for module, value in floors.items()}


def load_coverage(path: Path) -> dict[str, float]:
    """Map normalised file path -> line coverage percent."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read coverage report {path}: {exc} (run pytest with --cov-report=json)") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"malformed coverage report {path}: {exc}") from exc
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise InputError(f"{path} has no 'files' mapping; is it a coverage.py JSON report?")
    result: dict[str, float] = {}
    for name, info in files.items():
        try:
            result[str(name).replace("\\", "/")] = float(info["summary"]["percent_covered"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"{path}: file entry {name!r} lacks summary.percent_covered") from exc
    return result


def module_percent(module: str, coverage: dict[str, float]) -> float | None:
    """Coverage of ``cortex.a.b`` -> the unique report file ending in ``cortex/a/b.py``."""
    suffix = "/" + module.replace(".", "/") + ".py"
    matches = [name for name in coverage if ("/" + name).endswith(suffix)]
    if not matches:
        return None
    if len(matches) > 1:
        raise InputError(f"module {module} matches several report files: {sorted(matches)}")
    return coverage[matches[0]]


def check(floors: dict[str, int], coverage: dict[str, float]) -> tuple[list[str], list[str]]:
    """Return (table lines, failure messages)."""
    lines: list[str] = []
    failures: list[str] = []
    width = max(len(module) for module in floors)
    for module in sorted(floors):
        floor = floors[module]
        percent = module_percent(module, coverage)
        if percent is None:
            lines.append(f"  MISSING  {module:<{width}}  floor {floor}")
            failures.append(f"{module}: not present in the coverage report (floor {floor})")
        elif percent < floor:
            lines.append(f"  FAIL     {module:<{width}}  {percent:6.2f} < floor {floor}")
            failures.append(f"{module}: {percent:.2f}% is below its floor of {floor}%")
        else:
            lines.append(f"  ok       {module:<{width}}  {percent:6.2f} >= floor {floor}")
    return lines, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coverage", type=Path, default=DEFAULT_COVERAGE, help="coverage.py JSON report")
    parser.add_argument("--floors", type=Path, default=DEFAULT_FLOORS, help="TOML file with a [floors] table")
    args = parser.parse_args(argv)
    try:
        floors = load_floors(args.floors)
        coverage = load_coverage(args.coverage)
        lines, failures = check(floors, coverage)
    except InputError as exc:
        print(f"check_coverage_floors: {exc}", file=sys.stderr)
        return 2
    print(f"Coverage floors ({args.floors.name}) vs {args.coverage.name}:")
    print("\n".join(lines))
    if failures:
        print(f"\n{len(failures)} module(s) below floor:", file=sys.stderr)
        for message in failures:
            print(f"  - {message}", file=sys.stderr)
        return 1
    print(f"\nAll {len(floors)} floors satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
