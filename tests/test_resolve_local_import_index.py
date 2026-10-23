from __future__ import annotations

import re
import time
from pathlib import PurePosixPath

from cortex.structural import regex_backend

PATHS = {
    "src/a/widget.h",
    "src/a/widget.cpp",
    "src/b/other.hpp",
    "src/b/dup.h",
    "src/c/dup.h",
    "ui/Main.qml",
    "ui/Main.qml.bak",
    "src/device_list_model.hpp",
}


def _old_resolve_local_import(target: str, known_paths: set[str]) -> str | None:
    candidate = target.strip().strip('"').lstrip("./")
    if not candidate:
        return None
    if candidate in known_paths:
        return candidate
    basename = PurePosixPath(candidate).name
    matches = [p for p in known_paths if PurePosixPath(p).name == basename]
    return matches[0] if len(matches) == 1 else None


def _old_resolve_qml_cpp_type(name: str, known_paths: set[str]) -> str | None:
    if not name or not name[0].isupper():
        return None
    for suffix in (".hpp", ".h", ".hh", ".hxx", ".cpp", ".cc", ".cxx"):
        resolved = _old_resolve_local_import(f"{name}{suffix}", known_paths)
        if resolved is not None:
            return resolved
    normalised_name = re.sub(r"[^a-z0-9]", "", name.lower())
    candidates = [
        path for path in known_paths
        if PurePosixPath(path).suffix.lower() in regex_backend._CPP_SUFFIXES
        and re.sub(r"[^a-z0-9]", "", PurePosixPath(path).stem.lower()) == normalised_name
    ]
    return sorted(candidates)[0] if len(candidates) == 1 else None


def test_resolve_local_import_matches_the_linear_scan():
    for target in ["widget.h", "a/widget.h", "src/a/widget.cpp", "dup.h", "missing.h", '"other.hpp"', "./Main.qml", "", "  "]:
        assert regex_backend.resolve_local_import(target, PATHS) == _old_resolve_local_import(target, PATHS), target


def test_resolve_qml_cpp_type_matches_the_linear_scan():
    for name in ["Widget", "Other", "DeviceListModel", "Dup", "Missing", "lower", ""]:
        assert regex_backend.resolve_qml_cpp_type(name, PATHS) == _old_resolve_qml_cpp_type(name, PATHS), name


def test_index_is_not_reused_for_a_different_path_set():
    first = {"x/one.h"}
    second = {"y/one.h", "y/two.h"}
    assert regex_backend.resolve_local_import("one.h", first) == "x/one.h"
    assert regex_backend.resolve_local_import("one.h", second) == "y/one.h"
    assert regex_backend.resolve_local_import("two.h", first) is None


def test_index_follows_a_grown_path_set():
    paths = {"a/one.h"}
    assert regex_backend.resolve_local_import("two.h", paths) is None
    paths.add("b/two.h")
    assert regex_backend.resolve_local_import("two.h", paths) == "b/two.h"


def test_basename_lookups_scale_linearly():
    paths = {f"dir{i % 50}/file{i}.hpp" for i in range(20000)}
    started = time.perf_counter()
    for i in range(10000):
        regex_backend.resolve_local_import(f"file{i}.hpp", paths)
    assert time.perf_counter() - started < 1.0


STEP_PATHS = {
    "src/steps/stepbase.h",
    "tests/header-mocks/stepbase.h",
    "tests/header-mocks/steps/stepa.h",
    "src/steps/stepa.h",
    "src/app/x.cpp",
}


def test_quoted_include_prefers_the_including_files_directory():
    resolve = regex_backend.resolve_local_import
    # Duplicated basename: without `from_path` it stays unresolved (unchanged).
    assert resolve("stepbase.h", STEP_PATHS) is None
    # Nearest ancestor holding the target: the mock tree resolves to the mock base.
    assert resolve("stepbase.h", STEP_PATHS, from_path="tests/header-mocks/steps/stepa.h") == "tests/header-mocks/stepbase.h"
    assert resolve("stepbase.h", STEP_PATHS, from_path="src/steps/stepa.h") == "src/steps/stepbase.h"


def test_quoted_include_tries_each_ancestor_then_the_basename():
    resolve = regex_backend.resolve_local_import
    # `steps/stepbase.h` from src/app/x.cpp: the ancestor `src/` holds it.
    assert resolve("steps/stepbase.h", STEP_PATHS, from_path="src/app/x.cpp") == "src/steps/stepbase.h"
    # Nothing relative matches: fall back to the old exact / unique-basename rules.
    assert resolve("stepa.h", STEP_PATHS, from_path="src/app/x.cpp") is None  # two stepa.h
    assert resolve("x.cpp", STEP_PATHS, from_path="tests/header-mocks/steps/stepa.h") == "src/app/x.cpp"
    # An ancestor-relative exact path still wins over the old exact match.
    paths = {"lib/a/util.h", "util.h", "lib/b/main.cpp"}
    assert regex_backend.resolve_local_import("util.h", paths, from_path="lib/a/main.cpp") == "lib/a/util.h"
    assert regex_backend.resolve_local_import("util.h", paths) == "util.h"


