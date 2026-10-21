"""B.5 (0.12.2): receivers typed by a C++ cast, and by a `Base*` whose label is shared with mocks."""
from __future__ import annotations

import pytest

from cortex.store import CortexStore, default_db_path
from tests.bundle_shapes import make_repo

FILES = {
    "src/widget.h": "class Widget : public QObject {\npublic:\n    void refresh();\n};\n",
    "src/widget.cpp": '#include "widget.h"\nvoid Widget::refresh() { int x = 1; }\n',
    "src/mgr.cpp": (
        '#include "widget.h"\n'
        "void Mgr::run(QObject* o) {\n"
        "    auto* a = qobject_cast<Widget*>(o);\n"
        "    a->refresh();\n"
        "}\n"
    ),
}


def _calls(repo, source_file):
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    return {
        (e.metadata["receiver"], e.metadata["callee"]): e
        for e in edges
        if e.relation == "calls" and e.metadata.get("source_file") == source_file
    }


def test_a_qobject_cast_local_resolves_to_a_symbol(tmp_path):
    """Hard gate."""
    repo = make_repo(tmp_path / "repo", FILES)
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)

    call = next(e for e in edges if e.relation == "calls" and e.metadata.get("callee") == "refresh")

    assert call.metadata["receiver_type"] == "Widget"
    assert call.target == "symbol:src/widget.cpp:refresh"


@pytest.mark.parametrize(
    "init",
    [
        "qobject_cast<Widget*>(o)",
        "dynamic_cast<Widget*>(o)",
        "static_cast<Widget*>(o)",
        "reinterpret_cast<ns::Widget*>(o)",
        "dynamic_pointer_cast<Widget>(sp)",
        "qSharedPointerCast<Widget>(sp)",
    ],
)
def test_every_cast_form_types_the_auto_local(tmp_path, init):
    files = {**FILES, "src/mgr.cpp": f'#include "widget.h"\nvoid Mgr::run(QObject* o) {{\n    auto c = {init};\n    c->refresh();\n}}\n'}
    repo = make_repo(tmp_path / "repo", files)

    call = _calls(repo, "src/mgr.cpp")[("c", "refresh")]

    assert call.target == "symbol:src/widget.cpp:refresh", call.metadata


def test_a_cast_used_directly_as_the_receiver_is_typed_too(tmp_path):
    files = {
        **FILES,
        "src/mgr.cpp": '#include "widget.h"\nvoid Mgr::run(QObject* o) {\n    qobject_cast<Widget*>(o)->refresh();\n}\n',
    }
    repo = make_repo(tmp_path / "repo", files)

    (call,) = _calls(repo, "src/mgr.cpp").values()

    assert call.metadata["receiver_type"] == "Widget" and call.target == "symbol:src/widget.cpp:refresh"


def test_a_template_construction_is_still_typed_by_the_template_name(tmp_path):
    files = {
        **FILES,
        "src/mgr.cpp": '#include "widget.h"\nvoid Mgr::run() {\n    auto c = Widget<int>(1);\n    c.refresh();\n}\n',
    }
    repo = make_repo(tmp_path / "repo", files)

    call = _calls(repo, "src/mgr.cpp")[("c", "refresh")]

    assert call.metadata["receiver_type"] == "Widget"


MOCKED = {
    "src/base.h": "class Base {\npublic:\n    virtual int ready();\n};\n",
    "src/base.cpp": '#include "base.h"\nint Base::ready() { return 1; }\n',
    "tests/mocks/base.h": "class Base {\npublic:\n    virtual int ready() { return 0; }\n};\n",
    "src/mgr.h": 'class Base;\nclass Mgr {\npublic:\n    void check(Base* given);\n    void again();\nprivate:\n    Base* m_base = nullptr;\n};\n',
    "src/mgr.cpp": (
        '#include "mgr.h"\n#include "base.h"\n'
        "void Mgr::check(Base* given) {\n    given->ready();\n}\n"
        "void Mgr::again() {\n    m_base->ready();\n}\n"
    ),
}


def test_a_base_pointer_parameter_or_member_resolves_to_the_real_base_among_mocks(tmp_path):
    repo = make_repo(tmp_path / "repo", MOCKED)

    calls = _calls(repo, "src/mgr.cpp")

    assert calls[("given", "ready")].target == "symbol:src/base.cpp:ready"
    assert calls[("m_base", "ready")].target == "symbol:src/base.cpp:ready"
