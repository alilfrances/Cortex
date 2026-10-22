"""Task 10: impacted tests (``tests_to_run`` / ``tests_run_all``) in cortex_risk."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cortex.cli import main as cli_main
from cortex.impacted_tests import is_runnable_test, parse_changed_lines
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.risk import analyze_risk, truncate_risk_result
from tests.baseline_helpers import commit_all, fixed_git_env, git, init_git
from tests.golden_utils import force_stdlib_tokenizer, treesitter_available


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    force_stdlib_tokenizer(monkeypatch)
    fixed_git_env(monkeypatch)


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _by_test(result: dict) -> dict[str, dict]:
    return {item["test"]: item for item in result["tests_to_run"]}


# --------------------------------------------------------------------------- #
# Python: structural / cochange / pair tiers
# --------------------------------------------------------------------------- #

A_PY = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
TEST_A = "from a import f\n\n\ndef test_f():\n    assert f() == 1\n"


def python_repo(tmp_path: Path) -> Path:
    """``test_a`` imports and calls ``a.f``; ``test_history`` only co-changes
    with ``b.py`` (three times, the strongest pair; its name pairs with nothing);
    ``test_c`` pairs by name."""
    repo = tmp_path / "py"
    init_git(repo)
    _write(repo, "a.py", A_PY)
    _write(repo, "b.py", "VALUE = 1\n\n\ndef b():\n    return VALUE\n")
    _write(repo, "c.py", "def c():\n    return 3\n")
    _write(repo, "tests/test_a.py", TEST_A)
    _write(repo, "tests/test_history.py", "def test_b_value():\n    assert 1\n")
    _write(repo, "tests/test_c.py", "def test_c_value():\n    assert 3\n")
    _write(repo, "pyproject.toml", "[project]\nname = 'x'\n")
    _write(repo, "README.md", "# x\n")
    commit_all(repo, "initial")
    for value in (2, 3, 4):
        _write(repo, "b.py", f"VALUE = {value}\n\n\ndef b():\n    return VALUE\n")
        _write(repo, "tests/test_history.py", f"def test_b_value():\n    assert {value}\n")
        commit_all(repo, f"b and its test {value}")
    return repo


def _change(repo: Path, rel: str, text: str, message: str = "change") -> dict:
    _write(repo, rel, text)
    commit_all(repo, message)
    ingest_repository(repo, commit_limit=50)
    return analyze_risk(repo)


def test_function_body_change_selects_the_calling_test_structurally(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)

    result = _change(repo, "a.py", A_PY.replace("return 1", "return 10"))

    tests = _by_test(result)
    assert tests["tests/test_a.py"]["via"] == ["pair", "structural"]
    assert tests["tests/test_a.py"]["score"] == 1.0
    assert "structural: calls path (depth 1) from a.py:f" in tests["tests/test_a.py"]["reason"]
    assert "tests/test_history.py" not in tests and "tests/test_c.py" not in tests
    assert result["tests_run_all"] is False and result["tests_run_all_reason"] == ""
    assert "tests_to_run: tests/test_a.py (pair,structural)" in result["directives"]


def test_changed_span_precision_uncalled_function_is_not_structural(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)

    result = _change(repo, "a.py", A_PY.replace("return 2", "return 20"))

    # g() is in the same file as f(), but no test calls it: only the name pair remains.
    assert _by_test(result)["tests/test_a.py"]["via"] == ["pair"]
    assert _by_test(result)["tests/test_a.py"]["score"] == 0.6


def test_cochange_history_selects_a_test_with_no_graph_edge(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)

    result = _change(repo, "b.py", "VALUE = 9\n\n\ndef b():\n    return VALUE\n")

    tests = _by_test(result)
    assert list(tests) == ["tests/test_history.py"]
    assert tests["tests/test_history.py"]["via"] == ["cochange"]
    assert tests["tests/test_history.py"]["score"] == 1.0
    assert result["tests_run_all"] is False


def test_filename_pair_selects_a_test(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)

    result = _change(repo, "c.py", "def c():\n    return 30\n")

    assert result["tests_to_run"] == [
        {"test": "tests/test_c.py", "via": ["pair"], "reason": "pair: test file name pairs with c.py", "score": 0.6}
    ]
    assert result["tests_run_all"] is False
    # missing_tests keeps its 7b0e6e0 behaviour next to the new block.
    assert result["missing_tests"] == [{"source": "c.py", "test": "tests/test_c.py", "reason": "paired test is untouched"}]


def test_build_config_change_runs_everything(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)

    result = _change(repo, "pyproject.toml", "[project]\nname = 'y'\n")

    assert result["tests_run_all"] is True
    assert result["tests_run_all_reason"] == "build/config file changed: pyproject.toml"


def test_changed_test_and_docs_only(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)
    _write(repo, "README.md", "# y\n")
    result = _change(repo, "tests/test_c.py", "def test_c_value():\n    assert 30\n")

    assert result["tests_to_run"] == [
        {"test": "tests/test_c.py", "via": ["changed"], "reason": "changed: test file is part of this diff", "score": 1.0}
    ]
    assert result["tests_run_all"] is False  # README.md needs no test


def test_unreached_file_or_deletion_runs_everything(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)
    result = _change(repo, "orphan.py", "def lonely():\n    return 0\n")
    assert result["tests_run_all"] is True
    assert result["tests_run_all_reason"] == "no test reaches changed file orphan.py"

    git(repo, "rm", "-q", "orphan.py")
    commit_all(repo, "delete orphan")
    ingest_repository(repo, commit_limit=50)
    deleted = analyze_risk(repo)
    assert deleted["tests_run_all"] is True
    assert deleted["tests_run_all_reason"].startswith("deleted file orphan.py")


def test_missing_index_and_no_tests_run_everything(tmp_path: Path) -> None:
    repo = tmp_path / "noindex"
    init_git(repo)
    _write(repo, "one.py", "one = 1\n")
    commit_all(repo, "one")
    _write(repo, "one.py", "one = 2\n")
    commit_all(repo, "two")
    missing = analyze_risk(repo)
    assert missing["tests_run_all"] is True
    assert missing["tests_run_all_reason"] == "Cortex index is missing; impacted tests cannot be traced"

    ingest_repository(repo, commit_limit=10)
    no_tests = analyze_risk(repo)
    assert no_tests["tests_run_all"] is True and no_tests["tests_run_all_reason"] == "no test files are indexed"


def test_mcp_concise_carries_the_block(tmp_path: Path) -> None:
    repo = python_repo(tmp_path)
    _change(repo, "c.py", "def c():\n    return 30\n")

    payload = json.loads(call_tool("cortex_risk", {"repo_path": str(repo)})["content"][0]["text"])

    assert [item["test"] for item in payload["tests_to_run"]] == ["tests/test_c.py"]
    assert payload["tests_run_all"] is False and payload["tests_run_all_reason"] == ""


# --------------------------------------------------------------------------- #
# CLI --tests-only
# --------------------------------------------------------------------------- #


def _cli(repo: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["cortex", "risk", *argv])
    try:
        cli_main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def test_cli_tests_only_prints_paths_or_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = python_repo(tmp_path)
    _change(repo, "a.py", A_PY.replace("return 1", "return 10"))
    assert _cli(repo, monkeypatch, "--tests-only") == 0
    assert capsys.readouterr().out == "tests/test_a.py\n"

    _change(repo, "pyproject.toml", "[project]\nname = 'z'\n")
    assert _cli(repo, monkeypatch, "--tests-only") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "build/config file changed: pyproject.toml" in captured.err

    assert _cli(repo, monkeypatch, "--tests-only", "--output=/tmp/x") == 2
    assert capsys.readouterr().out == ""

    assert _cli(repo, monkeypatch) == 0
    assert "Run all tests: build/config file changed: pyproject.toml" in capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------- #
# Qt parity
# --------------------------------------------------------------------------- #

DEVICE_HPP = """#pragma once
#include <QObject>
class Device : public QObject {
    Q_OBJECT
public:
    explicit Device(QObject *parent = nullptr);
    void scan();
signals:
    void found(int id);
public slots:
    void onFound(int id);
};
"""

DEVICE_CPP = """#include "Device.hpp"
#include "helper.hpp"

