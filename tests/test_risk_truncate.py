"""truncate_risk_result: equivalence with the pre-0.12 loop, compact form and omitted_fields."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from cortex.risk import truncate_risk_result
from cortex.tokenizer import count_text_tokens


# --- oracle: the one-pop-at-a-time implementation shipped through 0.11 ---------------
def old_truncate_risk_result(result: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Deterministically keep high-risk files and directives within a budget."""
    output = json.loads(json.dumps(result, sort_keys=True))
    limit = max(0, int(budget))
    output["budget"] = limit
    output["truncated"] = False
    # Always preserve error/status and the concise directive list.  Remove
    # lower-ranked file detail first, then detailed formula/aliases if needed.
    while count_text_tokens(json.dumps(output, sort_keys=True)) > limit and output.get("files"):
        output["files"].pop()
        output["truncated"] = True
    if count_text_tokens(json.dumps(output, sort_keys=True)) > limit:
        for key in ("score_formula", "missing_cochanges", "analysis_errors", "unindexed_files"):
            if key in output and key not in {"analysis_errors", "unindexed_files"}:
                output.pop(key, None)
                output["truncated"] = True
                if count_text_tokens(json.dumps(output, sort_keys=True)) <= limit:
                    break
    if count_text_tokens(json.dumps(output, sort_keys=True)) > limit:
        # Remove redundant detailed directive buckets before shortening the
        # human-facing strings.  The unbudgeted result retains every bucket;
        # this order makes a small budget stable across Python versions.
        for key in (
            "tests_by_changed_file",
            "missing_cochange",
            "missing_cochanges",
            "missing_tests",
            "missing_qt_pairs",
            "missing_qt_instantiations",
            "missing_qt_sites",
            "build_system_misses",
            "score_formula",
            "analysis_errors",
            "unindexed_files",
        ):
            if key in output:
                output.pop(key, None)
                output["truncated"] = True
                if count_text_tokens(json.dumps(output, sort_keys=True)) <= limit:
                    break
    if count_text_tokens(json.dumps(output, sort_keys=True)) > limit:
        # Keep directives in stable order but drop from the end; the full
        # directive arrays remain available in an unbudgeted CLI analysis.
        while output.get("directives") and count_text_tokens(json.dumps(output, sort_keys=True)) > limit:
            output["directives"].pop()
            output["truncated"] = True
    if count_text_tokens(json.dumps(output, sort_keys=True)) > limit and output.get("tests_to_run"):
        # The impacted-test list goes last, lowest score first.  A shortened
        # list can no longer be vouched for, so it switches to run-all;
        # ``tests_run_all``/``tests_run_all_reason`` themselves are never dropped.
        while output.get("tests_to_run") and count_text_tokens(json.dumps(output, sort_keys=True)) > limit:
            output["tests_to_run"].pop()
            output["truncated"] = True
            if not output.get("tests_run_all"):
                output["tests_run_all"] = True
                output["tests_run_all_reason"] = "tests_to_run was truncated to fit the response budget"
    output["returned_count"] = len(output.get("files", []))
    output["budget_feasible"] = count_text_tokens(json.dumps(output, sort_keys=True)) <= limit
    return output



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


def test_matches_the_old_loop_for_every_budget_without_tests_by_changed_file() -> None:
    # The old loop never touched `reason`; with none present the new order's extra step is a no-op,
    # and with no map the remaining order (files, buckets, directives, tests_to_run) is unchanged.
    result = _without_reasons(_result(with_by_file=False))
    full = count_text_tokens(json.dumps(result, sort_keys=True))
    for budget in range(0, full + 40):
        new = truncate_risk_result(result, budget)
        old = old_truncate_risk_result(result, budget)
        # `returned_count` is now part of the fit, so a payload fitted to the limit keeps one
        # file fewer than the old loop where that count tipped it over.
        if "omitted_fields" not in new and len(new["files"]) == len(old["files"]):
            assert {k: v for k, v in new.items() if k != "budget_feasible"} == {
                k: v for k, v in old.items() if k != "budget_feasible"
            }, budget
            continue
        # `omitted_fields` counts toward the budget, so the new result can keep a little
        # less than the old one (which named nothing it removed), never more.
        assert set(_strip(new)) <= set(old), budget
        for key in ("files", "directives", "tests_to_run"):
            assert new[key] == old[key][: len(new[key])], (budget, key)


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
        named = [name for name in out.get("omitted_fields", []) if name != "tests_to_run.reason"]
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
    result["uncovered_changed_files"] = []
    return result


def test_large_diff_keeps_the_per_file_test_map_at_the_default_budget() -> None:
    result = _large_diff_result()
    assert count_text_tokens(json.dumps(result["tests_to_run"], sort_keys=True)) > 2000

    out = truncate_risk_result(result, 2000)

    assert out["truncated"] is True
    assert "tests_by_changed_file" in out, out.get("omitted_fields")
    assert out["tests_by_changed_file"]  # at least some changed files keep their tests
    assert "tests_by_changed_file" not in out.get("omitted_fields", [])


def test_the_map_outlives_directives_and_tests_to_run_reasons() -> None:
    result = _large_diff_result()
    full = count_text_tokens(json.dumps(result, sort_keys=True))
    for budget in range(1000, full, 250):
        out = truncate_risk_result(result, budget)
        if out.get("directives"):
            assert "tests_by_changed_file" in out, budget
