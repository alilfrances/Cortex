"""truncate_risk_result: equivalence with the pre-0.12 loop, compact form and omitted_fields."""
from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cortex.risk import (
    _RISK_REDUCIBLE_KEYS,
    _RISK_SCALAR_KEYS,
    FILES_FLOOR,
    MAP_TESTS_PER_FILE_CAPS,
    TESTS_TO_RUN_FLOOR,
    UNCOVERED_FLOOR,
    analyze_risk,
    truncate_risk_result,
)
from cortex.tokenizer import count_text_tokens


def _file(path: str, weight: int) -> dict:
    return {
        "path": path, "status": "M", "additions": weight, "deletions": 0, "churn": weight, "binary": False,
        "hotspot": {"churn": 1, "complexity": 1, "score": 1}, "fan_in": weight, "structural_fan_in": weight,
        "missing_cochange": [], "components": {"diff": 0.1}, "risk_score": float(weight), "risk": float(weight),
        "score": float(weight), "indexed": True,
    }


def _result(*, with_by_file: bool) -> dict:
    record = {"path": "b.py", "weight": 0.9, "for": "a.py"}
    names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    tests = [
        {"test": f"tests/integration/subsystem/test_{name}_behaviour.py", "via": ["structural"], "reason": "r", "score": 1.0 - i / 10}
        for i, name in enumerate(names)
    ]
    result = {
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
        "tests_to_run": tests, "tests_run_all": False, "tests_run_all_reason": "",
        "uncovered_changed_files": [],
    }
    if with_by_file:
        # Many changed files each pointing at several long test paths: the full mapping is large,
        # its index form small, so a wide budget window separates the two whatever the tokenizer.
        result["tests_by_changed_file"] = {
            f"src/subsystem/module_{n}/implementation_file_{n}.py": [t["test"] for t in tests[n % 3 : n % 3 + 3]]
            for n in range(8)
        }
    return result


def _strip(output: Mapping[str, Any]) -> dict:
    return {k: v for k, v in output.items() if k != "omitted_fields"}


def _without_reasons(result: dict) -> dict:
    return {**result, "tests_to_run": [{k: v for k, v in t.items() if k != "reason"} for t in result["tests_to_run"]]}


def _compact(item: Mapping[str, Any]) -> dict:
    return {k: v for k, v in item.items() if k in ("path", "previous_path", "status", "risk_score")}


def test_files_are_a_ranked_prefix_and_given_up_last() -> None:
    # Through 0.12 `files` was cut first, so a large range returned no ranked file at all
    # at the default budget.  Now it is cut only to FILES_FLOOR early and below it last.
    result = _without_reasons(_result(with_by_file=False))
    full = count_text_tokens(json.dumps(result, sort_keys=True))
    for budget in range(0, full + 40):
        out = truncate_risk_result(result, budget)
        files = out.get("files", [])
        assert out["returned_count"] == len(files), budget
        expected = result["files"]
        if "files.detail" in out.get("omitted_fields", []):
            expected = [_compact(item) for item in expected]
        assert files == expected[: len(files)], budget
        if out["budget_feasible"] and not out.get("minimal_payload"):
            assert count_text_tokens(json.dumps({k: v for k, v in out.items() if k != "budget_feasible"}, sort_keys=True)) <= budget
        if len(files) < len(result["files"]) and not out.get("minimal_payload"):
            # below the floor only once directives and tests_to_run are gone
            assert not out.get("directives") and not out.get("tests_to_run"), budget


def test_file_detail_goes_before_directives() -> None:
    result = _without_reasons(_result(with_by_file=False))
    full = count_text_tokens(json.dumps(result, sort_keys=True))
    for budget in range(full, 0, -1):
        out = truncate_risk_result(result, budget)
        if len(out.get("directives", [])) < len(result["directives"]):
            assert "files.detail" in out["omitted_fields"], budget
            break
    else:
        pytest.fail("directives were never shortened")


def _decode(out: dict) -> dict[str, set[str]]:
    table = out["tests_by_changed_file_tests"]
    return {path: {table[i] for i in indices} for path, indices in out["tests_by_changed_file"].items()}


