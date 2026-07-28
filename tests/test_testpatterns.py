from __future__ import annotations

import pytest

from cortex.testpatterns import is_test_path, test_base_name


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/test_a.py", True),
        ("pkg/foo_test.go", True),
        ("LingleTests/FooTests.swift", True),
        ("src/FooTest.java", True),
        ("spec/foo_spec.rb", True),
        ("src/foo.test.ts", True),
        ("src/Foo.swift", False),
        ("src/Contest.swift", False),
        ("src/FooTEST.swift", False),
        ("src/Contest.java", False),
        ("src/Testament.java", False),
        ("src/FooTEST.kt", False),
        ("src/Testament.kt", False),
        ("contest.py", False),
        ("protester.py", False),
    ],
)
def test_is_test_path_recognizes_language_conventions(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


def test_test_base_name_normalizes_source_and_test_names_symmetrically() -> None:
    source_base, source_is_test = test_base_name("src/Foo.swift")
    test_base, test_is_test = test_base_name("LingleTests/FooTests.swift")

    assert source_base == test_base == "foo"
    assert source_is_test is False
    assert test_is_test is True
