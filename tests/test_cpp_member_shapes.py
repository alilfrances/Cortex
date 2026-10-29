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


# --- Plan 0.16, Task 7b: shapes the Task 7 implementation saw and did not change ---------------------------------

# A Qt declaration macro written without its semicolon (Qt code usually omits it).  The macro is blanked for the
# tree-sitter parse only; what follows it must still be a member of the class.
_MACROS = [
    "Q_DECLARE_PRIVATE(Foo)",
    "Q_DECLARE_PUBLIC(Foo)",
    "Q_DECLARE_PRIVATE_D(d_ptr, Foo)",
    "Q_DECLARE_PUBLIC_D(Foo)",
    "Q_DECLARE_TR_FUNCTIONS(Foo)",
    "Q_DISABLE_COPY(Foo)",
    "Q_DISABLE_COPY_MOVE(Foo)",
    "Q_GADGET",
    "Q_ENUM(E)",
    "Q_FLAG(F)",
    "Q_INTERFACES(QObject)",
    'Q_PLUGIN_METADATA(IID "org.example.Foo" FILE "foo.json")',
    "Q_PROPERTY(int x READ x)",
    'Q_CLASSINFO("author", "me")',
]
# (id, class body between the macro and the member, the member_types of Foo)
_AFTER_MACRO = [
    ("field", "{m}\n    Bar *m_a;\n", {"m_a": "Bar"}),
    ("access_specifier", "{m}\npublic:\n    Bar *m_a;\n", {"m_a": "Bar"}),
    ("protected_section", "{m}\nprotected:\n    Bar *m_a;\n", {"m_a": "Bar"}),
    ("two_macros", "{m}\n    {m}\n    Bar *m_a;\n", {"m_a": "Bar"}),
    ("same_line", "{m} Bar *m_a;\n", {"m_a": "Bar"}),
]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("macro", _MACROS)
@pytest.mark.parametrize(("body", "types"), [s[1:] for s in _AFTER_MACRO], ids=[s[0] for s in _AFTER_MACRO])
def test_a_qt_declaration_macro_without_a_semicolon_does_not_hide_the_next_member(macro, body, types) -> None:
    files = {
        "bar.h": BAR_H,
        "foo.h": '#pragma once\n#include "bar.h"\nclass Foo {\npublic:\n    void run();\nprivate:\n    '
        + body.replace("{m}", macro)
        + "};\n",
        "foo.cpp": '#include "foo.h"\nvoid Foo::run() { m_a->bar(); }\n',
    }

    nodes, edges = _graph(files)

    assert _member_types(nodes) == types
    (edge,) = _calls(edges, "foo.cpp", "bar")
    assert edge.target == "symbol:bar.h:bar" and edge.metadata["resolution"] == "declared_type"


@TREE_SITTER_ONLY
def test_a_qt_declaration_macro_without_a_semicolon_does_not_hide_the_next_method() -> None:
    files = {
        "bar.h": BAR_H,
        "foo.h": '#pragma once\n#include "bar.h"\nclass Foo {\n    Q_DECLARE_PRIVATE(Foo)\npublic:\n    void run();\n'
        "    void other();\nprivate:\n    Bar *m_a;\n};\n",
        "foo.cpp": '#include "foo.h"\nvoid Foo::run() { other(); }\nvoid Foo::other() { m_a->bar(); }\n',
    }

    nodes, edges = _graph(files)

    assert _member_types(nodes) == {"m_a": "Bar"}
    (other,) = _calls(edges, "foo.cpp", "other")
    assert other.target.startswith("symbol:") and other.target.endswith(":other"), other.target
    (bar,) = _calls(edges, "foo.cpp", "bar")
    assert bar.target == "symbol:bar.h:bar"


def test_the_mask_blanks_the_declaration_macros_and_keeps_offsets_and_lines() -> None:
    from cortex.structural.treesitter_backend import _mask_qt_macros

    source = b'class Foo {\n    Q_DECLARE_PRIVATE(Foo)\n    Q_PLUGIN_METADATA(IID "x" FILE "y.json")\npublic:\n    int a;\n};\n'

    masked = _mask_qt_macros(source)

    assert len(masked) == len(source) and masked.count(b"\n") == source.count(b"\n")
    assert b"Q_DECLARE_PRIVATE" not in masked and b"Q_PLUGIN_METADATA" not in masked and b"public:" in masked
    assert masked.index(b"public:") == source.index(b"public:")


@TREE_SITTER_ONLY
def test_masked_macros_stay_visible_to_the_qt_extraction() -> None:
    """`Q_PROPERTY` is masked for the parser, yet the Qt-specific extraction still reads the original text."""
    files = {
        "foo.h": "#pragma once\nclass Foo : public QObject {\n    Q_OBJECT\n    Q_PROPERTY(int size READ size WRITE setSize NOTIFY sizeChanged)\n"
        "    Q_DECLARE_PRIVATE(Foo)\npublic:\n    int size() const;\n    void setSize(int s);\nsignals:\n    void sizeChanged();\n};\n",
    }

    nodes, _ = _graph(files)

    props = [n for n in nodes if n.granularity == "symbol" and n.metadata.get("qt_role") == "property"] or [
        n for n in nodes if n.granularity == "symbol" and n.label == "size"
    ]
    assert props, [(n.label, n.kind, n.metadata) for n in nodes if n.granularity == "symbol"]