def test_the_map_is_encoded_against_its_own_table_and_dropped_last() -> None:
    result = _result(with_by_file=True)
    original = {path: set(tests) for path, tests in result["tests_by_changed_file"].items()}
    encoded = dropped = False
    first_dropped_other: dict[str, int] = {}
    for budget in range(count_text_tokens(json.dumps(result, sort_keys=True)), -1, -1):
        out = truncate_risk_result(result, budget)
        for key in ("unindexed_files", "analysis_errors", "directives"):
            if not out.get(key) and key not in first_dropped_other:
                first_dropped_other[key] = budget
        if "tests_by_changed_file" not in out:
            dropped = True
            assert out["omitted_fields"][-1] == "tests_by_changed_file"
            assert not [k for k in out if k.startswith("tests_by_changed_file_")]
            continue
        if out.get("tests_by_changed_file_encoding") == "test_table":
            encoded = True
            decoded = _decode(out)
            assert all(tests <= original[path] for path, tests in decoded.items())
            assert all(0 <= i < len(out["tests_by_changed_file_tests"]) for v in out["tests_by_changed_file"].values() for i in v)
    assert encoded and dropped
    # The map goes after the bucket-level fields and before every directive is gone.
    assert first_dropped_other["unindexed_files"] >= first_dropped_other.get("directives", 0)


def test_cutting_tests_to_run_cannot_invalidate_the_map() -> None:
    result = _large_diff_result()
    original = {path: set(tests) for path, tests in result["tests_by_changed_file"].items()}
    cut = 0
    for budget in range(300, 6000, 100):
        out = truncate_risk_result(result, budget)
        if len(out["tests_to_run"]) < len(result["tests_to_run"]) and "tests_by_changed_file" in out:
            cut += 1
            assert all(tests <= original[path] for path, tests in _decode(out).items()), budget
            assert out["tests_run_all"] is True
    assert cut


def test_omitted_fields_names_exactly_the_removed_keys() -> None:
    result = _result(with_by_file=True)
    for budget in range(count_text_tokens(json.dumps(result, sort_keys=True)) + 10, -1, -1):
        out = truncate_risk_result(result, budget)
        missing = [key for key in result if key not in out and key not in {"files"}]
        named = [name for name in out.get("omitted_fields", []) if name not in {"tests_to_run.reason", "files.detail"}]
        assert sorted(named) == sorted(missing), budget
        stripped = "tests_to_run.reason" in out.get("omitted_fields", [])
        if any("reason" in t for t in out["tests_to_run"]):
            assert not stripped, budget
        if stripped:
            assert not any("reason" in t for t in out["tests_to_run"]), budget
        if not missing and "tests_to_run.reason" not in out.get("omitted_fields", []):
            assert "omitted_fields" not in out


def test_truncation_does_not_mutate_its_input() -> None:
    result = _result(with_by_file=True)
    snapshot = json.dumps(result, sort_keys=True)
    truncate_risk_result(result, 150)
    assert json.dumps(result, sort_keys=True) == snapshot


def test_shortening_tests_to_run_switches_to_run_all() -> None:
    result = _result(with_by_file=True)
    for budget in range(count_text_tokens(json.dumps(result, sort_keys=True)), -1, -1):
        out = truncate_risk_result(result, budget)
        if len(out["tests_to_run"]) < len(result["tests_to_run"]):
            assert out["tests_run_all"] is True and out["truncated"] is True


@pytest.mark.parametrize("budget", [-3, 0])
def test_budget_zero_clamps_and_empties(budget: int) -> None:
    out = truncate_risk_result(_result(with_by_file=True), budget)
    assert out["budget"] == 0 and out["files"] == [] and out["directives"] == [] and out["tests_to_run"] == []


# --- 0.12.0 review: the per-file test map on a large diff (docs/plan-0.12.1-review.md, Task A.1) ---


