from __future__ import annotations

import json
from pathlib import Path
import subprocess

from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.risk import (
    COCHANGE_THRESHOLD,
    analyze_risk,
    parse_name_status,
    parse_zero_context_diff,
    risk_score,
)
from cortex.store import CortexStore, default_db_path
from evals.run_evals import _build_qt_app


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "src/b.py").write_text("def b():\n    return 1\n", encoding="utf-8")
    (repo / "tests/test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (repo / "CMakeLists.txt").write_text("set(QML_FILES)\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "initial")
    # This history establishes an always-cochanging a/b pair.
    (repo / "src/a.py").write_text("def a():\n    return 2\n", encoding="utf-8")
    (repo / "src/b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "change pair")
    (repo / "src/a.py").write_text("def a():\n    return 3\n", encoding="utf-8")
    _run(repo, "add", "src/a.py")
    _run(repo, "commit", "-qm", "change one side")
    ingest_repository(repo, commit_limit=20)
    return repo


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_plain_git_and_shallow_history_errors_are_explicit(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _run(plain, "init", "-q")
    (plain / "one.py").write_text("one = 1\n", encoding="utf-8")
    _run(plain, "add", "one.py")
    _run(plain, "config", "user.email", "test@example.com")
    _run(plain, "config", "user.name", "Test")
    _run(plain, "commit", "-qm", "one")
    (plain / "one.py").write_text("one = 2\n", encoding="utf-8")
    _run(plain, "add", ".")
    _run(plain, "commit", "-qm", "two")
    result = analyze_risk(plain)
    assert result["status"] == "partial" and result["index_status"] == "missing"

    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q")
    _run(origin, "config", "user.email", "test@example.com")
    _run(origin, "config", "user.name", "Test")
    (origin / "one.py").write_text("one = 1\n", encoding="utf-8")
    _run(origin, "add", ".")
    _run(origin, "commit", "-qm", "one")
    (origin / "one.py").write_text("one = 2\n", encoding="utf-8")
    _run(origin, "add", ".")
    _run(origin, "commit", "-qm", "two")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "--depth", "1", f"file://{origin}", str(shallow)], check=True, capture_output=True)
    shallow_result = analyze_risk(shallow)
    assert shallow_result["status"] == "error" and shallow_result["error"] == "shallow_history"


def test_diff_parsers_handle_renames_and_zero_context() -> None:
    names = parse_name_status(b"R087\0old.cpp\0new.cpp\0D\0gone.bin\0")
    assert names == [
        {"status": "R", "score": "087", "path": "new.cpp", "previous_path": "old.cpp"},
        {"status": "D", "score": None, "path": "gone.bin", "previous_path": None},
    ]
    diff = """diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old()\n+new()\n"""
    assert parse_zero_context_diff(diff)["a.py"] == {"added": ["new()"], "removed": ["old()"]}


