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
