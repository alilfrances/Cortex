from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from cortex import risk

PATHS = {
    "src/a.h", "src/a.cpp", "src/b.hpp", "src/b.cc", "src/B.HPP", "lib/a.hh", "lib/a.c",
    "x/dup.h", "y/dup.cpp", "z/dup.cxx", "py/a.py", "README", ".h", "src/a.tar.h", "src/a.tar.cpp",
}


def _old_pair_for(path: str, paths: Iterable[str]) -> list[str]:
    suffix = Path(path).suffix.lower()
    if suffix not in risk._CPP_HEADER_SUFFIXES | risk._CPP_IMPL_SUFFIXES:
        return []
    stem = Path(path).stem
    allowed = risk._CPP_IMPL_SUFFIXES if suffix in risk._CPP_HEADER_SUFFIXES else risk._CPP_HEADER_SUFFIXES
    return sorted(
        candidate for candidate in paths
        if candidate != path and Path(candidate).stem == stem and Path(candidate).suffix.lower() in allowed
    )


def test_pair_for_matches_the_linear_scan_for_every_path():
    for path in sorted(PATHS) + ["new/a.h", "src/missing.cpp"]:
        assert risk._pair_for(path, PATHS) == _old_pair_for(path, PATHS), path


def test_pair_for_accepts_non_set_iterables():
    assert risk._pair_for("src/a.h", sorted(PATHS)) == _old_pair_for("src/a.h", PATHS)


def test_pair_index_is_not_reused_for_a_different_set():
    assert risk._pair_for("a/x.h", {"a/x.h", "a/x.cpp"}) == ["a/x.cpp"]
    assert risk._pair_for("a/x.h", {"a/x.h", "b/x.cc"}) == ["b/x.cc"]


def test_pairing_all_paths_scales_linearly():
    paths = {f"d{i % 40}/m{i}.{ext}" for i in range(6000) for ext in ("hpp", "cpp")}
    started = time.perf_counter()
    for path in paths:
        risk._pair_for(path, paths)
    assert time.perf_counter() - started < 2.0