def _large_diff_result() -> dict:
    """Shaped like `HEAD~30..HEAD` on a real repository: many changed files, many tests,
    long reasons. `tests_to_run` alone is far over the 2000-token default budget."""
    tests = [
        {
            "test": f"tests/unit/module_{i % 40}/test_component_{i}.cpp",
            "via": ["structural", "pair"] if i % 3 else ["cochange"],
            "reason": f"structural: calls path (depth {1 + i % 3}) from src/module_{i % 40}/component_{i}.cpp:Component{i}::apply",
            "score": round(1.0 - i / 400, 3),
        }
        for i in range(180)
    ]
    by_file = {
        f"src/module_{n % 40}/component_{n}.cpp": [tests[(n + k) % 180]["test"] for k in range(3)]
        for n in range(120)
    }
    result = _result(with_by_file=False)
    result["files"] = [_file(path, 120 - index) for index, path in enumerate(by_file)]
    result["directives"] = [f"missing_tests: {tests[i]['test']} for src/x{i}.cpp" for i in range(60)]
    result["tests_to_run"] = tests
    result["tests_by_changed_file"] = by_file
    # Most changed files of a real large range have no reaching test (276 of 334 in the field
    # report), and this list alone was over the default budget in 0.12.1.
    result["uncovered_changed_files"] = [
        f"src/subsystem_{n % 17}/platform/implementation/component_{n}_with_a_long_name.cpp" for n in range(250)
    ]
    return result


def test_large_diff_keeps_the_per_file_test_map_at_the_default_budget() -> None:
    result = _large_diff_result()
    assert count_text_tokens(json.dumps(result["tests_to_run"], sort_keys=True)) > 2000

    out = truncate_risk_result(result, 2000)

    assert out["truncated"] is True
    assert "tests_by_changed_file" in out, out.get("omitted_fields")
    assert out["tests_by_changed_file"]  # at least some changed files keep their tests
    assert "tests_by_changed_file" not in out.get("omitted_fields", [])
    # The plan's acceptance for the encoded, partial map (docs/plan-0.12.1-review.md, Task A.1).
    assert out["tests_by_changed_file_encoding"] == "test_table"
    table = out["tests_by_changed_file_tests"]
    assert all(0 <= i < len(table) for indices in out["tests_by_changed_file"].values() for i in indices)
    original = result["tests_by_changed_file"]
    for path, indices in out["tests_by_changed_file"].items():
        assert {table[i] for i in indices} <= set(original[path])
    assert len(out["tests_by_changed_file"]) + out.get("tests_by_changed_file_omitted", 0) == len(original)
    assert len(out["tests_to_run"]) >= 10
    assert out["budget_feasible"] is True
    assert len(out["tests_by_changed_file"]) >= 10
    assert out["uncovered_changed_files_total"] == 250
    assert out["uncovered_changed_files"] == result["uncovered_changed_files"][: len(out["uncovered_changed_files"])]
    assert len(out["uncovered_changed_files"]) <= UNCOVERED_FLOOR


def test_the_highest_risk_changed_files_keep_their_map_entries() -> None:
    result = _large_diff_result()
    out = truncate_risk_result(result, 2000)

    by_risk = [item["path"] for item in result["files"]]
    kept = list(out["tests_by_changed_file"])
    assert kept == by_risk[: len(kept)]


def test_below_the_floor_the_map_wins_over_tests_to_run() -> None:
    result = _large_diff_result()
    for budget in range(400, 1600, 50):
        out = truncate_risk_result(result, budget)
        if len(out["tests_to_run"]) < 10 and out["tests_to_run"]:
            # tests_to_run was cut below its floor, so the map must still be there.
            assert out.get("tests_by_changed_file"), budget
        if "tests_by_changed_file" in out.get("omitted_fields", []):
            # The map is dropped only once tests_to_run is gone, and only the ranked files remain.
            assert not out["tests_to_run"], budget
            assert out["files"], budget


def test_the_map_outlives_directives_and_tests_to_run_reasons() -> None:
    result = _large_diff_result()
    full = count_text_tokens(json.dumps(result, sort_keys=True))
    for budget in range(1000, full, 250):
        out = truncate_risk_result(result, budget)
        if out.get("directives"):
            assert "tests_by_changed_file" in out, budget


# --- 0.12.2: `uncovered_changed_files` is ranked, counted and cut; every collection key reducible ---


def test_uncovered_changed_files_is_untouched_when_nothing_is_over_budget() -> None:
    result = _large_diff_result()
    out = truncate_risk_result(result, 1_000_000)
    assert out["uncovered_changed_files"] == result["uncovered_changed_files"]
    assert "uncovered_changed_files_total" not in out and out["truncated"] is False