Device::Device(QObject *parent) : QObject(parent) {}

void Device::scan() {
    emit found(1);
}

void Device::onFound(int id) {
    helper(id);
}
"""


def qt_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "qt"
    init_git(repo)
    _write(repo, "include/Device.hpp", DEVICE_HPP)
    _write(repo, "src/Device.cpp", DEVICE_CPP)
    _write(repo, "include/helper.hpp", "#pragma once\nint helper(int x);\n")
    _write(repo, "src/helper.cpp", '#include "helper.hpp"\nint helper(int x) {\n    return x + 1;\n}\n')
    # connect() to the slot is written in the test; both endpoints are header symbols.
    _write(repo, "tests/tst_device.cpp", '#include "Device.hpp"\nvoid testWiring() {\n    Device d;\n'
           "    QObject::connect(&d, &Device::found, &d, &Device::onFound);\n}\n")
    _write(repo, "tests/tst_qml.qml", "import QtQuick 2.15\nItem {\n    Device { }\n}\n")
    _write(repo, "qml/Main.qml", "import QtQuick 2.15\nItem {\n    Device { id: dev }\n}\n")
    _write(repo, "tests/tst_main.cpp", "int main() { return 0; }\n")
    _write(repo, "CMakeLists.txt", "add_executable(app src/Device.cpp src/helper.cpp)\n"
           "qt_add_executable(tst_main tests/tst_main.cpp)\n"
           "qt_add_qml_module(tst_main URI T QML_FILES qml/Main.qml)\n")
    _write(repo, "notes/log.txt", "0\n")
    commit_all(repo, "initial")
    # Raise the co-change maximum so the initial commit's pairs stay below 0.5.
    for value in range(1, 4):
        _write(repo, "notes/log.txt", f"{value}\n")
        _write(repo, "notes/more.txt", f"{value}\n")
        commit_all(repo, f"notes {value}")
    return repo


QT_BACKENDS = pytest.mark.parametrize("backend", ["treesitter", "regex"])


def _set_backend(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if backend == "regex":
        monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    elif not treesitter_available():
        pytest.skip("managed tree-sitter runtime not usable here")


@QT_BACKENDS
def test_cpp_slot_body_change_selects_connect_site_and_instantiating_test(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_backend(backend, monkeypatch)
    repo = qt_repo(tmp_path)

    result = _change(repo, "src/Device.cpp", DEVICE_CPP.replace("helper(id);", "helper(id + 1);"))

    tests = _by_test(result)
    # The slot's out-of-line definition is bridged to its header declaration,
    # which the test's connect() targets (site = tests/tst_device.cpp).
    assert "structural" in tests["tests/tst_device.cpp"]["via"]
    assert "connects path (depth 1) from src/Device.cpp:onFound" in tests["tests/tst_device.cpp"]["reason"]
    # The owning type Device is instantiated by a QML test.
    assert "structural" in tests["tests/tst_qml.qml"]["via"]
    assert "instantiates path (depth 2) from include/Device.hpp:Device" in tests["tests/tst_qml.qml"]["reason"]
    assert "tests/tst_main.cpp" not in tests
    assert result["tests_run_all"] is False


@QT_BACKENDS
def test_cpp_change_outside_the_slot_does_not_select_the_connect_site(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_backend(backend, monkeypatch)
    repo = qt_repo(tmp_path)

    result = _change(repo, "src/Device.cpp", DEVICE_CPP.replace("emit found(1);", "emit found(2);"))

    tests = _by_test(result)
    # scan() is not connected anywhere, so the connect site is not reached structurally.
    # The QTest-named file still pairs with Device.cpp by name (tst_<name>.cpp, 0.12).
    assert tests["tests/tst_device.cpp"]["via"] == ["pair"]
    assert "structural" in tests["tests/tst_qml.qml"]["via"]  # still a Device member


@QT_BACKENDS
def test_qml_change_selects_the_test_target_that_registers_it(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_backend(backend, monkeypatch)
    repo = qt_repo(tmp_path)

    result = _change(repo, "qml/Main.qml", "import QtQuick 2.15\nItem {\n    Device { id: device }\n}\n")

    tests = _by_test(result)
    # Main.qml <-registers- target:tst_main -builds-> tests/tst_main.cpp
    assert tests["tests/tst_main.cpp"]["via"] == ["structural"]
    assert tests["tests/tst_main.cpp"]["reason"].startswith("structural: builds path (depth 2) from qml/Main.qml")
    assert "tests/tst_device.cpp" not in tests
    assert result["tests_run_all"] is False


@QT_BACKENDS
def test_helper_called_from_a_slot_reaches_the_connect_site_or_runs_all(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """helper() <-calls- Device::onFound (.cpp) == Device::onFound (.hpp) <-connects- (site: test).

    The regex fallback has only name-only (unresolved) calls, so nothing can be
    vouched for and the whole suite is requested instead of a silent miss."""
    _set_backend(backend, monkeypatch)
    repo = qt_repo(tmp_path)

    result = _change(repo, "src/helper.cpp", '#include "helper.hpp"\nint helper(int x) {\n    return x + 2;\n}\n')

    if backend == "treesitter":
        assert result["tests_to_run"] == [{
            "test": "tests/tst_device.cpp", "via": ["structural"],
            "reason": "structural: connects path (depth 2) from src/helper.cpp:helper", "score": 0.5,
        }]
        assert result["tests_run_all"] is False
    else:
        assert result["tests_to_run"] == []
        assert result["tests_run_all"] is True
        assert result["tests_run_all_reason"] == "no test reaches changed file src/helper.cpp"


# --------------------------------------------------------------------------- #
# Units: hunk parsing, runnable tests, truncation
# --------------------------------------------------------------------------- #


def test_parse_changed_lines_uses_new_side_and_ignores_blank_lines() -> None:
    raw = "\n".join([
        "diff --git a/x.py b/x.py",
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -3 +3 @@ def f():",
        "-    return 1",
        "+    return 2",
        "@@ -10,0 +11,2 @@",
        "+",
        "+def h():",
        "@@ -20,2 +21,0 @@",
        "-def gone():",
        "-    pass",
        "@@ -30 +29,0 @@",
        "-",
        "diff --git a/old.py b/old.py",
        "--- a/old.py",
        "+++ /dev/null",
        "@@ -1 +0,0 @@",
        "-x = 1",
        "diff --git a/bin.png b/bin.png",
        "Binary files a/bin.png and b/bin.png differ",
    ])

    parsed = parse_changed_lines(raw)

    assert parsed["x.py"].lines == {3, 12, 21, 22}
    assert parsed["x.py"].hunks == 4
    assert parsed["old.py"].lines == {1, 2}
    assert "bin.png" not in parsed


def test_runnable_test_detection() -> None:
    assert is_runnable_test("tests/test_a.py")
    assert is_runnable_test("pkg/a_test.go")
    assert is_runnable_test("tests/tst_device.cpp")
    assert is_runnable_test("tests/tst_qml.qml")
    assert is_runnable_test("src/__tests__/app.js")
    assert is_runnable_test("tests/integration.rs")
    assert not is_runnable_test("tests/helpers.py")
    assert not is_runnable_test("tests/conftest.py")
    assert not is_runnable_test("tests/fixtures/data.json")
    assert not is_runnable_test("tests/support.hpp")
    assert not is_runnable_test("src/a.py")


def _synthetic(tests: int) -> dict:
    return {
        "status": "ok", "files": [], "directives": [],
        "tests_to_run": [
            {"test": f"tests/test_{i:02d}.py", "via": ["structural"], "reason": "structural: x", "score": 1.0}
            for i in range(tests)
        ],
        "tests_run_all": False, "tests_run_all_reason": "",
    }


def test_truncation_drops_tests_last_and_switches_to_run_all() -> None:
    from cortex.tokenizer import count_text_tokens

    result = _synthetic(12)
    result["missing_tests"] = [{"source": "a.py", "test": "t.py", "reason": "r"}]
    result["directives"] = ["missing_tests: t.py for a.py"]
    full = count_text_tokens(json.dumps(result, sort_keys=True))

    roomy = truncate_risk_result(result, full + 50)
    assert roomy["tests_to_run"] == result["tests_to_run"] and roomy["tests_run_all"] is False

    seen_partial = False
    for budget in range(full, 0, -1):
        out = truncate_risk_result(result, budget)
        assert "tests_run_all" in out and "tests_run_all_reason" in out  # never dropped
        if out["tests_to_run"]:
            # Anything else droppable is gone before the first test is popped.
            assert "missing_tests" not in out or len(out["tests_to_run"]) == 12
            if len(out["tests_to_run"]) < 12:
                assert out["directives"] == [] and "missing_tests" not in out
                # (Under pressure `reason` is stripped first, 0.12.1.)
                kept = [{k: v for k, v in t.items() if k != "reason"} for t in result["tests_to_run"]]
                assert out["tests_to_run"] == kept[: len(out["tests_to_run"])]
                assert out["tests_run_all"] is True
                assert out["tests_run_all_reason"] == "tests_to_run was truncated to fit the response budget"
                seen_partial = True
    assert seen_partial
    tiny = truncate_risk_result(result, 0)
    assert tiny["tests_to_run"] == [] and tiny["tests_run_all"] is True


# --------------------------------------------------------------------------- #
# Task 11 findings on vendored upstream layouts (evals/fixtures/oss)
# --------------------------------------------------------------------------- #


def test_runnable_test_detection_follows_upstream_runner_globs() -> None:
    # rack: Rake::TestTask `test/**/spec_*.rb`; minitest `test_*.rb`.
    assert is_runnable_test("test/spec_etag.rb")
    assert is_runnable_test("test/test_utils.rb")
    assert not is_runnable_test("test/helper.rb")
    # p-queue: `node --test test/*.ts`; nested support files are not test files.
    assert is_runnable_test("test/basic.ts")
    assert is_runnable_test("tests/app.js")
    assert not is_runnable_test("test/fixtures/types.ts")
    assert not is_runnable_test("test/_setup.ts")
    assert not is_runnable_test("src/basic.ts")
    # semver: Cargo targets are tests/*.rs and tests/<name>/main.rs; modules are helpers.
    assert is_runnable_test("tests/test_version.rs")
    assert is_runnable_test("tests/suite/main.rs")
    assert not is_runnable_test("tests/util/mod.rs")
    assert not is_runnable_test("tests/util/helpers.rs")


def _cpp_header_repo(tmp_path: Path) -> Path:
    """SortFilterProxyModel shape: `tests/indexsorter.h` includes `sorters/sorter.h`
    and derives from a namespace-qualified base; `tests/indexsorter.cpp` includes it."""
    repo = tmp_path / "cpp"
    init_git(repo)
    _write(repo, "sorters/sorter.h", "namespace ns {\nclass Sorter\n{\npublic:\n    int priority() const;\n};\n}\n")
    _write(repo, "sorters/sorter.cpp", '#include "sorter.h"\nnamespace ns {\nint Sorter::priority() const { return 0; }\n}\n')
    _write(repo, "tests/indexsorter.h", '#include "sorters/sorter.h"\nclass IndexSorter : public ns::Sorter\n{\n};\n')
    _write(repo, "tests/indexsorter.cpp", '#include "indexsorter.h"\nint use() { return 1; }\n')
    commit_all(repo, "initial")
    return repo


@pytest.mark.skipif(not treesitter_available(), reason="managed tree-sitter runtime is not ready")
def test_cpp_header_symbol_change_reaches_includers(tmp_path: Path) -> None:
    repo = _cpp_header_repo(tmp_path)
    text = (repo / "sorters/sorter.h").read_text(encoding="utf-8").replace(
        "public:\n", "public:\n    // changed\n"
    )
    result = _change(repo, "sorters/sorter.h", text)

    hit = _by_test(result)["tests/indexsorter.cpp"]
    assert "structural" in hit["via"]
    assert result["tests_run_all"] is False


@pytest.mark.skipif(not treesitter_available(), reason="managed tree-sitter runtime is not ready")
def test_inherits_edges_with_unresolved_base_names_are_reverse_hops(tmp_path: Path) -> None:
    """`class Child extends Base` keeps `name:Base`; a change to Base must reach the
    test that uses only Child."""
    repo = tmp_path / "java"
    init_git(repo)
    _write(repo, "src/Base.java", "public class Base {\n    public int run() {\n        return 1;\n    }\n}\n")
    _write(repo, "src/Child.java", "public class Child extends Base {\n}\n")
    _write(repo, "test/ChildTest.java", "public class ChildTest {\n    void testChild() {\n        Child child = new Child();\n    }\n}\n")
    commit_all(repo, "initial")
    text = "public class Base {\n    public int run() {\n        // changed\n        return 1;\n    }\n}\n"
    result = _change(repo, "src/Base.java", text)

    hit = _by_test(result)["test/ChildTest.java"]
    assert "structural" in hit["via"]


def test_coverage_breakdown_names_tests_per_changed_file_and_the_uncovered_ones(tmp_path):
    """The partial list stays visible next to a run-all verdict, per changed file."""
    import subprocess

    from cortex.ingest import ingest_repository
    from cortex.risk import analyze_risk

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    (repo / "covered.py").write_text("def covered():\n    return 1\n")
    (repo / "test_covered.py").write_text("from covered import covered\n\n\ndef test_it():\n    assert covered() == 1\n")
    git("add", "-A")
    git("commit", "-qm", "one")
    (repo / "orphan.py").write_text("def orphan():\n    return 1\n")  # never changed together with a test
    git("add", "-A")
    git("commit", "-qm", "two")
    (repo / "covered.py").write_text("def covered():\n    return 2\n")
    (repo / "orphan.py").write_text("def orphan():\n    return 2\n")
    git("commit", "-qam", "three")
    import os
    os.environ["CORTEX_DATA_DIR"] = str(tmp_path / "data")
    ingest_repository(repo)

    result = analyze_risk(repo, "HEAD~1..HEAD")

    assert result["tests_run_all"] is True
    assert result["tests_by_changed_file"] == {"covered.py": ["test_covered.py"]}
    assert result["uncovered_changed_files"] == ["orphan.py"]
    assert "orphan.py" in result["tests_run_all_reason"]


# --- base-label attachment cap (0.12) ---


def _class(node_id: str, path: str):
    from cortex.models import GraphNode

    label = node_id.rsplit(":", 1)[-1]
    return GraphNode(
        node_id=node_id, kind="class", label=label, source_ref=path, granularity="symbol",
        span_start=1, span_end=5, metadata={},
    )


def _inherits(source: str, base: str):
    from cortex.models import GraphEdge

    return GraphEdge(
        edge_id=f"e:{source}", source=source, target=base, relation="inherits",
        layer="STRUCTURAL", confidence="LOW", weight=1.0, metadata={"source_file": "x.h"},
    )


@pytest.mark.parametrize(("same_label", "attached"), [(2, True), (5, False)])
def test_unresolved_inherits_attaches_only_to_unambiguous_labels(same_label: int, attached: bool) -> None:
    from cortex.impacted_tests import _GraphIndex

    bases = [_class(f"symbol:base{i}.h:Item", f"base{i}.h") for i in range(same_label)]
    child = _class("symbol:child.h:Child", "child.h")
    index = _GraphIndex([*bases, child], [_inherits(child.node_id, "name:Item")])

    assert any(index.reverse.get(base.node_id) for base in bases) is attached


@pytest.mark.parametrize(("same_label", "attached"), [(2, True), (5, False)])
def test_a_resolver_rewritten_inherits_edge_selects_tests_exactly_as_the_label_edge_did(same_label: int, attached: bool) -> None:
    """0.12.2: the call resolver rewrites `name:Item` to the one class it prefers and keeps the label in
    `unresolved_target`. Test selection still attaches the edge to EVERY same-label class (a mock base
    included, the safe over-selection) and still skips labels shared by more than LABEL_ATTACH_MAX classes."""
    from cortex.impacted_tests import _GraphIndex

    real = _class("symbol:src/item.h:Item", "src/item.h")
    others = [_class(f"symbol:tests/mocks/m{i}/item.h:Item", f"tests/mocks/m{i}/item.h") for i in range(same_label - 1)]
    child = _class("symbol:src/child.h:Child", "src/child.h")
    edge = _inherits(child.node_id, real.node_id)
    edge.metadata["unresolved_target"] = "name:Item"
    label_edge = _inherits(child.node_id, "name:Item")

    rewritten = _GraphIndex([real, *others, child], [edge])
    original = _GraphIndex([real, *others, child], [label_edge])

    for base in (real, *others):
        assert bool(rewritten.reverse.get(base.node_id)) is attached
        assert [e.source for e in rewritten.reverse.get(base.node_id, [])] == [
            e.source for e in original.reverse.get(base.node_id, [])
        ]


# --- production-over-mock edges are not walked (0.14: review 0.13.0 section C) ---

STEP_H = "#pragma once\nclass Step {\npublic:\n    explicit Step(int v);\n    int run();\n};\n"
OPS_CPP = '#include "ops.h"\n#include "steps/step.h"\n\nint runOp() {\n    Step *step = new Step(1);\n    return step->run();\n}\n'


def _mock_step_repo(tmp_path: Path) -> Path:
    """`src/ops.cpp` builds `new Step`; `src/steps/step.h` and a mock under `tests/header-mocks` both define it.
    The test target lists the mock directory ahead of `src`, so `opstest` compiles the mock, not the real header."""
    repo = tmp_path / "mockstep"
    init_git(repo)
    _write(repo, "src/steps/step.h", STEP_H)
    _write(repo, "tests/header-mocks/steps/step.h", STEP_H.replace("int run();", "int run() { return 0; }"))
    _write(repo, "src/ops.cpp", OPS_CPP)
    _write(repo, "src/ops.h", "#pragma once\nint runOp();\n")
    _write(repo, "tests/opstest.cpp", '#include "ops.h"\n\nvoid testOp() {\n    runOp();\n}\n')
    _write(repo, "tests/CMakeLists.txt", "add_executable(opstest opstest.cpp ../src/ops.cpp)\n"
           "target_include_directories(opstest PRIVATE header-mocks ../src)\n")
    _write(repo, "notes/log.txt", "0\n")
    commit_all(repo, "initial")
    for value in range(1, 4):  # keep the initial commit's co-change pairs below the test threshold
        _write(repo, "notes/log.txt", f"{value}\n")
        _write(repo, "notes/more.txt", f"{value}\n")
        commit_all(repo, f"notes {value}")
    return repo


@pytest.mark.skipif(not treesitter_available(), reason="managed tree-sitter runtime is not ready")
def test_a_header_whose_calls_were_resolved_only_by_preferring_production_maps_to_no_test(tmp_path: Path) -> None:
    repo = _mock_step_repo(tmp_path)

    result = _change(repo, "src/steps/step.h", STEP_H.replace("int run();", "int run();\n    int extra();"))

    assert "tests/opstest.cpp" not in result["tests_by_changed_file"].get("src/steps/step.h", [])
    assert "tests/opstest.cpp" not in _by_test(result)
    assert result["tests_run_all"] is True  # nothing vouches for the header: the honest answer, as in 0.12.1

    from cortex.store import CortexStore, default_db_path

    _, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    new_step = [e for e in edges if e.relation == "calls" and e.metadata.get("callee") == "Step"]
    assert [(e.source, e.target) for e in new_step] == [("symbol:src/ops.cpp:runOp", "symbol:src/steps/step.h:Step")]
    assert new_step[0].metadata["preferred"] == "production"  # the call graph keeps the production answer


@pytest.mark.skipif(not treesitter_available(), reason="managed tree-sitter runtime is not ready")
def test_an_include_that_resolved_by_exact_path_stays_walked_despite_a_mock_twin(tmp_path: Path) -> None:
    """Only an include newly resolved by the includer-relative rule is shadowable: `#include "src/steps/step.h"`
    resolved by its exact path in 0.12.1 too, so the header still selects the test that builds the includer."""
    repo = _mock_step_repo(tmp_path)
    _write(repo, "src/ops.cpp", OPS_CPP.replace('"steps/step.h"', '"src/steps/step.h"'))
    commit_all(repo, "exact path include")

    result = _change(repo, "src/steps/step.h", STEP_H.replace("int run();", "int run();\n    int extra();"))

    assert result["tests_by_changed_file"]["src/steps/step.h"] == ["tests/opstest.cpp"]
    assert result["tests_run_all"] is False


@QT_BACKENDS
def test_a_change_to_the_caller_of_a_preferred_edge_still_maps_to_its_test(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_backend(backend, monkeypatch)
    repo = _mock_step_repo(tmp_path)

    result = _change(repo, "src/ops.cpp", OPS_CPP.replace("return step->run();", "return step->run() + 1;"))

    assert result["tests_by_changed_file"]["src/ops.cpp"] == ["tests/opstest.cpp"]
    assert "structural" in _by_test(result)["tests/opstest.cpp"]["via"]