# Members declared inside `#ifdef` / `#if` branches of a class body are members of the class, every branch.
_PREPROC = [
    ("ifdef", "#ifdef X\n    Bar *m_a;\n#endif\n", {"m_a": "Bar"}),
    ("ifdef_else", "#ifdef X\n    Bar *m_a;\n#else\n    Bar *m_b;\n#endif\n", {"m_a": "Bar", "m_b": "Bar"}),
    ("if_elif_else", "#if A\n    Bar *m_a;\n#elif B\n    Bar *m_b;\n#else\n    Bar *m_c;\n#endif\n", {"m_a": "Bar", "m_b": "Bar", "m_c": "Bar"}),
    ("ifndef_nested", "#ifndef X\n#if Y\n    Bar *m_a;\n#endif\n    Bar *m_b;\n#endif\n", {"m_a": "Bar", "m_b": "Bar"}),
    ("around_other_members", "    Bar *m_a;\n#ifdef X\n    Bar *m_b;\n#endif\n    Bar *m_c;\n", {"m_a": "Bar", "m_b": "Bar", "m_c": "Bar"}),
]


@TREE_SITTER_ONLY
@pytest.mark.parametrize(("decl", "types"), [s[1:] for s in _PREPROC], ids=[s[0] for s in _PREPROC])
def test_members_declared_in_preprocessor_branches_of_a_class_body_are_recorded(decl, types) -> None:
    call = " ".join(f"{name}->bar();" for name in types)
    files = {
        "bar.h": BAR_H,
        "foo.h": '#pragma once\n#include "bar.h"\nclass Foo {\npublic:\n    void run();\nprivate:\n' + decl + "};\n",
        "foo.cpp": f'#include "foo.h"\nvoid Foo::run() {{ {call} }}\n',
    }

    nodes, edges = _graph(files)

    assert _member_types(nodes) == types
    found = _calls(edges, "foo.cpp", "bar")
    assert len(found) == len(types) and all(e.target == "symbol:bar.h:bar" for e in found), [(e.target, e.metadata) for e in found]


@TREE_SITTER_ONLY
def test_a_member_in_a_branch_of_a_base_class_is_found_too() -> None:
    files = {
        "bar.h": BAR_H,
        "base.h": '#pragma once\n#include "bar.h"\nclass Base {\nprotected:\n#ifdef X\n    Bar *m_a;\n#else\n    Bar *m_b;\n#endif\n};\n',
        "foo.h": '#pragma once\n#include "base.h"\nclass Foo : public Base {\npublic:\n    void run();\n};\n',
        "foo.cpp": '#include "foo.h"\nvoid Foo::run() { m_a->bar(); m_b->bar(); }\n',
    }

    _, edges = _graph(files)

    found = _calls(edges, "foo.cpp", "bar")
    assert len(found) == 2 and {e.target for e in found} == {"symbol:bar.h:bar"}


# `Q_Q(Foo)` in a private class declares `Foo *const q = q_func();`
Q_FILES = {
    "foo.h": "#pragma once\nclass Foo {\npublic:\n    void run();\n    void bar() {}\n};\n",
    "foo_p.h": '#pragma once\n#include "foo.h"\nclass FooPrivate {\npublic:\n    void helper();\n    void again() const;\n};\n',
}


@TREE_SITTER_ONLY
def test_q_q_declares_the_public_class_as_the_type_of_q() -> None:
    files = {**Q_FILES, "foo.cpp": '#include "foo_p.h"\nvoid FooPrivate::helper() {\n    Q_Q(Foo);\n    q->bar();\n}\n'}

    _, edges = _graph(files)

    (edge,) = _calls(edges, "foo.cpp", "bar")
    assert edge.metadata["receiver_type"] == "Foo"
    assert edge.target == "symbol:foo.h:bar" and edge.metadata["resolution"] == "declared_type"


@TREE_SITTER_ONLY
def test_q_q_of_a_const_method_and_a_qualified_class_and_it_does_not_leak() -> None:
    files = {
        **Q_FILES,
        "foo.cpp": '#include "foo_p.h"\nvoid FooPrivate::again() const {\n    Q_Q(const Foo);\n    q->bar();\n}\n'
        "void FooPrivate::helper() {\n    Q_Q(lib::Foo);\n    q->bar();\n}\n"
        "void other() {\n    q->bar();\n}\n",
    }

    _, edges = _graph(files)

    by_line = {e.metadata["lineno"]: e for e in _calls(edges, "foo.cpp", "bar")}
    assert by_line[4].target == "symbol:foo.h:bar" and by_line[4].metadata["receiver_type"] == "Foo"
    assert by_line[8].target == "symbol:foo.h:bar" and by_line[8].metadata["receiver_type"] == "Foo"
    assert by_line[11].target == "name:bar" and by_line[11].metadata["receiver_type"] is None  # no Q_Q in other()