def test_uncovered_total_appears_only_when_the_list_was_shortened() -> None:
    result = _large_diff_result()
    result["uncovered_changed_files"] = result["uncovered_changed_files"][:3]
    for budget in range(0, 6000, 150):
        out = truncate_risk_result(result, budget)
        shortened = len(out["uncovered_changed_files"]) < 3
        assert ("uncovered_changed_files_total" in out) == shortened, budget
        if shortened:
            assert out["uncovered_changed_files_total"] == 3


def test_the_uncovered_list_goes_below_its_floor_only_after_tests_to_run_reach_theirs() -> None:
    result = _large_diff_result()
    for budget in range(400, 4000, 50):
        out = truncate_risk_result(result, budget)
        if len(out["uncovered_changed_files"]) < min(UNCOVERED_FLOOR, 250):
            assert len(out["tests_to_run"]) <= 10 or out["tests_to_run"] == [], budget
            assert out["uncovered_changed_files_total"] == 250
        assert out["uncovered_changed_files"] == result["uncovered_changed_files"][: len(out["uncovered_changed_files"])]


def test_an_unknown_oversized_key_gets_the_minimal_payload() -> None:
    result = _large_diff_result()
    result["brand_new_bucket"] = [{"path": f"x/{n}", "why": "w" * 40} for n in range(400)]
    out = truncate_risk_result(result, 2000)
    assert out["budget_feasible"] is True and out["minimal_payload"] is True
    assert "brand_new_bucket" in out["omitted_fields"] and "brand_new_bucket" not in out
    for key in ("status", "range", "tests_run_all", "tests_run_all_reason"):
        assert key in out
    missing = [key for key in result if key not in out and key != "files"]
    assert sorted(missing) == sorted(name for name in out["omitted_fields"] if name not in {"tests_to_run.reason", "files.detail"})
    assert out["tests_by_changed_file"]  # as much of the map as fits
    assert out["uncovered_changed_files_total"] == 250


def test_absurd_budgets_stay_honest() -> None:
    result = _large_diff_result()
    result["brand_new_bucket"] = ["x" * 50] * 200
    out = truncate_risk_result(result, 0)
    assert out["budget_feasible"] is False
    out = truncate_risk_result(result, 5)
    assert out["budget_feasible"] is False


def test_normal_results_never_use_the_minimal_payload() -> None:
    result = _large_diff_result()
    for budget in range(600, 6000, 400):
        assert "minimal_payload" not in truncate_risk_result(result, budget), budget


def _inflate(value: Any) -> Any:
    """Make a list/dict value ~50x bigger, filling an empty one with plausible junk."""
    if isinstance(value, list):
        return list(value) * 50 if value else [f"filler/{n}/some_long_path_component.cpp: detail text" for n in range(50)]
    if isinstance(value, dict):
        out = {f"{key}#{n}" if n else key: item for n in range(50) for key, item in value.items()}
        return out or {f"filler_{n}": ["tests/t.cpp"] for n in range(50)}
    return value


def test_every_collection_key_is_reducible(tmp_path: Path) -> None:
    """A list/dict key added to `analyze_risk` but not to the truncator fails here (0.12.1's bug class)."""
    from tests.field_shape import build_field_shape_repo

    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    real = analyze_risk(repo, "HEAD~25..HEAD")
    assert real["status"] in {"ok", "partial"} and real["uncovered_changed_files"] and real["tests_by_changed_file"]

    unclassified = [
        key for key, value in real.items() if key not in _RISK_SCALAR_KEYS and not isinstance(value, (list, dict))
    ]
    assert not unclassified, f"scalar key(s) missing from _RISK_SCALAR_KEYS: {unclassified}"
    hidden = [key for key in _RISK_SCALAR_KEYS if isinstance(real.get(key), (list, dict))]
    assert not hidden, f"collection(s) listed as scalar: {hidden}"
    unreduced = [key for key, value in real.items() if isinstance(value, (list, dict)) and key not in _RISK_REDUCIBLE_KEYS]
    assert not unreduced, f"collection key(s) with no truncation step: {unreduced}"

    inflated = {key: _inflate(value) for key, value in real.items()}
    out = truncate_risk_result(inflated, 2000)

    assert out["budget_feasible"] is True
    assert "minimal_payload" not in out, "the fallback must not be what makes a known key fit"
    omitted = set(out.get("omitted_fields", []))
    for key, value in inflated.items():
        if not isinstance(value, (list, dict)):
            continue
        counted = f"{key}_total" in out or f"{key}_omitted" in out
        shortened = key in out and len(out[key]) < len(value)
        assert key in omitted or shortened or counted, f"{key} is neither shortened, omitted nor counted"


