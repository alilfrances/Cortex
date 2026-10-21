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


@pytest.mark.parametrize(
    ("source", "test", "expected"),
    [
        ("src/foo.cpp", "tests/tst_foo.cpp", True),
        ("src/foo.cpp", "tests/foo_test.cpp", True),
        ("src/foo.cpp", "tests/unit/FooTest.cpp", True),
        ("src/foo.cpp", "tests/unit/footest.cpp", True),
        ("src/foo.cpp", "src/foo_test.cpp", True),
        ("ui/main.cpp", "tests/integration/main.cpp", False),
        ("ui/main.cpp", "tests/ui/main.cpp", True),
        ("app/main.cpp", "tests/main_test.cpp", False),
        ("src/net/socket.cpp", "tests/net/socket.cpp", True),
        ("src/net/socket.cpp", "tests/socket.cpp", True),
        ("src/net/socket.cpp", "tests/db/socket.cpp", False),
        ("src/contest.cpp", "src/contest.cpp", False),
        ("a.py", "tests/test_a.py", True),
        ("utils.py", "tests/test_utils.py", True),  # the C++ generic-stem rule does not touch other languages
    ],
)
def test_pairs_with_test(source: str, test: str, expected: bool) -> None:
    from cortex.testpatterns import pairs_with_test

    assert pairs_with_test(source, test) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/tst_foo.cpp", True),
        ("src/foo_test.cpp", True),
        ("src/FooTests.cpp", True),
        ("src/contest.cpp", False),
        ("src/latest.h", False),
        ("tests/footest.cpp", True),
        ("tests/helpers/fixture.cpp", True),  # still a test directory
    ],
)
def test_cpp_test_files_are_recognised_by_name_or_directory(path: str, expected: bool) -> None:
    assert is_test_path(path) is expected


def test_cpp_test_base_name_is_normalised_symmetrically() -> None:
    assert test_base_name("src/foo.cpp")[0] == test_base_name("tests/tst_foo.cpp")[0] == "foo"
    assert test_base_name("tests/unit/FooTest.cpp") == ("foo", True)
    assert test_base_name("tests/unit/footest.cpp") == ("foo", True)
