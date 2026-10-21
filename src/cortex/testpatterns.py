from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


_TEST_DIRECTORIES = {"tests", "test", "spec", "specs", "__tests__"}
_SUFFIX_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    ".py": ((r"^(?:test_)+", ""), (r"(?:_test)+$", "")),
    ".go": ((r"(?:_test)+$", ""),),
    ".rs": ((r"^(?:test_)+", ""), (r"(?:_test)+$", "")),
    ".swift": ((r"(?:Tests?|Test)+$", ""),),
    ".java": ((r"^Test(?=[A-Z_])", ""), (r"(?:Tests?|Test)+$", "")),
    ".kt": ((r"^Test(?=[A-Z_])", ""), (r"(?:Tests?|Test)+$", "")),
    # `foo_spec.rb`/`foo_test.rb` (RSpec, minitest) and `spec_foo.rb`/`test_foo.rb`
    # (minitest/Rake `TestTask` globs such as rack's `test/**/spec_*.rb`).
    ".rb": ((r"^(?:spec_|test_)+", ""), (r"(?:_spec|_test)+$", "")),
    ".js": ((r"(?:\.test|\.spec)+$", ""),),
    ".jsx": ((r"(?:\.test|\.spec)+$", ""),),
    ".ts": ((r"(?:\.test|\.spec)+$", ""),),
    ".tsx": ((r"(?:\.test|\.spec)+$", ""),),
}
_CASE_SENSITIVE_SUFFIXES = {".swift", ".java", ".kt"}
_CPP_TEST_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
# Stems so generic that sharing one says nothing about which source a test covers
# (`ui/main.cpp` vs every `tests/**/main.cpp`).
GENERIC_STEMS = frozenset({
    "main", "utils", "util", "common", "config", "helpers", "helper", "types",
    "constants", "index", "init", "__init__", "mod", "lib", "app",
})


def _path_parts(path: str) -> tuple[tuple[str, ...], str, str]:
    pure = PurePosixPath(path.replace("\\", "/"))
    return pure.parts, pure.stem, pure.suffix.lower()


def _is_test_directory(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in _TEST_DIRECTORIES or part.lower().endswith("tests") for part in parts[:-1])


@dataclass(frozen=True)
class TestName:
    base: str       # filename base normalised for comparison with the source's
    is_test: bool
    by_name: bool   # the file name itself follows a test naming convention

    __test__ = False


def _cpp_test_name(stem: str, in_test_directory: bool) -> tuple[str, bool]:
    """C/C++: `tst_foo`/`test_foo` (QTest), `foo_test`, `FooTest`/`FooTests`, and, inside a
    test directory only, `footest`. Returns the normalised base and whether a name rule matched."""
    matched = False
    camel = re.sub(r"(?<=[a-z0-9])Tests?$", "", stem)
    if camel != stem:
        matched, stem = True, camel
    lowered = stem.lower()
    for pattern in (r"^(?:tst_|test_)+", r"(?:_tests?)+$"):
        candidate = re.sub(pattern, "", lowered)
        if candidate and candidate != lowered:
            matched, lowered = True, candidate
    if not matched and in_test_directory:
        candidate = re.sub(r"(?<=[a-z0-9])tests?$", "", lowered)
        if candidate and candidate != lowered:
            matched, lowered = True, candidate
    return lowered, matched


def test_name_info(path: str) -> TestName:
    parts, stem, suffix = _path_parts(path)
    in_test_directory = _is_test_directory(parts)
    if suffix in _CPP_TEST_SUFFIXES:
        base, by_name = _cpp_test_name(stem, in_test_directory)
        return TestName(base, in_test_directory or by_name, by_name)
    normalized = stem.lower()
    patterns = _SUFFIX_PATTERNS.get(suffix, ())
    stripped = stem if suffix in _CASE_SENSITIVE_SUFFIXES else normalized
    matched = False
    for pattern, replacement in patterns:
        candidate = re.sub(pattern, replacement, stripped)
        if candidate != stripped:
            stripped = candidate
            matched = True
    return TestName(stripped.lower(), in_test_directory or matched, matched)


def test_base_name(path: str) -> tuple[str, bool]:
    """Return a comparison-normalized filename base and test-file status."""
    info = test_name_info(path)
    return info.base, info.is_test


def pairs_with_test(source: str, test: str) -> bool:
    """Whether `test` is the test file for `source` (not the other way round).

    The normalised names must match. A test named for its source (`tst_foo.cpp`,
    `foo_test.cpp`, `FooTest.cpp`) pairs unless the stem is generic; a test that is only
    in a test directory must also mirror the source's directory (`src/net/x.cpp` ~
    `tests/net/x.cpp`), or sit directly in the test root for a non-generic stem. Other
    languages pair on the normalised name alone, as before.
    """
    src, tst = test_name_info(source), test_name_info(test)
    if src.is_test or not tst.is_test or src.base != tst.base:
        return False
    if PurePosixPath(test).suffix.lower() not in _CPP_TEST_SUFFIXES:
        return True  # the stricter rules below are C/C++ only; other languages are unchanged
    generic = src.base in GENERIC_STEMS
    if tst.by_name and not generic:
        return True
    test_dirs = [part.lower() for part in PurePosixPath(test.replace("\\", "/")).parts[:-1] if not _is_test_directory((part, "x"))]
    source_parts = PurePosixPath(source.replace("\\", "/")).parts
    source_dir = source_parts[-2].lower() if len(source_parts) >= 2 else ""
    if source_dir and source_dir in test_dirs:
        return True
    return not test_dirs and not generic


def is_test_path(path: str) -> bool:
    return test_base_name(path)[1]


# This public helper intentionally starts with ``test_``; prevent pytest from
# mistaking an imported helper for a test function.
test_base_name.__test__ = False
test_name_info.__test__ = False
pairs_with_test.__test__ = False

__all__ = ["GENERIC_STEMS", "TestName", "is_test_path", "pairs_with_test", "test_base_name", "test_name_info"]
