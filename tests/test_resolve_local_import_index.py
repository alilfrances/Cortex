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