def test_quoted_include_normalises_dotdot_and_rejects_root_escapes():
    resolve = regex_backend.resolve_local_import
    assert resolve("../stepbase.h", STEP_PATHS, from_path="tests/header-mocks/steps/stepa.h") == "tests/header-mocks/stepbase.h"
    assert resolve("./stepa.h", STEP_PATHS, from_path="src/steps/other.h") == "src/steps/stepa.h"
    # `../../../etc/x.h` leaves the repository root from every directory: no relative hit.
    assert resolve("../../../etc/x.h", STEP_PATHS | {"etc/y.h"}, from_path="src/steps/stepa.h") is None
    # An escaping candidate is skipped, not matched; the old basename/exact rules still apply.
    assert resolve("../../../stepbase.h", {"stepbase.h", "a/stepbase.h", "b/stepbase.h"}, from_path="a/b/c.h") == "stepbase.h"
    assert resolve("/abs/stepbase.h", STEP_PATHS, from_path="src/steps/stepa.h") is None


def test_include_lookup_without_from_path_is_unchanged():
    for target in ["widget.h", "a/widget.h", "dup.h", "missing.h", "./Main.qml", ""]:
        assert regex_backend.resolve_local_import(target, PATHS, from_path=None) == _old_resolve_local_import(target, PATHS), target


STEP_FILES = {
    "src/steps/stepbase.h": "class StepBase {};\n",
    "tests/header-mocks/stepbase.h": "class StepBase {};\n",
    "tests/header-mocks/steps/stepa.h": '#include "stepbase.h"\nclass StepA : public StepBase {};\n',
    "src/steps/stepa.h": '#include "stepbase.h"\nclass StepA : public StepBase {};\n',
    "src/app/x.cpp": '#include "steps/stepbase.h"\n#include <stepbase.h>\nint main() { return 0; }\n',
}


def _import_targets(extract, path):
    _, edges = extract(path, STEP_FILES[path], set(STEP_FILES))
    return [(e.target, e.metadata["lineno"]) for e in edges if e.relation == "imports"]


def _extractors():
    from cortex.structural import treesitter_backend

    return [regex_backend.extract_regex_edges, treesitter_backend.extract_treesitter_edges]


def test_both_backends_resolve_quoted_includes_relative_to_the_includer():
    for extract in _extractors():
        assert _import_targets(extract, "tests/header-mocks/steps/stepa.h") == [("file:tests/header-mocks/stepbase.h", 1)]
        assert _import_targets(extract, "src/steps/stepa.h") == [("file:src/steps/stepbase.h", 1)]
        # Quoted: ancestor rule; angle form keeps the old behaviour (duplicated basename -> module).
        targets = dict(_import_targets(extract, "src/app/x.cpp"))
        assert targets["file:src/steps/stepbase.h"] == 1
        assert targets["module:stepbase.h"] == 2


def test_angle_include_ignores_the_includer_directory():
    files = {"a/util.h": "", "b/util.h": "", "a/main.cpp": "#include <util.h>\n"}
    for extract in _extractors():
        _, edges = extract("a/main.cpp", files["a/main.cpp"], set(files))
        assert [e.target for e in edges if e.relation == "imports"] == ["module:util.h"]


def test_include_rule_marks_only_what_the_old_rules_would_have_left_unresolved():
    rule = regex_backend.resolve_local_import_rule
    # Duplicated basename: only the includer-relative lookup resolves it -> "relative".
    assert rule("stepbase.h", STEP_PATHS, "src/steps/stepa.h") == ("src/steps/stepbase.h", "relative")
    # The old exact-path rule resolves it; the includer-relative lookup finds the same file -> not "relative".
    assert rule("src/steps/stepbase.h", STEP_PATHS, "src/app/x.cpp") == ("src/steps/stepbase.h", "exact")
    # The old unique-basename rule resolves it -> not "relative".
    assert rule("widget.h", PATHS, "src/a/widget.cpp") == ("src/a/widget.h", "basename")
    # No from_path (angle include, QML, other languages): never "relative".
    assert rule("stepbase.h", STEP_PATHS) == (None, "none")
    assert regex_backend.resolve_local_import("stepbase.h", STEP_PATHS) is None


def test_include_rule_metadata_is_written_only_for_a_relative_resolution():
    files = {
        "src/steps/stepbase.h": "",
        "tests/header-mocks/stepbase.h": "",
        "src/ops.cpp": '#include "steps/stepbase.h"\n#include "src/steps/stepbase.h"\n#include "ops.h"\n',
        "src/ops.h": "",
    }
    for extract in _extractors():
        _, edges = extract("src/ops.cpp", files["src/ops.cpp"], set(files))
        rules = {e.target: e.metadata.get("include_rule") for e in edges if e.relation == "imports"}
        # `steps/stepbase.h` (line 1) is relative-only; the exact-path form and the unique basename are not marked.
        marked = [e for e in edges if e.relation == "imports" and e.metadata.get("include_rule")]
        assert [e.metadata["lineno"] for e in marked] == [1]
        assert rules["file:src/ops.h"] is None
        assert all(set(e.metadata) <= {"lineno", "source_file", "include_rule"} for e in edges if e.relation == "imports")
