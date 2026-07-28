from __future__ import annotations

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
    ".rb": ((r"(?:_spec|_test)+$", ""),),
    ".js": ((r"(?:\.test|\.spec)+$", ""),),
    ".jsx": ((r"(?:\.test|\.spec)+$", ""),),
    ".ts": ((r"(?:\.test|\.spec)+$", ""),),
    ".tsx": ((r"(?:\.test|\.spec)+$", ""),),
}
_CASE_SENSITIVE_SUFFIXES = {".swift", ".java", ".kt"}


def _path_parts(path: str) -> tuple[tuple[str, ...], str, str]:
    pure = PurePosixPath(path.replace("\\", "/"))
    return pure.parts, pure.stem, pure.suffix.lower()


def _is_test_directory(parts: tuple[str, ...]) -> bool:
    return any(part.lower() in _TEST_DIRECTORIES or part.lower().endswith("tests") for part in parts[:-1])


def test_base_name(path: str) -> tuple[str, bool]:
    """Return a comparison-normalized filename base and test-file status."""
    parts, stem, suffix = _path_parts(path)
    normalized = stem.lower()
    patterns = _SUFFIX_PATTERNS.get(suffix, ())
    stripped = stem if suffix in _CASE_SENSITIVE_SUFFIXES else normalized
    matched = False
    for pattern, replacement in patterns:
        candidate = re.sub(pattern, replacement, stripped)
        if candidate != stripped:
            stripped = candidate
            matched = True
    return stripped.lower(), _is_test_directory(parts) or matched


def is_test_path(path: str) -> bool:
    return test_base_name(path)[1]


# This public helper intentionally starts with ``test_``; prevent pytest from
# mistaking an imported helper for a test function.
test_base_name.__test__ = False

__all__ = ["is_test_path", "test_base_name"]
