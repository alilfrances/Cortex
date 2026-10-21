"""B.5 (0.12.2): a local initialised by a C++ cast is typed by the cast's template argument."""
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


@pytest.mark.xfail(strict=True, reason="B.5: receiver_type of a qobject_cast local is the cast function")
def test_a_qobject_cast_local_resolves_to_a_symbol(tmp_path):
    """Hard gate."""
    repo = make_repo(tmp_path / "repo", FILES)
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)

    call = next(e for e in edges if e.relation == "calls" and e.metadata.get("callee") == "refresh")

    assert call.metadata["receiver_type"] == "Widget"
    assert call.target == "symbol:src/widget.cpp:refresh"
