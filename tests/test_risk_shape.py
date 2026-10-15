"""Shape lock for ``cortex.risk.analyze_risk`` at 7b0e6e0 (Task 0 baseline).

Pins the exact top-level key set, the ``files[]`` item key set, the directive string
formats and ``truncate_risk_result`` drop order so Task 10 (``tests_to_run``) can add
fields without altering anything pre-existing.  Task 10 must update
``TOP_LEVEL_KEYS`` and ``DIRECTIVE_PREFIXES`` -- and nothing else here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cortex.cli import main as cli_main
from cortex.ingest import ingest_repository
from cortex.risk import _directive_strings, analyze_risk, truncate_risk_result
from tests.baseline_helpers import commit_all, fixed_git_env, git, init_git
from tests.golden_utils import force_stdlib_tokenizer

TOP_LEVEL_KEYS = [
    "analysis_errors",
    "build_system_misses",
    "cochange_threshold",
    "directives",
    "files",
    "index_status",
    "missing_cochange",
    "missing_cochanges",
    "missing_qt_instantiations",
    "missing_qt_pairs",
    "missing_qt_sites",
    "missing_tests",
    "range",
    "repo_path",
    "score_formula",
    "staged",
    "status",
    "unindexed",
    "unindexed_files",
]

FILE_ITEM_KEYS = [
    "additions",
    "binary",
    "churn",
    "components",
    "deletions",
    "fan_in",
    "hotspot",
    "indexed",
    "missing_cochange",
    "path",
    "risk",
    "risk_score",
    "score",
    "status",
    "structural_fan_in",
]

RENAME_EXTRA_KEYS = ["previous_path", "rename_score"]

COMPONENT_KEYS = ["cochange", "diff", "directives", "fan_in", "hotspot"]

DIRECTIVE_PREFIXES = [
    "build_system_miss:",
    "missing_cochange:",
    "missing_qt_instantiation:",
    "missing_qt_pair:",
    "missing_qt_site:",
    "missing_tests:",
]

ERROR_KEYS_NOT_GIT = ["directives", "error", "files", "message", "status"]
ERROR_KEYS_BAD_RANGE = ["directives", "error", "files", "message", "range", "staged", "status"]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    force_stdlib_tokenizer(monkeypatch)
    fixed_git_env(monkeypatch)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(tmp_path: Path) -> Path:
    """a.py/b.py always co-change; a.py has a paired test; a third commit touches only a.py."""
    repo = tmp_path / "repo"
    init_git(repo)
    _write(repo, "src/a.py", "def a():\n    return 1\n")
    _write(repo, "src/b.py", "def b():\n    return 1\n")
    _write(repo, "tests/test_a.py", "def test_a():\n    assert True\n")
    commit_all(repo, "initial")
    _write(repo, "src/a.py", "def a():\n    return 2\n")
    _write(repo, "src/b.py", "def b():\n    return 2\n")
    commit_all(repo, "change pair")
    _write(repo, "src/a.py", "def a():\n    return 3\n")
    commit_all(repo, "change one side")
    ingest_repository(repo, commit_limit=20)
    return repo


def test_result_top_level_key_set(tmp_path: Path) -> None:
    result = analyze_risk(make_repo(tmp_path))

    assert sorted(result) == TOP_LEVEL_KEYS
    assert result["status"] == "ok"
    assert result["range"] == "HEAD~1..HEAD"
    assert result["staged"] is False
    assert result["index_status"] == "available"
    assert result["cochange_threshold"] == 0.5
    assert result["unindexed"] is False and result["unindexed_files"] == [] and result["analysis_errors"] == []
    assert sorted(result["score_formula"]) == ["normalization", "score", "weights"]
    assert result["score_formula"]["weights"] == {
        "diff": 0.3, "hotspot": 0.2, "fan_in": 0.15, "cochange": 0.15, "directives": 0.2,
    }


def test_files_item_key_set_and_component_keys(tmp_path: Path) -> None:
    result = analyze_risk(make_repo(tmp_path))

    assert [item["path"] for item in result["files"]] == ["src/a.py"]
    for item in result["files"]:
        assert sorted(item) == FILE_ITEM_KEYS
        assert sorted(item["components"]) == COMPONENT_KEYS
        assert sorted(item["hotspot"]) == ["churn", "complexity", "score"]
        assert item["risk"] == item["risk_score"] == item["score"]
        assert item["fan_in"] == item["structural_fan_in"]


def test_rename_items_carry_extra_keys(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    git(repo, "mv", "src/b.py", "src/renamed.py")
    commit_all(repo, "rename b")

    result = analyze_risk(repo)

    (item,) = result["files"]
    assert item["status"] == "R" and item["path"] == "src/renamed.py"
    assert sorted(item) == sorted(FILE_ITEM_KEYS + RENAME_EXTRA_KEYS)
    assert item["previous_path"] == "src/b.py"


def test_directive_prefixes_and_formats_from_a_real_diff(tmp_path: Path) -> None:
    result = analyze_risk(make_repo(tmp_path))

    assert result["directives"] == [
        "missing_cochange: src/b.py (1.00) for src/a.py",
        "missing_tests: tests/test_a.py for src/a.py",
    ]
    assert result["directives"] == sorted(result["directives"])
    assert result["missing_cochange"] == result["missing_cochanges"]
    assert result["missing_cochange"][0] == {"path": "src/b.py", "weight": 1.0, "for": "src/a.py"}
    assert result["missing_tests"] == [
        {"source": "src/a.py", "test": "tests/test_a.py", "reason": "paired test is untouched"}
    ]


def test_every_directive_prefix_and_string_format() -> None:
    directives = _directive_strings(
        [{"path": "b.py", "weight": 0.875, "for": "a.py"}],
        [{"test": "tests/test_a.py", "source": "a.py"}],
        [{"partner": "a.hpp", "changed": "a.cpp"}],
        [{"qml": "Main.qml", "changed": "A.cpp"}],
        [{"site": "a.cpp", "relation": "connects", "signal": "done"}],
        [{"qml": "New.qml"}],
    )

    assert directives == [
        "build_system_miss: New.qml",
        "missing_cochange: b.py (0.88) for a.py",
        "missing_qt_instantiation: Main.qml for A.cpp",
        "missing_qt_pair: a.hpp for a.cpp",
        "missing_qt_site: a.cpp (connects done)",
        "missing_tests: tests/test_a.py for a.py",
    ]
    assert sorted(d.split(" ", 1)[0] for d in directives) == DIRECTIVE_PREFIXES
    assert _directive_strings([], [], [], [], [], []) == []


def test_error_results_have_stable_key_sets(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    not_git = analyze_risk(plain)
    assert sorted(not_git) == ERROR_KEYS_NOT_GIT
    assert not_git["status"] == "error" and not_git["error"] == "not_git"

    repo = make_repo(tmp_path)
    bad = analyze_risk(repo, "--output=/tmp/nope")
    assert sorted(bad) == ERROR_KEYS_BAD_RANGE
    assert bad["error"] == "invalid_range" and bad["range"] == "--output=/tmp/nope"

    single = tmp_path / "single"
    init_git(single)
    _write(single, "one.py", "one = 1\n")
    commit_all(single, "only commit")
    assert analyze_risk(single)["status"] == "error"


def test_unindexed_repo_is_partial_with_same_key_set(tmp_path: Path) -> None:
    repo = tmp_path / "noindex"
    init_git(repo)
    _write(repo, "one.py", "one = 1\n")
    commit_all(repo, "one")
    _write(repo, "one.py", "one = 2\n")
    commit_all(repo, "two")

    result = analyze_risk(repo)

    assert sorted(result) == TOP_LEVEL_KEYS
    assert result["status"] == "partial" and result["index_status"] == "missing"
    assert result["unindexed"] is True
    assert result["analysis_errors"] == ["Cortex index is unavailable; graph/hotspot analysis was skipped."]
    assert result["unindexed_files"] == ["one.py"]


def test_cli_json_output_matches_analyze_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["cortex", "risk", "--format", "json"])

    cli_main()

    parsed = json.loads(capsys.readouterr().out)
    assert sorted(parsed) == TOP_LEVEL_KEYS
    assert sorted(parsed["files"][0]) == FILE_ITEM_KEYS
    assert parsed == json.loads(json.dumps(analyze_risk(repo)))


def test_cli_text_output_lists_directives_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["cortex", "risk"])

    cli_main()

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Risk: HEAD~1..HEAD (committed range)"
    assert "- missing_cochange: src/b.py (1.00) for src/a.py" in lines
    assert "- missing_tests: tests/test_a.py for src/a.py" in lines
    assert any(line.startswith("src/a.py: risk=") and "fan-in=" in line for line in lines)


# --------------------------------------------------------------------------- #
# truncate_risk_result
# --------------------------------------------------------------------------- #


def _file(path: str, weight: int) -> dict:
    return {
        "path": path, "status": "M", "additions": weight, "deletions": 0, "churn": weight, "binary": False,
        "hotspot": {"churn": 1, "complexity": 1, "score": 1}, "fan_in": weight, "structural_fan_in": weight,
        "missing_cochange": [], "components": {"diff": 0.1}, "risk_score": float(weight), "risk": float(weight),
        "score": float(weight), "indexed": True,
    }


def _synthetic_result() -> dict:
    record = {"path": "b.py", "weight": 0.9, "for": "a.py"}
    return {
        "status": "ok", "range": "HEAD~1..HEAD", "staged": False, "repo_path": "/r", "index_status": "available",
        "cochange_threshold": 0.5,
        "score_formula": {"weights": {"a": 1}, "normalization": {"a": "x" * 40}, "score": "round"},
        "files": [_file("a.py", 3), _file("b.py", 2), _file("c.py", 1)],
        "missing_cochange": [record], "missing_cochanges": [record],
        "missing_tests": [{"source": "a.py", "test": "test_a.py", "reason": "r"}],
        "missing_qt_pairs": [{"changed": "a.cpp", "partner": "a.hpp", "reason": "r"}],
        "missing_qt_instantiations": [], "missing_qt_sites": [], "build_system_misses": [],
        "directives": [
            "build_system_miss: x.qml",
            "missing_cochange: b.py (0.90) for a.py",
            "missing_qt_pair: a.hpp for a.cpp",
            "missing_tests: test_a.py for a.py",
        ],
        "unindexed": False, "unindexed_files": [], "analysis_errors": [],
    }


# Order in which truncation gives things up as the budget shrinks.
DROP_ORDER = [
    "file:c.py", "file:b.py", "file:a.py",
    "score_formula", "missing_cochanges", "missing_cochange", "missing_tests",
    "missing_qt_pairs", "missing_qt_instantiations", "missing_qt_sites", "build_system_misses",
    "analysis_errors", "unindexed_files",
    "directive:missing_tests: test_a.py for a.py",
    "directive:missing_qt_pair: a.hpp for a.cpp",
    "directive:missing_cochange: b.py (0.90) for a.py",
    "directive:build_system_miss: x.qml",
]


def test_truncate_risk_result_drop_order_and_added_fields() -> None:
    from cortex.tokenizer import count_text_tokens

    result = _synthetic_result()
    snapshot = json.dumps(result, sort_keys=True)
    full = count_text_tokens(snapshot)

    removed_at: dict[str, int] = {}  # item -> largest budget at which it is gone
    for budget in range(full + 20, -1, -1):
        out = truncate_risk_result(result, budget)
        present = {f"file:{f['path']}" for f in out["files"]} | {k for k in result if k in out}
        present |= {f"directive:{d}" for d in out["directives"]}
        for item in DROP_ORDER:
            if item not in present:
                removed_at.setdefault(item, budget)
        # Every call: documented added fields, never mutates the input.
        assert out["budget"] == budget
        assert out["returned_count"] == len(out["files"])
        # Quirk pinned: feasibility is measured *before* the flag itself is added.
        without_flag = {k: v for k, v in out.items() if k != "budget_feasible"}
        assert out["budget_feasible"] == (count_text_tokens(json.dumps(without_flag, sort_keys=True)) <= budget)
        assert out["truncated"] in (True, False)
    assert json.dumps(result, sort_keys=True) == snapshot

    assert set(removed_at) == set(DROP_ORDER)  # budget 0 removes everything
    thresholds = [removed_at[item] for item in DROP_ORDER]
    assert thresholds == sorted(thresholds, reverse=True), dict(zip(DROP_ORDER, thresholds))


def test_truncate_risk_result_generous_budget_keeps_everything_and_adds_fields() -> None:
    result = _synthetic_result()

    out = truncate_risk_result(result, 100_000)

    assert out["truncated"] is False and out["budget_feasible"] is True
    assert out["budget"] == 100_000 and out["returned_count"] == 3
    assert {k: v for k, v in out.items() if k not in {"budget", "truncated", "returned_count", "budget_feasible"}} == result
    assert [f["path"] for f in out["files"]] == ["a.py", "b.py", "c.py"]  # highest risk first, kept in order


def test_truncate_risk_result_negative_budget_clamps_to_zero() -> None:
    out = truncate_risk_result(_synthetic_result(), -5)

    assert out["budget"] == 0 and out["truncated"] is True
    assert out["files"] == [] and out["directives"] == []
