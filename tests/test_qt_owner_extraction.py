"""Qt class ownership in C++ headers (0.12 plan, Tasks 2.1 and 2.2).

Qt style puts the class brace on the line after `class X : public QObject`, and
library headers add an all-caps export macro. Neither may fabricate a `QObject`
owner, lose the class, or drop its `qt: qobject` tag.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cortex.structural.regex_backend import extract_regex_edges
from tests.golden_utils import TREE_SITTER_ONLY

HEADER = """#pragma once
#include <QObject>
class {export}Widget : public QObject
{{
    Q_OBJECT
    Q_CLASSINFO("Author", "Jürgen")
    Q_PROPERTY(int value READ value WRITE setValue NOTIFY valueChanged)
public:
    explicit Widget(QObject *parent = nullptr);
    int value() const;
    void setValue(int v);
    Q_INVOKABLE void refresh();
signals:
    void valueChanged();
private:
    int m_value = 0;
}};

struct Gadget
{{
    Q_GADGET
    Q_PROPERTY(int x MEMBER m_x)
    int m_x;
}};

Q_PROPERTY(int stray READ stray)
"""
STYLES = {"plain": "", "export": "WIDGETS_EXPORT "}
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "oss" / "cpp_qt_sortfilterproxymodel"


def _regex(content: str):
    return extract_regex_edges("src/widget.h", content, set())


def _treesitter(content: str):
    from cortex.structural.treesitter_backend import extract_treesitter_edges

    return extract_treesitter_edges("src/widget.h", content, set())


BACKENDS = [
    pytest.param(_regex, id="regex"),
    pytest.param(_treesitter, id="treesitter", marks=TREE_SITTER_ONLY),
]


def _fabricated(nodes) -> list[str]:
    return [node.node_id for node in nodes if node.label == "QObject" or ":QObject." in node.node_id]


@pytest.mark.parametrize("extract", BACKENDS)
@pytest.mark.parametrize("style", sorted(STYLES))
def test_brace_on_next_line_owns_members_without_a_fake_qobject(extract, style):
    nodes, edges = extract(HEADER.format(export=STYLES[style]))
    by_id = {node.node_id: node for node in nodes}

    assert _fabricated(nodes) == []
    assert "symbol:src/widget.h:Widget" in by_id
    assert by_id["symbol:src/widget.h:Widget"].metadata.get("qt") == "qobject"
    assert by_id["symbol:src/widget.h:Widget.value"].kind == "property"
    assert by_id["symbol:src/widget.h:Widget.refresh"].kind == "func"
    assert by_id["symbol:src/widget.h:Gadget.x"].kind == "property"
    assert not any(node.label == "stray" or node.node_id.endswith(".stray") for node in nodes)
    assert any(
        edge.relation == "inherits" and edge.source == "symbol:src/widget.h:Widget" and edge.target == "name:QObject"
        for edge in edges
    )


@pytest.mark.parametrize("extract", BACKENDS)
def test_no_fabricated_qobject_in_vendored_qt_project(extract):
    headers = sorted(SOURCE_ROOT.rglob("*.h"))
    assert headers
    offenders = {}
    for header in headers:
        rel = str(header.relative_to(SOURCE_ROOT))
        nodes, _ = extract(header.read_text(encoding="utf-8")) if extract is _regex else _treesitter_at(rel, header)
        bad = _fabricated(nodes)
        if bad:
            offenders[rel] = bad
    assert offenders == {}


def _treesitter_at(rel: str, header: Path):
    from cortex.structural.treesitter_backend import extract_treesitter_edges

    return extract_treesitter_edges(rel, header.read_text(encoding="utf-8"), set())


@TREE_SITTER_ONLY
def test_mask_preserves_length_newlines_and_multibyte_offsets():
    from cortex.structural.treesitter_backend import _mask_qt_macros

    source = HEADER.format(export="WIDGETS_EXPORT ").encode("utf-8")
    masked = _mask_qt_macros(source)

    assert len(masked) == len(source)
    assert [i for i, b in enumerate(masked) if b == 10] == [i for i, b in enumerate(source) if b == 10]
    assert b"Q_OBJECT" not in masked and b"Q_PROPERTY" not in masked and b"WIDGETS_EXPORT" not in masked
    assert b"Widget : public QObject" in masked
    assert b"signals" not in masked


@TREE_SITTER_ONLY
@pytest.mark.parametrize("style", sorted(STYLES))
def test_masked_header_parses_without_errors(style):
    from cortex.structural import treesitter_backend as tb

    source = HEADER.format(export=STYLES[style]).encode("utf-8")
    parser = tb._parser_for_language(tb._language_for_suffix(".h"))
    root = parser.parse(tb._mask_qt_macros(source)).root_node

    assert not [node for node in tb._iter_nodes(root) if node.type == "ERROR"]
    classes = [node for node in tb._iter_nodes(root) if node.type in {"class_specifier", "struct_specifier"}]
    assert [tb._name_for_node(node, source) for node in classes] == ["Widget", "Gadget"]


@TREE_SITTER_ONLY
def test_slots_sections_and_invokables_are_parsed():
    content = (
        "class Foo : public QObject\n{\n    Q_OBJECT\npublic slots:\n    void go();\nsignals:\n"
        "    void done();\npublic:\n    Q_INVOKABLE void refresh();\n};\n"
    )
    nodes, _ = _treesitter(content)
    labels = {node.label for node in nodes}

    assert {"Foo", "go", "done", "refresh"} <= labels
    assert _fabricated(nodes) == []