def test_cochange_missing_test_and_score_ties_are_deterministic(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    first = analyze_risk(repo)
    second = analyze_risk(repo)
    assert first == second
    assert first["missing_cochange"]
    assert first["missing_cochange"][0]["path"] == "src/b.py"
    assert first["missing_cochange"][0]["weight"] >= COCHANGE_THRESHOLD
    assert any(item["source"] == "src/a.py" and item["test"] == "tests/test_a.py" for item in first["missing_tests"])
    assert [item["path"] for item in first["files"]] == sorted(
        (item["path"] for item in first["files"]),
        key=lambda path: (-next(item["risk_score"] for item in first["files"] if item["path"] == path), path),
    )
    assert risk_score({"diff": 1, "hotspot": 0, "fan_in": 0, "cochange": 0, "directives": 0}) == 3.0


def test_staged_and_explicit_range_and_binary_rename_delete(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "src/a.py").write_text("def a():\n    return 9\n", encoding="utf-8")
    _run(repo, "add", "src/a.py")
    staged = analyze_risk(repo, staged=True)
    assert staged["staged"] and staged["files"][0]["path"] == "src/a.py"
    explicit = analyze_risk(repo, "HEAD~1..HEAD")
    assert explicit["files"] and explicit["range"] == "HEAD~1..HEAD"

    (repo / "src/a.py").rename(repo / "src/renamed.py")
    (repo / "src/b.py").unlink()
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02")
    _run(repo, "add", "-A")
    result = analyze_risk(repo, staged=True)
    paths = {item["path"]: item for item in result["files"]}
    assert "src/renamed.py" in paths and paths["src/renamed.py"]["status"] in {"R", "A"}
    assert "src/b.py" in paths and paths["src/b.py"]["status"] == "D"
    assert paths["blob.bin"]["binary"] is True


def test_range_cannot_be_interpreted_as_git_option(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "git-output.txt"

    result = analyze_risk(repo, f"--output={target}")

    assert result["status"] == "error"
    assert result["error"] == "invalid_range"
    assert not target.exists()


def test_qt_instantiation_header_pair_signal_sites_and_qml_registration(tmp_path: Path) -> None:
    repo = _build_qt_app(tmp_path)
    # Change only the implementation: resolved QML instantiation and the
    # header/implementation partner are both untouched.
    cpp = repo / "src/DeviceManager.cpp"
    cpp.write_text(cpp.read_text(encoding="utf-8").replace("const auto deviceId = 42;", "const auto deviceId = 43;"), encoding="utf-8")
    _run(repo, "add", "src/DeviceManager.cpp")
    _run(repo, "commit", "-qm", "backend only")
    ingest_repository(repo, commit_limit=20)
    result = analyze_risk(repo)
    assert any(item["partner"] == "include/DeviceManager.hpp" for item in result["missing_qt_pairs"])
    assert any(item["qml"] == "qml/Main.qml" for item in result["missing_qt_instantiations"])

    # A declaration edit keeps the resolved graph edge but leaves the C++
    # connect site and QML handler untouched.
    header = repo / "include/DeviceManager.hpp"
    header.write_text(header.read_text(encoding="utf-8").replace("deviceConnected(int deviceId)", "deviceConnected(long deviceId)"), encoding="utf-8")
    _run(repo, "add", "include/DeviceManager.hpp")
    _run(repo, "commit", "-qm", "signal declaration only")
    ingest_repository(repo, commit_limit=20)
    result = analyze_risk(repo)
    sites = {(item["relation"], item["site"]) for item in result["missing_qt_sites"]}
    assert ("connects", "src/DeviceManager.cpp") in sites

    # New QML without a current CMake/QRC mention is a build miss; adding it to
    # the current qrc in the same staged diff clears the directive.
    new_qml = repo / "qml/New.qml"
    new_qml.write_text("Item {}\n", encoding="utf-8")
    _run(repo, "add", "qml/New.qml")
    unregistered = analyze_risk(repo, staged=True)
    assert any(item["qml"] == "qml/New.qml" for item in unregistered["build_system_misses"])
    with (repo / "resources.qrc").open("a", encoding="utf-8") as handle:
        handle.write("  <file>qml/New.qml</file>\n")
    _run(repo, "add", "resources.qrc")
    registered = analyze_risk(repo, staged=True)
    assert not any(item["qml"] == "qml/New.qml" for item in registered["build_system_misses"])


def test_swift_test_pairs_produce_missing_test_directives_only_when_untouched(tmp_path: Path) -> None:
    repo = tmp_path / "swift_repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "Sources").mkdir()
    (repo / "LingleTests").mkdir()
    source = repo / "Sources" / "Foo.swift"
    test = repo / "LingleTests" / "FooTests.swift"
    source.write_text("func foo() { }\n", encoding="utf-8")
    test.write_text("func testFoo() { }\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "initial")

    source.write_text("func foo() { print(1) }\n", encoding="utf-8")
    _run(repo, "add", "Sources/Foo.swift")
    _run(repo, "commit", "-qm", "source only")
    missing = analyze_risk(repo)
    assert {"source": "Sources/Foo.swift", "test": "LingleTests/FooTests.swift", "reason": "paired test is untouched"} in missing["missing_tests"]

    source.write_text("func foo() { print(2) }\n", encoding="utf-8")
    test.write_text("func testFoo() { print(2) }\n", encoding="utf-8")
    _run(repo, "add", ".")
    _run(repo, "commit", "-qm", "source and test")
    touched = analyze_risk(repo)
    assert not any(item["source"] == "Sources/Foo.swift" for item in touched["missing_tests"])


def test_mcp_risk_meta_ledger_and_budget(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    import cortex.mcp.tools as mcp_tools

    freshness_calls = {"count": 0}
    original_ensure_fresh = mcp_tools._ensure_fresh

    def counting_ensure_fresh(*args, **kwargs):
        freshness_calls["count"] += 1
        return original_ensure_fresh(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "_ensure_fresh", counting_ensure_fresh)
    result = call_tool("cortex_risk", {"repo_path": str(repo), "budget": 1, "response_format": "detailed"})
    payload = _payload(result)
    assert freshness_calls["count"] == 1
    assert not result["isError"]
    assert payload["_meta"]["fingerprint_fresh"] is True
    assert payload["truncated"] is True
    assert payload["returned_count"] <= len(payload["files"])
    store = CortexStore(default_db_path(repo))
    rows = store.fetch_tool_usage(repo)
    assert rows and rows[-1]["tool"] == "cortex_risk"


def test_tests_to_run_block_survives_tight_mcp_budgets(tmp_path: Path) -> None:
    """Task 10: concise cortex_risk carries tests_to_run; a tight budget drops the
    list last and never drops tests_run_all (it flips to True when the list is cut)."""
    repo = _repo(tmp_path)

    roomy = _payload(call_tool("cortex_risk", {"repo_path": str(repo), "budget": 20000}))
    assert [item["test"] for item in roomy["tests_to_run"]] == ["tests/test_a.py"]
    assert "pair" in roomy["tests_to_run"][0]["via"]
    assert roomy["tests_run_all"] is False

    for budget in (400, 120, 40, 0):
        tight = _payload(call_tool("cortex_risk", {"repo_path": str(repo), "budget": budget}))
        assert "tests_run_all" in tight and "tests_run_all_reason" in tight
        # A stripped `reason` (named in omitted_fields) leaves the list itself intact.
        if [item["test"] for item in tight.get("tests_to_run") or []] != [item["test"] for item in roomy["tests_to_run"]]:
            assert tight["tests_run_all"] is True


def _expire_inside_test_selection(monkeypatch) -> None:
    """Let every deadline check before test selection pass and expire the first one inside it."""
    from cortex import impacted_tests
    from cortex.deadline import DeadlineExceeded

    def expired() -> None:
        raise DeadlineExceeded(budget_seconds=30.0, elapsed_seconds=31.0, tool_name="cortex_risk")

    monkeypatch.setattr(impacted_tests, "check_deadline", expired)


def test_deadline_inside_test_selection_returns_a_partial_payload(tmp_path: Path, monkeypatch) -> None:
    from cortex.risk import truncate_risk_result

    repo = _repo(tmp_path)
    full = analyze_risk(repo, "HEAD~2..HEAD")
    assert "partial" not in full and full["tests_to_run"]

    _expire_inside_test_selection(monkeypatch)
    result = analyze_risk(repo, "HEAD~2..HEAD")

    assert result["partial"] is True
    assert result["tests_run_all"] is True
    assert result["tests_run_all_reason"] == "time budget exceeded during test selection"
    assert result["tests_to_run"] == [] and result["tests_by_changed_file"] == {}
    # The ranking computed before test selection is the one a complete run returns.
    assert [item["path"] for item in result["files"]] == [item["path"] for item in full["files"]]
    assert result["files"]
    # Test selection never finished, so no changed file was shown to be uncovered: they are untraced.
    assert result["uncovered_changed_files"] == []
    assert result["untraced_changed_files_total"] == len(result["files"])

    truncated = truncate_risk_result(result, 2000)
    assert truncated["partial"] is True
    assert truncated["tests_run_all"] is True
    assert truncated["tests_run_all_reason"] == "time budget exceeded during test selection"
    assert [item["path"] for item in truncated["files"]] == [item["path"] for item in full["files"]]

    # Even the minimal fallback payload (a budget too small for the ordinary one) keeps the flag.
    tiny = truncate_risk_result(result, 150)
    assert tiny["partial"] is True and tiny["tests_run_all"] is True


def test_a_deadline_in_test_selection_counts_untraced_files_not_uncovered_ones(tmp_path: Path, monkeypatch) -> None:
    from cortex.impacted_tests import is_documentation, is_runnable_test
    from cortex.risk import _RISK_MINIMAL_KEYS, _RISK_SCALAR_KEYS, truncate_risk_result

    repo = _repo(tmp_path)
    _expire_inside_test_selection(monkeypatch)
    result = analyze_risk(repo, "HEAD~2..HEAD")

    # The same filter `analyze_risk` applies: not deleted, not a runnable test, not documentation.
    reviewable = [
        item["path"] for item in result["files"]
        if item["status"] != "D" and not is_runnable_test(item["path"]) and not is_documentation(item["path"])
    ]
    assert result["partial"] is True
    assert result["uncovered_changed_files"] == []
    assert result["untraced_changed_files_total"] == len(reviewable) > 0
    assert "untraced_changed_files_total" in _RISK_SCALAR_KEYS and "untraced_changed_files_total" in _RISK_MINIMAL_KEYS

    for budget in (150, 500, 2000, 8000):
        truncated = truncate_risk_result(result, budget)
        assert truncated["untraced_changed_files_total"] == result["untraced_changed_files_total"], budget
        assert "uncovered_changed_files_total" not in truncated, budget


def test_uncovered_changed_files_total_does_not_depend_on_the_budget() -> None:
    from cortex.risk import truncate_risk_result
    from tests.test_risk_truncate import _large_diff_result

    result = _large_diff_result()
    full = len(result["uncovered_changed_files"])
    for budget in range(500, 20001, 500):
        out = truncate_risk_result(result, budget)
        assert out.get("uncovered_changed_files_total", len(out["uncovered_changed_files"])) == full, budget
        assert "untraced_changed_files_total" not in out, budget


def test_mcp_risk_partial_payload_is_not_a_deadline_error_or_cached(tmp_path: Path, monkeypatch) -> None:
    from cortex import impacted_tests

    repo = _repo(tmp_path)
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    monkeypatch.setenv("CORTEX_QUERY_CACHE", "1")
    real_check_deadline = impacted_tests.check_deadline
    _expire_inside_test_selection(monkeypatch)
    result = call_tool("cortex_risk", {"repo_path": str(repo), "range": "HEAD~2..HEAD", "budget": 20000})
    assert result.get("isError") is not True
    payload = _payload(result)
    assert payload["partial"] is True and payload["files"]
    monkeypatch.setattr(impacted_tests, "check_deadline", real_check_deadline)
    again = _payload(call_tool("cortex_risk", {"repo_path": str(repo), "range": "HEAD~2..HEAD", "budget": 20000}))
    assert "partial" not in again and again["tests_to_run"], "a partial payload must not be served from the cache"
