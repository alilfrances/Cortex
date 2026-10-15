"""Unit tests for ``scripts/check_coverage_floors.py`` and the checked-in floors file."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_coverage_floors.py"
FLOORS_FILE = ROOT / "tests" / "coverage_floors.toml"

# The modules Task 0 pins (plan: "Coverage floors").
PINNED_MODULES = {
    "cortex.gitutils", "cortex.cochange", "cortex.hotspots", "cortex.impact", "cortex.pathfind",
    "cortex.references", "cortex.deadcode", "cortex.risk", "cortex.graph", "cortex.ingest",
    "cortex.store", "cortex.ast_extract", "cortex.structural.treesitter_backend",
    "cortex.structural.regex_backend", "cortex.mcp.tools", "cortex.mcp.server",
}


def _load_script():
    spec = importlib.util.spec_from_file_location("check_coverage_floors", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


checker = _load_script()


def _report(tmp_path: Path, percents: dict[str, float]) -> Path:
    report = {"files": {name: {"summary": {"percent_covered": value}} for name, value in percents.items()}}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _floors(tmp_path: Path, floors: dict[str, object]) -> Path:
    body = "[floors]\n" + "".join(f'"{module}" = {value}\n' for module, value in floors.items())
    path = tmp_path / "floors.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _run(*args: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True, check=False)


def test_passes_at_or_above_floor(tmp_path: Path) -> None:
    coverage = _report(tmp_path, {"src/cortex/impact.py": 90.0, "src/cortex/mcp/tools.py": 80.5})
    floors = _floors(tmp_path, {"cortex.impact": 90, "cortex.mcp.tools": 80})

    result = _run("--coverage", coverage, "--floors", floors)

    assert result.returncode == 0, result.stderr
    assert "All 2 floors satisfied." in result.stdout


def test_fails_below_floor_and_names_the_module(tmp_path: Path) -> None:
    coverage = _report(tmp_path, {"src/cortex/impact.py": 89.99, "src/cortex/risk.py": 99.0})
    floors = _floors(tmp_path, {"cortex.impact": 90, "cortex.risk": 90})

    result = _run("--coverage", coverage, "--floors", floors)

    assert result.returncode == 1
    assert "cortex.impact: 89.99% is below its floor of 90%" in result.stderr
    assert "cortex.risk" not in result.stderr


def test_module_missing_from_report_fails(tmp_path: Path) -> None:
    coverage = _report(tmp_path, {"src/cortex/impact.py": 99.0})
    floors = _floors(tmp_path, {"cortex.impact": 90, "cortex.risk": 90})

    result = _run("--coverage", coverage, "--floors", floors)

    assert result.returncode == 1
    assert "cortex.risk: not present in the coverage report" in result.stderr


def test_dotted_module_maps_to_path_and_windows_separators(tmp_path: Path) -> None:
    coverage = _report(tmp_path, {"src\\cortex\\structural\\regex_backend.py": 95.0, "src/cortex/regex_backend.py": 1.0})
    floors = _floors(tmp_path, {"cortex.structural.regex_backend": 95})

    assert _run("--coverage", coverage, "--floors", floors).returncode == 0


def test_ambiguous_module_match_is_an_input_error(tmp_path: Path) -> None:
    coverage = _report(tmp_path, {"src/cortex/impact.py": 99.0, "build/lib/cortex/impact.py": 99.0})
    floors = _floors(tmp_path, {"cortex.impact": 90})

    result = _run("--coverage", coverage, "--floors", floors)

    assert result.returncode == 2 and "matches several report files" in result.stderr


@pytest.mark.parametrize("bad", ['"90"', "101", "-1", "true", "90.5"])
def test_invalid_floor_values_are_rejected(tmp_path: Path, bad: str) -> None:
    coverage = _report(tmp_path, {"src/cortex/impact.py": 99.0})
    floors = _floors(tmp_path, {"cortex.impact": bad})

    result = _run("--coverage", coverage, "--floors", floors)

    assert result.returncode == 2 and "must be an integer 0..100" in result.stderr


def test_missing_or_malformed_inputs_exit_2(tmp_path: Path) -> None:
    floors = _floors(tmp_path, {"cortex.impact": 90})
    assert _run("--coverage", tmp_path / "absent.json", "--floors", floors).returncode == 2

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _run("--coverage", broken, "--floors", floors).returncode == 2

    not_coverage = tmp_path / "other.json"
    not_coverage.write_text("[]", encoding="utf-8")
    assert _run("--coverage", not_coverage, "--floors", floors).returncode == 2

    empty = tmp_path / "empty.toml"
    empty.write_text("# nothing\n", encoding="utf-8")
    assert _run("--coverage", _report(tmp_path, {}), "--floors", empty).returncode == 2


def test_checked_in_floors_cover_exactly_the_planned_modules() -> None:
    floors = tomllib.loads(FLOORS_FILE.read_text(encoding="utf-8"))["floors"]

    assert set(floors) == PINNED_MODULES
    assert all(isinstance(v, int) and not isinstance(v, bool) and 50 <= v <= 100 for v in floors.values())
    # Each pinned module corresponds to a real source file (typos would make the CI step fail late).
    for module in PINNED_MODULES:
        assert (ROOT / "src" / (module.replace(".", "/") + ".py")).is_file(), module
    assert checker.load_floors(FLOORS_FILE) == floors
