"""Plan 0.16, Task 4: `member:no_enclosing_class`. The class a call sits in must be found.

Every case is a header (`foo.h`) with a class `Foo` that holds `Helper *m_h`, plus a method `run` that calls
`m_h->ping()`, defined inline or out of line.  The call is typed through the enclosing class's
`member_types`, so it resolves to `Helper::ping` only when the extractor names the class (the class node's
label, and the call's `enclosing_class`) the way the resolver looks it up.  Shapes that already worked on
0.15.2 stay in the table as regression cases.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from cortex.structural.treesitter_backend import _mask_qt_macros
from tests.golden_utils import TREE_SITTER_ONLY

HELPER = "class Helper { public: void ping() {} };\n"
PING = "symbol:foo.h:ping"
MEMBER = "private:\n    Helper* m_h;\n"
RUN_OUT_OF_LINE = "#include \"foo.h\"\nvoid {qual}::run() {{ m_h->ping(); }}\n"


def _header(opening: str, *, before: str = "", after: str = "", inline_run: bool = False) -> str:
    run = "void run() { m_h->ping(); }" if inline_run else "void run();"
    return f"#pragma once\n{HELPER}{before}{opening} {{\npublic:\n    {run}\n{MEMBER}}};\n{after}"


# name -> (files, class label that must exist, qualifier of the out-of-line definition or None when inline)
CASES: dict[str, tuple[dict[str, str], str]] = {
    # --- worked on 0.15.2 -------------------------------------------------------------------------------
    "all_caps_export_macro": ({"foo.h": _header("class Q_DECL_EXPORT Foo : public Helper"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "declspec": ({"foo.h": _header("class __declspec(dllexport) Foo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "nested_class": (
        {
            "foo.h": f"#pragma once\n{HELPER}class Outer {{\npublic:\n    class Foo {{\n    public:\n        void run();\n    {MEMBER}    }};\n}};\n",
            "foo.cpp": RUN_OUT_OF_LINE.format(qual="Outer::Foo"),
        },
        "Foo",
    ),
    "class_template": (
        {"foo.h": _header("template <class T>\nclass Foo"), "foo.cpp": "#include \"foo.h\"\ntemplate <class T> void Foo<T>::run() { m_h->ping(); }\n"},
        "Foo",
    ),
    "namespaced_out_of_line": ({"foo.h": _header("class Foo", before="namespace app {\n", after="}\n"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="app::Foo")}, "Foo"),
    # --- an export macro the parser takes for the class name ----------------------------------------------
    "mixed_case_export_macro": ({"foo.h": _header("class MyLib_Export Foo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "api_suffix_macro": ({"foo.h": _header("class Core_API Foo : public Helper"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "two_macros": ({"foo.h": _header("class Q_DECL_EXPORT Q_DECL_FINAL Foo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "macro_in_template_header": (
        {"foo.h": _header("template <class T> class LIB_EXPORT Foo"), "foo.cpp": "#include \"foo.h\"\ntemplate <class T> void Foo<T>::run() { m_h->ping(); }\n"},
        "Foo",
    ),
    "macro_then_newline": ({"foo.h": _header("class LIB_EXPORT\nFoo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "macro_with_arguments": ({"foo.h": _header("class QT_DEPRECATED_X(\"old\") Foo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "macro_call_naming_a_library": ({"foo.h": _header("class DLL_API(core) Foo"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")}, "Foo"),
    "macro_on_the_namespace_line": ({"foo.h": _header("class Q_DECL_EXPORT Foo", before="namespace app { ", after="}\n"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="app::Foo")}, "Foo"),
    "macro_inline_method": ({"foo.h": _header("class MyLib_Export Foo", inline_run=True)}, "Foo"),
    "qt_begin_namespace": (
        {"foo.h": _header("class Foo", before="QT_BEGIN_NAMESPACE\n", after="QT_END_NAMESPACE\n"), "foo.cpp": RUN_OUT_OF_LINE.format(qual="Foo")},
        "Foo",
    ),
    # --- out-of-line definitions whose declarator has no `declarator` field -------------------------------
    "reference_return": (
        {
            "foo.h": _header("class Foo").replace("void run();", "void run();\n    Foo& self();"),
            "foo.cpp": "#include \"foo.h\"\nFoo& Foo::self() { m_h->ping(); return *this; }\nvoid Foo::run() { }\n",
        },
        "Foo",
    ),
    "const_reference_return": (
        {
            "foo.h": _header("class Foo").replace("void run();", "void run();\n    const Helper& helper() const;"),
            "foo.cpp": "#include \"foo.h\"\nconst Helper& Foo::helper() const { m_h->ping(); return *m_h; }\nvoid Foo::run() { }\n",
        },
        "Foo",
    ),
    "rvalue_reference_return": (
        {
            "foo.h": _header("class Foo").replace("void run();", "void run();\n    Helper&& take();"),
            "foo.cpp": "#include \"foo.h\"\nHelper&& Foo::take() { m_h->ping(); return static_cast<Helper&&>(*m_h); }\nvoid Foo::run() { }\n",
        },
        "Foo",
    ),
    "operator_returning_reference": (
        {
            "foo.h": _header("class Foo").replace("void run();", "void run();\n    Foo& operator=(const Foo&);"),
            "foo.cpp": "#include \"foo.h\"\nFoo& Foo::operator=(const Foo&) { m_h->ping(); return *this; }\nvoid Foo::run() { }\n",
        },
        "Foo",
    ),
    # --- a macro in front of the return type of an out-of-line definition --------------------------------
    "export_macro_before_return_type": (
        {"foo.h": _header("class Foo"), "foo.cpp": "#include \"foo.h\"\nMYLIB_EXPORT void Foo::run() { m_h->ping(); }\n"},
        "Foo",
    ),
}


def _graph(files: dict[str, str]):
    sources = [
        SourceRecord(path=path, content=content, kind=_classify_path(Path(path)), size_bytes=len(content.encode()), modified_at=0.0)
        for path, content in files.items()
    ]
    return build_graph(sources, [])


@TREE_SITTER_ONLY
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_class_is_named_and_the_member_call_in_its_method_resolves(name: str) -> None:
    files, label = CASES[name]

    nodes, edges = _graph(files)

    classes = [n for n in nodes if n.kind == "class" and n.label == label]
    assert [n.node_id for n in classes] == [f"symbol:foo.h:{label}"], [n.node_id for n in nodes if n.kind == "class"]
    pings = [e for e in edges if e.relation == "calls" and e.metadata.get("callee") == "ping"]
    assert len(pings) == 1, [e.metadata for e in pings]
    ping = pings[0]
    assert ping.target == PING and ping.metadata.get("resolution") == "declared_type", (ping.target, ping.metadata)
    assert ping.metadata["enclosing_class"] is not None
    assert ping.metadata["enclosing_class"].split("<")[0].split("::")[-1] == label


@pytest.mark.parametrize(
    "source",
    [
        "class Foo final : public Bar {\n",
        "class Foo final {\n",
        "struct stat st{};\n",
        "struct sockaddr_in addr{};\n",
        "enum class Color : int {\n",
        "template <class T> class Box {\n",
        "template <class T, class U> struct Pair {\n",
        "class Foo : public Bar {\n",
        "friend class Other;\n",
        "struct Point { int x; };\n",
        "x = a ? b : c; class Foo {\n",
        "typedef struct _GOBJ GObj;\n",
    ],
)
def test_masking_leaves_ordinary_class_heads_alone(source: str) -> None:
    assert _mask_qt_macros(source.encode()) == source.encode()


@pytest.mark.parametrize(
    "source, blanked",
    [
        ("class MyLib_Export Foo {\n", "MyLib_Export"),
        ("class Core_API Foo : public Bar {\n", "Core_API"),
        ("class Q_DECL_EXPORT Q_DECL_FINAL Foo {\n", "Q_DECL_EXPORT Q_DECL_FINAL"),
        ("template <class T> class LIB_EXPORT Foo {\n", "LIB_EXPORT"),
        ("namespace a { class LIB_EXPORT Foo {\n", "LIB_EXPORT"),
        ("class LIB_EXPORT\nFoo {\n", "LIB_EXPORT"),
        ('class QT_DEPRECATED_X("old") Foo {\n', 'QT_DEPRECATED_X("old")'),
        ("class DLL_API(core) Foo {\n", "DLL_API(core)"),
        ("QT_BEGIN_NAMESPACE\nclass Foo {\n", "QT_BEGIN_NAMESPACE"),
    ],
)
def test_masking_blanks_export_macros_in_front_of_a_class_name(source: str, blanked: str) -> None:
    out = _mask_qt_macros(source.encode()).decode()

    assert len(out) == len(source)
    assert out.replace(" ", "") == source.replace(blanked, "").replace(" ", "")  # only the macro went
    assert blanked.split("(")[0] not in out and "Foo" in out
