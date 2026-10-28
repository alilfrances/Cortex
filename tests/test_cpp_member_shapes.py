"""Plan 0.16, Task 7: `member:not_declared`. The declaration shapes of a member the extractor must record.

One hand-written fixture per shape: `Foo` declares the member (class body in `foo.h`, the method out of line in
`foo.cpp`), `Bar` is the member's class, and the call `m_a->bar()` must resolve through `member_types`.
Shapes 1-6 already worked on 0.15.2 and are pinned here; shape 7 (Qt pimpl, `Q_D(Foo)`) needed local-type
recording in `call_sites.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from tests.golden_utils import TREE_SITTER_ONLY

BAR_H = "#pragma once\nclass Bar {\npublic:\n    void bar() {}\n    class Inner {\n    public:\n        void inner() {}\n    };\n};\n"


def _graph(files: dict[str, str]):
    sources = [
        SourceRecord(path=path, content=content, kind=_classify_path(Path(path)), size_bytes=len(content.encode()), modified_at=0.0)
        for path, content in files.items()
    ]
    return build_graph(sources, [])


def _foo(decl: str, call: str) -> dict[str, str]:
    return {
        "bar.h": BAR_H,
        "foo.h": f'#pragma once\n#include "bar.h"\nclass Foo {{\npublic:\n    void run();\nprivate:\n    {decl}\n}};\n',
        "foo.cpp": f'#include "foo.h"\nvoid Foo::run() {{ {call} }}\n',
    }


def _calls(edges, path: str, callee: str):
    return [e for e in edges if e.relation == "calls" and e.metadata["source_file"] == path and e.metadata["callee"] == callee]


def _member_types(nodes, label: str = "Foo") -> dict[str, str]:
    found = [n for n in nodes if n.granularity == "symbol" and n.kind == "class" and n.label == label]
    assert len(found) == 1
    return dict(found[0].metadata.get("member_types") or {})


# (id, declaration, member_types of Foo, call, callee)
SHAPES = [
    ("several_declarators", "Bar *m_a, *m_b;", {"m_a": "Bar", "m_b": "Bar"}, "m_a->bar(); m_b->bar();", "bar"),
    ("const_pointee", "const Bar *m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("const_pointer", "Bar *const m_a = nullptr;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("mutable", "mutable Bar m_a;", {"m_a": "Bar"}, "m_a.bar();", "bar"),
    ("static", "static Bar *s_a;", {"s_a": "Bar"}, "s_a->bar();", "bar"),
    ("namespaced", "ns::Bar *m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("nested", "Bar::Inner m_a;", {"m_a": "Inner"}, "m_a.inner();", "inner"),
    ("qpointer", "QPointer<Bar> m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("unique_ptr", "std::unique_ptr<Bar> m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("qscopedpointer", "QScopedPointer<Bar> m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("shared_ptr_const", "std::shared_ptr<const Bar> m_a;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("initialiser_assign", "Bar *m_a = nullptr;", {"m_a": "Bar"}, "m_a->bar();", "bar"),
    ("initialiser_brace", "Bar m_a{};", {"m_a": "Bar"}, "m_a.bar();", "bar"),
    ("initialiser_brace_pointer", "Bar *m_a{nullptr};", {"m_a": "Bar"}, "m_a->bar();", "bar"),
]


@TREE_SITTER_ONLY
@pytest.mark.parametrize(("decl", "types", "call", "callee"), [s[1:] for s in SHAPES], ids=[s[0] for s in SHAPES])
def test_a_declared_member_shape_is_recorded_and_its_call_resolves(decl, types, call, callee) -> None:
    nodes, edges = _graph(_foo(decl, call))

    assert _member_types(nodes) == types
    found = _calls(edges, "foo.cpp", callee)
    assert found and all(e.target == f"symbol:bar.h:{callee}" and e.metadata["resolution"] == "declared_type" for e in found), [
        (e.target, e.metadata) for e in found
    ]


BASE_H = '#pragma once\n#include "bar.h"\nclass Base {\nprotected:\n    Bar *m_a;\n};\n'


@TREE_SITTER_ONLY
@pytest.mark.parametrize(
    "files",
    [
        # a base class that lives in another header
        {
            "base.h": BASE_H,
            "foo.h": '#pragma once\n#include "base.h"\nclass Foo : public Base {\npublic:\n    void run();\n};\n',
        },
        # two levels up
        {
            "base.h": BASE_H,
            "mid.h": '#pragma once\n#include "base.h"\nclass Mid : public Base {};\n',
            "foo.h": '#pragma once\n#include "mid.h"\nclass Foo : public Mid {\npublic:\n    void run();\n};\n',
        },
        # a namespace-qualified base
        {
            "base.h": BASE_H.replace("class Base", "namespace lib {\nclass Base").replace("};\n", "};\n}\n"),
            "foo.h": '#pragma once\n#include "base.h"\nclass Foo : public lib::Base {\npublic:\n    void run();\n};\n',
        },
    ],
    ids=["base_header", "two_levels", "namespaced_base"],
)
def test_a_member_declared_in_a_base_class_of_another_header_is_found(files) -> None:
    files = {**files, "bar.h": BAR_H, "foo.cpp": '#include "foo.h"\nvoid Foo::run() { m_a->bar(); }\n'}

    _, edges = _graph(files)

    (edge,) = _calls(edges, "foo.cpp", "bar")
    assert edge.target == "symbol:bar.h:bar" and edge.metadata["resolution"] == "declared_type"


PIMPL = {
    "bar.h": BAR_H,
    "foo.h": "#pragma once\nclass FooPrivate;\nclass Foo {\npublic:\n    void run();\n    void other();\nprotected:\n    FooPrivate *d_ptr;\n};\n",
    "foo_p.h": "#pragma once\nclass FooPrivate {\npublic:\n    void helper() {}\n};\n",
}


@TREE_SITTER_ONLY
def test_q_d_declares_the_private_class_as_the_type_of_d() -> None:
    files = {**PIMPL, "foo.cpp": '#include "foo.h"\n#include "foo_p.h"\nvoid Foo::run() {\n    Q_D(Foo);\n    d->helper();\n}\n'}

    _, edges = _graph(files)

    (edge,) = _calls(edges, "foo.cpp", "helper")
    assert edge.metadata["receiver_type"] == "FooPrivate"
    assert edge.target == "symbol:foo_p.h:helper" and edge.metadata["resolution"] == "declared_type"


@TREE_SITTER_ONLY
def test_q_d_of_a_const_method_and_a_qualified_class() -> None:
    files = {
        **PIMPL,
        "foo.cpp": '#include "foo.h"\n#include "foo_p.h"\nvoid Foo::run() const {\n    Q_D(const Foo);\n    d->helper();\n}\n'
        "void Foo::other() {\n    Q_D(lib::Foo);\n    d->helper();\n}\n",
    }

    _, edges = _graph(files)

    found = _calls(edges, "foo.cpp", "helper")
    assert [e.metadata["receiver_type"] for e in sorted(found, key=lambda e: e.metadata["lineno"])] == ["FooPrivate", "FooPrivate"]
    assert {e.target for e in found} == {"symbol:foo_p.h:helper"}


@TREE_SITTER_ONLY
def test_d_ptr_through_the_member_still_resolves_and_d_without_q_d_does_not_leak_between_functions() -> None:
    files = {
        **PIMPL,
        "foo.cpp": '#include "foo.h"\n#include "foo_p.h"\nvoid Foo::run() {\n    Q_D(Foo);\n    d->helper();\n}\n'
        "void Foo::other() {\n    d_ptr->helper();\n    d->helper();\n}\n",
    }

    _, edges = _graph(files)

    by_line = {e.metadata["lineno"]: e for e in _calls(edges, "foo.cpp", "helper")}
    assert by_line[5].target == "symbol:foo_p.h:helper"  # run(): Q_D
    assert by_line[8].target == "symbol:foo_p.h:helper"  # other(): d_ptr is Foo's own member
    assert by_line[9].target == "name:helper" and by_line[9].metadata["receiver_type"] is None  # no Q_D in other()


@TREE_SITTER_ONLY
def test_q_d_without_a_private_class_names_no_repository_class() -> None:
    files = {"foo.h": "#pragma once\nclass Foo {\npublic:\n    void run();\n};\n", "foo.cpp": '#include "foo.h"\nvoid Foo::run() {\n    Q_D(Foo);\n    d->helper();\n}\n'}

    _, edges = _graph(files)

    (edge,) = _calls(edges, "foo.cpp", "helper")
    assert edge.target == "name:helper" and edge.metadata["receiver_type"] == "FooPrivate" and "resolution" not in edge.metadata
