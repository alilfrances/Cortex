"""Plan 0.16, Task 5: `member:method_not_found`. The method lives in a base outside the repository.

A call typed to a repository class that neither the class nor its repository bases has a member for is
`metadata.external = "inherited"` when a class on that inheritance path derives from something outside the
repository (`QWidget::update()`, `QObject::deleteLater()`); the target stays `name:`.  When every base is
in the repository nothing is marked.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from cortex.callresolve import _Index
from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from tests.golden_utils import TREE_SITTER_ONLY

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "audit_unresolved_calls.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("cortex_audit_unresolved_calls_t5", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_script = _load_audit()


def _graph(files: dict[str, str]):
    sources = [
        SourceRecord(path=path, content=content, kind=_classify_path(Path(path)), size_bytes=len(content.encode()), modified_at=0.0)
        for path, content in files.items()
    ]
    return build_graph(sources, [])


def _call(edges, callee: str, line: int | None = None):
    found = [e for e in edges if e.relation == "calls" and e.metadata.get("callee") == callee and line in (None, e.metadata["lineno"])]
    assert len(found) == 1, [(e.target, e.metadata) for e in found]
    return found[0]


WIDGET = """\
#pragma once
class W : public QWidget {
public:
    void f();
    void g();
    void h();
    void viaMember();
private:
    W* m_self;
};
class Holder {
public:
    void run(W* w);
private:
    W* m_w;
    void go();
};
"""

WIDGET_CPP = """\
#include "w.h"
void W::f() { update(); }
void W::g() { this->update(); }
void W::h() { m_self->update(); }
void W::viaMember() { qDebug(); }
void Holder::run(W* w) { w->update(); }
void Holder::go() { m_w->update(); m_w->show(); }
"""


@TREE_SITTER_ONLY
def test_a_method_inherited_from_an_outside_base_is_marked_external_from_every_call_shape() -> None:
    nodes, edges = _graph({"w.h": WIDGET, "w.cpp": WIDGET_CPP})

    sites = {
        "implicit this": _call(edges, "update", 2),
        "this->": _call(edges, "update", 3),
        "member of the class's own type": _call(edges, "update", 4),
        "typed parameter": _call(edges, "update", 6),
        "typed member of another class": _call(edges, "update", 7),
        "second inherited method": _call(edges, "show", 7),
    }
    for name, edge in sites.items():
        assert edge.target == f"name:{edge.metadata['callee']}", name
        assert edge.metadata.get("external") == "inherited", (name, edge.metadata)
        assert "ambiguous" not in edge.metadata and "resolution" not in edge.metadata, name
    # a more specific rule still wins
    assert _call(edges, "qDebug").metadata.get("external") == "qt_macro"


@TREE_SITTER_ONLY
def test_the_outside_base_may_be_further_up_the_repository_hierarchy() -> None:
    files = {
        "a.h": "class A : public QObject { public: void own(); };\nclass B : public A { public: void f(); };\nclass C : public B { public: void g(); };\n",
        "a.cpp": '#include "a.h"\nvoid A::own() {}\nvoid B::f() { deleteLater(); own(); }\nvoid C::g() { deleteLater(); this->own(); }\nvoid use(C* c) { c->deleteLater(); c->own(); }\n',
    }

    _, edges = _graph(files)

    for line in (3, 4, 5):
        assert _call(edges, "deleteLater", line).metadata.get("external") == "inherited"
    for line, target in ((3, "symbol:a.cpp:own"), (4, "symbol:a.cpp:own"), (5, "symbol:a.cpp:own")):
        assert _call(edges, "own", line).target == target  # found in the repository: resolved, never marked


@TREE_SITTER_ONLY
def test_a_class_whose_bases_are_all_in_the_repository_is_not_marked() -> None:
    files = {
        "b.h": "class Base { public: void here(); };\nclass D : public Base { public: void f(); };\nclass Plain { public: void g(); };\n",
        "b.cpp": '#include "b.h"\nvoid Base::here() {}\nvoid D::f() { missing(); this->absent(); }\nvoid Plain::g() { gone(); }\nvoid use(D* d, Plain* p) { d->nothing(); p->nope(); }\n',
    }

    nodes, edges = _graph(files)

    for callee in ("missing", "absent", "gone", "nothing", "nope"):
        edge = _call(edges, callee)
        assert edge.target == f"name:{callee}" and "external" not in edge.metadata, (callee, edge.metadata)
    typed = _call(edges, "nothing")
    assert audit_script.classify(typed, _Index(nodes, [e for e in edges if e.relation in ("imports", "inherits")])) == "member_not_found"


@TREE_SITTER_ONLY
def test_several_candidate_classes_are_marked_only_when_each_has_an_outside_base() -> None:
    files = {
        "x/k.h": "class K : public QObject { public: void f(); };\n",
        "y/k.h": "class K { public: void g(); };\n",
        "use.cpp": "void use(K* k) { k->deleteLater(); }\n",
    }

    _, edges = _graph(files)

    edge = _call(edges, "deleteLater")
    assert edge.target == "name:deleteLater" and "external" not in edge.metadata


@TREE_SITTER_ONLY
def test_audit_files_the_inherited_calls_under_external_and_method_not_found_drops_to_zero() -> None:
    files = {
        "m.h": "class Base { public: void here(); };\nclass V : public QWidget { public: void run(); private: Base* m_b; V* m_v; };\n",
        "m.cpp": '#include "m.h"\nvoid Base::here() {}\nvoid V::run() { m_v->update(); m_b->here(); update(); m_b->nothing(); }\n',
    }

    nodes, edges = _graph(files)
    index = _Index(nodes, [e for e in edges if e.relation in ("imports", "inherits")])
    buckets = sorted(
        (e.metadata["callee"], audit_script.classify(e, index)) for e in edges if e.relation == "calls" and e.target.startswith("name:")
    )

    # `m_b->nothing()` stays: Base derives from nothing outside the repository
    assert buckets == [("nothing", "member:method_not_found"), ("update", "external"), ("update", "external")]


@TREE_SITTER_ONLY
def test_cortex_status_counts_the_inherited_calls_as_out_of_repo(tmp_path) -> None:
    from cortex.store import CortexStore, default_db_path
    from tests.bundle_shapes import make_repo

    repo = make_repo(
        tmp_path / "repo",
        {
            "m.h": "class Base { public: void here(); };\nclass V : public QWidget { public: void run(); private: Base* m_b; };\n",
            "m.cpp": '#include "m.h"\nvoid Base::here() {}\nvoid V::run() { m_b->here(); update(); }\n',
        },
    )

    calls = CortexStore(default_db_path(repo)).resolution_counts(repo)["calls"]

    assert (calls["symbol"], calls["name"], calls["name_external"]) == (1, 1, 1)
    assert calls["resolvable_share"] == calls["in_repo_symbol_share"] == 1.0
