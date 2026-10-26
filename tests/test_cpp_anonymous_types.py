"""§1.2 (0.14.0 field review): an unnamed C/C++ struct/class/union/enum has no body-identifier label.

`_name_for_node` fell back to the first identifier in the body, so `enum { kMax = 10 };` became a class
`kMax` and `typedef struct { QString name; } Rec;` a class `QString`.  A typedef takes the typedef name
(what code actually writes); every other unnamed type emits no node, like an anonymous namespace.  The
regex backend sees none of these shapes, so these tests are tree-sitter-only.
"""
from __future__ import annotations

from cortex.structural.treesitter_backend import extract_treesitter_edges
from tests.golden_utils import TREE_SITTER_ONLY

pytestmark = TREE_SITTER_ONLY

SOURCE = """\
enum { kMax = 10 };
union { int i; float f; } u;
typedef struct { QString name; } Rec;
struct { bool operator()(int a) const { return a; } } lessThan;
struct Outer { struct { int x; } inner; };
typedef struct Tag { int y; } TagT;
"""


def _nodes(path: str, content: str):
    nodes, _edges = extract_treesitter_edges(path, content, set())
    return [n for n in nodes if n.granularity == "symbol"]


def test_unnamed_types_emit_no_node_and_a_typedef_takes_its_typedef_name():
    nodes = _nodes("src/types.h", SOURCE)

    assert {n.label for n in nodes if n.kind == "class"} == {"Rec", "Outer", "Tag"}
    labels = {n.label for n in nodes if n.kind == "class"}
    assert not labels & {"kMax", "i", "QString", "a", "x"}, labels


def test_a_function_inside_an_anonymous_struct_never_becomes_a_class():
    nodes = _nodes("src/types.h", SOURCE)

    # The call operator is the one `func` node; `_name_for_node` labels it after its first parameter (`a`),
    # which is a separate naming limit of `operator()` definitions, not of anonymous types.
    assert len([n for n in nodes if n.kind == "func"]) <= 1
    assert not [n for n in nodes if n.kind == "class" and n.label in {"a", "operator()", "lessThan"}]


def test_typedef_with_several_declarators_and_c_files():
    content = "typedef struct { int v; } A, *PA;\ntypedef union { int i; } U;\ntypedef enum { kOne } E;\n"
    for path in ("src/many.h", "src/many.c", "src/many.cpp"):
        nodes = _nodes(path, content)
        assert {n.label for n in nodes if n.kind == "class"} == {"A", "U", "E"}, path


def test_named_types_are_unchanged():
    content = "struct Tag { int y; };\nclass K { int z; };\nenum Color { kRed };\nunion Bits { int i; };\n"
    nodes = _nodes("src/named.h", content)

    assert {n.label for n in nodes if n.kind == "class"} == {"Tag", "K", "Color", "Bits"}