def test_uncovered_changed_files_follow_the_files_risk_ranking(tmp_path: Path) -> None:
    from tests.field_shape import build_field_shape_repo

    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    result = analyze_risk(repo, "HEAD~25..HEAD")
    rank = {item["path"]: index for index, item in enumerate(result["files"])}
    uncovered = result["uncovered_changed_files"]
    assert len(uncovered) > UNCOVERED_FLOOR  # a prefix cut is meaningful on this profile
    assert uncovered == sorted(uncovered, key=lambda path: (rank[path], path))
    assert uncovered != sorted(uncovered)  # ranked, not alphabetical
    assert not set(uncovered) & set(result["tests_by_changed_file"])


# --- a real large range: `analyze_risk HEAD~30..HEAD` on the synthetic perf repository -------
# (scripts/perf/synthetic_repo.py): 172 ranked files, 117 changed files in the per-file test
# map, 185 tests to run and 55 uncovered changed files; 46k tokens unbudgeted.
_BENCH_FIXTURE = Path(__file__).parent / "fixtures" / "risk" / "synthetic_head30.json.gz"


def _bench_result() -> dict:
    with gzip.open(_BENCH_FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_bench_range_keeps_ranked_files_tests_and_most_of_the_map_at_the_default_budget() -> None:
    # Measured targets (0.13.0).  Through 0.12.1 this range returned no ranked file and 15 of
    # the 117 map entries at 2000; 117 entries at 2000 is not reachable while FILES_FLOOR files
    # and TESTS_TO_RUN_FLOOR tests are kept (their compact forms alone take ~750 tokens).
    result = _bench_result()
    assert len(result["tests_by_changed_file"]) == 117

    out = truncate_risk_result(result, 2000)

    assert out["budget_feasible"] is True and not out.get("minimal_payload")
    assert out["returned_count"] == len(out["files"]) >= FILES_FLOOR
    assert len(out["tests_to_run"]) >= TESTS_TO_RUN_FLOOR
    assert len(out["tests_by_changed_file"]) >= 45
    assert len(out["tests_by_changed_file"]) + out["tests_by_changed_file_omitted"] == 117


@pytest.mark.parametrize("budget", [3000, 4000])
def test_bench_range_keeps_every_changed_file_in_the_map(budget: int) -> None:
    result = _bench_result()
    out = truncate_risk_result(result, budget)

    assert out["budget_feasible"] is True
    assert len(out["tests_by_changed_file"]) == 117
    assert "tests_by_changed_file_omitted" not in out
    assert len(out["files"]) >= FILES_FLOOR


def test_capped_map_entries_keep_the_highest_scoring_tests_and_count_the_rest() -> None:
    result = _bench_result()
    score = {item["test"]: (-item["score"], position) for position, item in enumerate(result["tests_to_run"])}
    full = result["tests_by_changed_file"]
    for budget in (2000, 3000, 4000):
        out = truncate_risk_result(result, budget)
        decoded = _decode(out)
        caps = {len(tests) for path, tests in decoded.items() if len(tests) < len(full[path])}
        assert len(caps) <= 1 and caps <= set(MAP_TESTS_PER_FILE_CAPS), (budget, caps)
        more = out.get("tests_by_changed_file_more", {})
        for path, tests in decoded.items():
            best = sorted(full[path], key=lambda test: score.get(test, (0.0, len(score))))[: len(tests)]
            assert tests == set(best), (budget, path)
            assert more.get(path, 0) == len(full[path]) - len(tests), (budget, path)


def test_the_map_is_capped_before_any_changed_file_is_dropped() -> None:
    result = _bench_result()
    for budget in range(1500, 4001, 250):
        out = truncate_risk_result(result, budget)
        if out.get("tests_by_changed_file_omitted"):
            # files are dropped only at the smallest cap
            assert max(len(v) for v in out["tests_by_changed_file"].values()) <= MAP_TESTS_PER_FILE_CAPS[-1], budget
