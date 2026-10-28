"""Plan 0.16, Task 8: `chain`. A call on a call's result: ``a->b()->c()``, ``a.b().c()``.

The extractor emits the outer call with `receiver_kind = "chain"` and the receiver text, and the inner call
``a->b()`` as an edge of its own on the same line.  The resolver reads the inner edge's resolved target, takes the
return type from that function's signature, and looks the callee up in that class (and its bases); a receiver
that ends in a field (``h->item->apply()``) is typed by the declared member of the class before it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cortex.callresolve import _return_label, _type_label
from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from tests.golden_utils import TREE_SITTER_ONLY

ITEM_H = (
    "#pragma once\n"
    "class Item {\n"
    "public:\n"
    "    void apply() {}\n"
    "    Item* next() { return this; }\n"
    "    static Item* create() { return nullptr; }\n"
    "};\n"
)
MANAGER_H = (
    '#pragma once\n#include "item.h"\n'
    "class Manager {\n"
    "public:\n"
    "    Item* current();\n"
    "    std::unique_ptr<Item> take();\n"
    "    const Item& ref() const;\n"
    "    Item* operator->();\n"
    "};\n"
)
MANAGER_CPP = (
    '#include "manager.h"\n'
    "Item* Manager::current() { return nullptr; }\n"
    "std::unique_ptr<Item> Manager::take() { return nullptr; }\n"
    "const Item& Manager::ref() const { return *current(); }\n"
)


def _graph(files: dict[str, str]):
    sources = [
        SourceRecord(path=path, content=content, kind=_classify_path(Path(path)), size_bytes=len(content.encode()), modified_at=0.0)
        for path, content in files.items()
    ]
    return build_graph(sources, [])


def _repo(app: str, **extra: str) -> dict[str, str]:
    return {"item.h": ITEM_H, "manager.h": MANAGER_H, "manager.cpp": MANAGER_CPP, "app.cpp": '#include "manager.h"\n' + app, **extra}


def _calls(edges, callee: str, path: str = "app.cpp"):
    return sorted(
        (e for e in edges if e.relation == "calls" and e.metadata["callee"] == callee and e.metadata["source_file"] == path),
        key=lambda e: e.metadata["lineno"],
    )


def _one(edges, callee: str, path: str = "app.cpp"):
    found = _calls(edges, callee, path)
    assert len(found) == 1, [(e.target, e.metadata) for e in found]
    return found[0]


# -- the return type of a signature ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("signature", "name", "label"),
    [
        ("Item* Manager::current() { return nullptr; }", "current", "Item"),
        ("const ns::Item& Manager::ref() { return *(Item*)0; }", "ref", "Item"),
        ("static inline Item* inst() { return nullptr; }", "inst", "Item"),
        ("virtual Item *clone() const override;", "clone", "Item"),
        ("Item *const get() const", "get", "Item"),
        ("Q_INVOKABLE Item* qi();", "qi", "Item"),
        ("MYLIB_EXPORT Item *Manager::make(int a) const {", "make", "Item"),
        ("std::unique_ptr<Item> Manager::take() {", "take", "Item"),
        ("QSharedPointer<const Item> sp()", "sp", "Item"),
        ("Outer<int>::Inner* Box<T>::find()", "find", "Inner"),
        ("QList<Item*> items() const", "items", "QList"),
        ("static Foo::Bar make(){", "make", "Bar"),
        ("void run();", "run", None),
        ("auto f() -> Item*", "f", None),
        ("unsigned int count()", "count", None),
        ("bool ok() const", "ok", None),
        ("Manager::Manager() {", "Manager", None),
        ("Item* operator->()", "operator->", None),
    ],
)
def test_the_return_type_label_of_a_signature(signature, name, label) -> None:
    assert _return_label(signature, name) == label


def test_a_written_type_is_reduced_to_one_class_label() -> None:
    assert _type_label("const Item &") == "Item"
    assert _type_label("std::shared_ptr<ns::Item>") == "Item"
    assert _type_label("QList<Item*>") == "QList"
    assert _type_label("int") is None and _type_label("unsigned long") is None and _type_label("void") is None


# -- resolution -------------------------------------------------------------------------------------------------

@TREE_SITTER_ONLY
def test_a_call_on_the_result_of_a_member_call_resolves_to_the_returned_class() -> None:
    _, edges = _graph(_repo("void use(Manager* mgr) {\n    mgr->current()->apply();\n}\n"))

    inner = _one(edges, "current")
    assert inner.target == "symbol:manager.cpp:current"
    outer = _one(edges, "apply")
    assert outer.target == "symbol:item.h:apply" and outer.metadata["resolution"] == "chain"
    assert "ambiguous" not in outer.metadata and "external" not in outer.metadata


@TREE_SITTER_ONLY
def test_a_dot_chain_and_a_multi_line_chain_resolve() -> None:
    app = "void use(Manager& mgr) {\n    mgr.current()->apply();\n    mgr.current()\n        ->apply();\n}\n"

    _, edges = _graph(_repo(app))

    assert [(e.target, e.metadata["resolution"]) for e in _calls(edges, "apply")] == [("symbol:item.h:apply", "chain")] * 2


@TREE_SITTER_ONLY
def test_a_three_link_chain_resolves_link_by_link() -> None:
    _, edges = _graph(_repo("void use(Manager* mgr) {\n    mgr->current()->next()->apply();\n}\n"))

    nxt = _one(edges, "next")
    assert nxt.target == "symbol:item.h:next" and nxt.metadata["resolution"] == "chain"
    assert _one(edges, "apply").target == "symbol:item.h:apply"


@TREE_SITTER_ONLY
def test_a_chain_longer_than_the_limit_stays_unresolved() -> None:
    _, edges = _graph(_repo("void use(Manager* mgr) {\n    mgr->current()->next()->next()->next()->apply();\n}\n"))

    assert _one(edges, "apply").target == "name:apply"  # five calls
    assert {e.target for e in _calls(edges, "next")} == {"symbol:item.h:next"}  # the first three links resolve


@TREE_SITTER_ONLY
def test_a_free_function_and_a_static_factory_start_a_chain() -> None:
    files = _repo(
        "void use() {\n    makeItem()->apply();\n    Item::create()->apply();\n}\n",
    )
    files["factory.cpp"] = '#include "item.h"\nItem* makeItem() { return nullptr; }\n'
    files["app.cpp"] = '#include "item.h"\nItem* makeItem();\n' + files["app.cpp"].split("\n", 1)[1]

    _, edges = _graph(files)

    assert [e.target for e in _calls(edges, "apply")] == ["symbol:item.h:apply"] * 2


@TREE_SITTER_ONLY
def test_a_smart_pointer_result_is_unwrapped() -> None:
    _, edges = _graph(_repo("void use(Manager* mgr) {\n    mgr->take()->apply();\n    mgr->ref().apply();\n}\n"))

    assert [e.target for e in _calls(edges, "apply")] == ["symbol:item.h:apply"] * 2


@TREE_SITTER_ONLY
def test_a_member_inherited_by_the_returned_class_resolves() -> None:
    files = _repo("void use(Manager* mgr) {\n    mgr->derived()->apply();\n}\n")
    files["manager.h"] = MANAGER_H.replace("Item* current();", "Item* current();\n    struct Child* derived();")
    files["child.h"] = '#pragma once\n#include "item.h"\nstruct Child : public Item {\n    void own() {}\n};\n'
    files["manager.cpp"] = MANAGER_CPP + '#include "child.h"\nChild* Manager::derived() { return nullptr; }\n'

    _, edges = _graph(files)

    assert _one(edges, "apply").target == "symbol:item.h:apply"


@TREE_SITTER_ONLY
def test_a_framework_result_stays_unresolved() -> None:
    files = {
        "widget.h": "#pragma once\nclass Widget {\npublic:\n    QLayout* layout() { return nullptr; }\n};\n",
        "app.cpp": '#include "widget.h"\nvoid use(Widget& widget) {\n    widget.layout()->addWidget(nullptr);\n}\n',
    }

    _, edges = _graph(files)

    assert _one(edges, "layout").target == "symbol:widget.h:layout"
    outer = _one(edges, "addWidget")
    assert outer.target == "name:addWidget" and "resolution" not in outer.metadata and "ambiguous" not in outer.metadata


@TREE_SITTER_ONLY
def test_a_missing_member_of_a_class_with_an_external_base_is_external() -> None:
    files = {
        "w.h": "#pragma once\nclass W : public QWidget {\npublic:\n    W* self() { return this; }\n};\n",
        "app.cpp": '#include "w.h"\nvoid use(W* w) {\n    w->self()->update();\n}\n',
    }

    _, edges = _graph(files)

    outer = _one(edges, "update")
    assert outer.target == "name:update" and outer.metadata["external"] == "inherited"


@TREE_SITTER_ONLY
def test_a_missing_member_of_a_class_with_repository_bases_is_not_external() -> None:
    files = _repo("void use(Manager* mgr) {\n    mgr->current()->missing();\n}\n")

    _, edges = _graph(files)

    outer = _one(edges, "missing")
    assert outer.target == "name:missing" and "external" not in outer.metadata and "resolution" not in outer.metadata


@TREE_SITTER_ONLY
def test_an_ambiguous_inner_call_leaves_the_chain_alone() -> None:
    files = {
        "item.h": ITEM_H,
        "a.cpp": '#include "item.h"\nItem* pick() { return nullptr; }\n',
        "b.cpp": '#include "item.h"\nItem* pick() { return nullptr; }\n',
        "app.cpp": 'void use() {\n    pick()->apply();\n}\n',
    }

    _, edges = _graph(files)

    inner = _one(edges, "pick")
    assert inner.target == "name:pick" and inner.metadata["ambiguous"] is True
    outer = _one(edges, "apply")
    assert outer.target == "name:apply" and "resolution" not in outer.metadata and "ambiguous" not in outer.metadata


@TREE_SITTER_ONLY
def test_two_classes_with_the_returned_label_keep_the_chain_ambiguous() -> None:
    files = {
        "x/item.h": ITEM_H,
        "y/item.h": ITEM_H,
        "z/mk.cpp": "Item* make() { return nullptr; }\n",
        "z/app.cpp": "void use() {\n    make()->apply();\n}\n",
    }

    _, edges = _graph(files)

    outer = _one(edges, "apply", "z/app.cpp")
    assert outer.target == "name:apply" and outer.metadata["ambiguous"] is True
    assert sorted(outer.metadata["candidates"]) == ["symbol:x/item.h:apply", "symbol:y/item.h:apply"]


HOLDER = {
    "item.h": ITEM_H,
    "holder.h": '#pragma once\n#include "item.h"\nclass Holder {\npublic:\n    Item* item;\n    Holder* self() { return this; }\n};\n',
    "foo.h": '#pragma once\n#include "holder.h"\nclass Foo {\npublic:\n    void run();\nprivate:\n    Holder* m_h;\n};\n',
}


@TREE_SITTER_ONLY
def test_a_receiver_that_ends_in_a_declared_field_is_typed_by_the_member() -> None:
    files = {
        **HOLDER,
        "foo.cpp": '#include "foo.h"\nvoid Foo::run() {\n    m_h->item->apply();\n    this->m_h->item->apply();\n    m_h->self()->item->apply();\n}\n',
    }

    _, edges = _graph(files)

    found = _calls(edges, "apply", "foo.cpp")
    assert [(e.target, e.metadata["resolution"]) for e in found] == [("symbol:item.h:apply", "chain")] * 3


@TREE_SITTER_ONLY
def test_a_field_of_an_untyped_base_stays_unresolved() -> None:
    files = {**HOLDER, "foo.cpp": '#include "foo.h"\nvoid Foo::run() {\n    other->item->apply();\n}\n'}

    _, edges = _graph(files)

    assert _one(edges, "apply", "foo.cpp").target == "name:apply"


@TREE_SITTER_ONLY
def test_a_receiver_cut_at_the_length_limit_is_not_followed() -> None:
    long_name = "a" * 90
    _, edges = _graph(_repo(f"void use(Manager* mgr) {{\n    mgr->current()->next()->{long_name}()->apply();\n}}\n"))

    assert _one(edges, "apply").target == "name:apply"


@TREE_SITTER_ONLY
def test_chains_in_other_languages_are_unchanged() -> None:
    files = {"a.js": "class Item { apply() {} }\nclass Mgr { current() { return new Item(); } }\nfunction use(m) { m.current().apply(); }\n"}

    _, edges = _graph(files)

    assert _one(edges, "apply", "a.js").target == "name:apply"


# -- incremental refresh ----------------------------------------------------------------------------------------

@TREE_SITTER_ONLY
def test_a_changed_return_type_re_resolves_a_chain_in_a_file_that_did_not_change(tmp_path) -> None:
    from cortex.ingest import ingest_repository
    from cortex.store import CortexStore
    from tests.test_call_resolution import _commit, _repo as init_repo

    repo = init_repo(tmp_path)
    files = {
        "item.h": ITEM_H,
        "other.h": "#pragma once\nclass Other {\npublic:\n    void apply() {}\n};\n",
        "manager.h": '#pragma once\n#include "item.h"\n#include "other.h"\nclass Manager {\npublic:\n    Item* current();\n};\n',
        "manager.cpp": '#include "manager.h"\nItem* Manager::current() { return nullptr; }\n',
        "app.cpp": '#include "manager.h"\nvoid use(Manager* mgr) {\n    mgr->current()->apply();\n}\n',
    }
    for path, text in files.items():
        (repo / path).write_text(text)
    _commit(repo, "init")
    db = tmp_path / "chain.db"
    ingest_repository(repo, commit_limit=20, db_path=db)

    def state(store_db):
        _, edges = CortexStore(store_db).fetch_graph(repo)
        return {e.edge_id: (e.target, json.dumps(e.metadata, sort_keys=True)) for e in edges if e.relation == "calls"}

    def target_of(table, callee: str) -> str:
        (value,) = [t for k, (t, _) in table.items() if k.startswith("treesitter:app.cpp:") and f".{callee}:" in k or k.startswith("treesitter:app.cpp:") and k.endswith(f":{callee}:3")]
        return value

    before = state(db)
    assert target_of(before, "apply") == "symbol:item.h:apply"

    # manager.cpp is the only file that changes: `current()` now returns an `Other`
    (repo / "manager.cpp").write_text('#include "manager.h"\nOther* Manager::current() { return nullptr; }\n')
    _commit(repo, "return an Other")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    incremental = state(db)
    full_db = tmp_path / "chain-full.db"
    ingest_repository(repo, commit_limit=20, db_path=full_db)

    assert incremental == state(full_db)
    assert target_of(incremental, "apply") == "symbol:other.h:apply"
