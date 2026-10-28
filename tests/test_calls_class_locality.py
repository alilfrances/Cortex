"""Plan 0.16, Task 6: `member:ambiguous`. Several classes share the label a call names.

The member's declared type, or the enclosing class, is a label that two classes carry (same name in two
modules).  `_Index.prefer` already picks the class in the caller's file, in a file the caller includes, or in
the nearest directory.  When that leaves several, a second attempt (see `callresolve._resolve_into`) also
accepts the class that sits in the include ring of the calling file and of its header sibling (`foo.cpp` ->
`foo.h`), up to three include hops.  It resolves only when exactly one class remains, and never changes a
call that already resolved.
"""
from __future__ import annotations

from pathlib import Path

from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from tests.golden_utils import TREE_SITTER_ONLY

BAR = "#pragma once\nclass Bar {{ public: void common() {{}} void only_{m}() {{}} }};\n"


def _graph(files: dict[str, str]):
    sources = [
        SourceRecord(path=path, content=content, kind=_classify_path(Path(path)), size_bytes=len(content.encode()), modified_at=0.0)
        for path, content in files.items()
    ]
    return build_graph(sources, [])


def _call(edges, callee: str, path: str):
    found = [e for e in edges if e.relation == "calls" and e.metadata.get("callee") == callee and e.metadata["source_file"] == path]
    assert len(found) == 1, [(e.target, e.metadata) for e in found]
    return found[0]


def _modules(caller) -> dict[str, str]:
    """Two modules `a` and `b`: `m/bar.h` (class Bar), `m/foo.h` (class Foo holding `Bar *m_bar`, including
    "bar.h") and the source `impl/m/foo.cpp`, whose text `caller(m)` gives.  The sources sit in a tree of their
    own, so the nearest-directory rule has nothing to choose by (no shared prefix with either module)."""
    files: dict[str, str] = {}
    for m in ("a", "b"):
        files[f"{m}/bar.h"] = BAR.format(m=m)
        files[f"{m}/foo.h"] = '#pragma once\n#include "bar.h"\nclass Foo { public: void run(); private: Bar* m_bar; };\n'
        files[f"impl/{m}/foo.cpp"] = caller(m)
    return files


@TREE_SITTER_ONLY
def test_the_caller_that_includes_the_class_directly_already_resolves() -> None:
    files = _modules(lambda m: f'#include "../../{m}/foo.h"\n#include "../../{m}/bar.h"\nvoid Foo::run() {{ m_bar->common(); }}\n')

    _, edges = _graph(files)

    assert _call(edges, "common", "impl/a/foo.cpp").target == "symbol:a/bar.h:common"
    assert _call(edges, "common", "impl/b/foo.cpp").target == "symbol:b/bar.h:common"


@TREE_SITTER_ONLY
def test_the_declaring_header_two_hops_away_picks_the_class() -> None:
    # impl/a/foo.cpp -> foo.h -> bar.h: Foo is one include away, Bar two; the directories tie
    files = _modules(lambda m: f'#include "../../{m}/foo.h"\nvoid Foo::run() {{ m_bar->common(); }}\n')

    _, edges = _graph(files)

    for m in ("a", "b"):
        edge = _call(edges, "common", f"impl/{m}/foo.cpp")
        assert edge.target == f"symbol:{m}/bar.h:common", (m, edge.target, edge.metadata)
        assert edge.metadata["resolution"] == "declared_type" and "ambiguous" not in edge.metadata


@TREE_SITTER_ONLY
def test_a_private_header_in_between_stays_inside_three_hops() -> None:
    # foo.cpp -> foo_p.h -> foo.h -> bar.h
    files = _modules(lambda m: 'x')
    for m in ("a", "b"):
        files[f"impl/{m}/foo.cpp"] = '#include "foo_p.h"\nvoid Foo::run() { m_bar->common(); }\n'
        files[f"impl/{m}/foo_p.h"] = f'#pragma once\n#include "../../{m}/foo.h"\n'

    _, edges = _graph(files)

    for m in ("a", "b"):
        assert _call(edges, "common", f"impl/{m}/foo.cpp").target == f"symbol:{m}/bar.h:common"


@TREE_SITTER_ONLY
def test_the_header_sibling_of_the_calling_file_supplies_the_include_ring() -> None:
    # impl/a/foo.cpp includes only a precompiled header; its sibling a/foo.h (same stem) includes bar.h.
    # Module b has a Bar but no foo.h, so only a's header is a sibling.
    files = {
        "a/bar.h": BAR.format(m="a"),
        "a/foo.h": '#pragma once\n#include "bar.h"\nclass Foo { public: void run(); private: Bar* m_bar; };\n',
        "impl/a/foo.cpp": '#include "pch.h"\nvoid Foo::run() { m_bar->common(); }\n',
        "b/bar.h": BAR.format(m="b"),
        "pch.h": "#pragma once\n",
    }

    _, edges = _graph(files)

    assert _call(edges, "common", "impl/a/foo.cpp").target == "symbol:a/bar.h:common"


@TREE_SITTER_ONLY
def test_two_header_siblings_in_different_directories_leave_the_call_ambiguous() -> None:
    # a/foo.h and b/foo.h share the stem, so both are siblings of both sources: nothing to choose by
    files = _modules(lambda m: 'void Foo::run() { m_bar->common(); }\n')

    _, edges = _graph(files)

    edge = _call(edges, "common", "impl/a/foo.cpp")
    assert edge.target == "name:common" and edge.metadata["ambiguous"] is True


@TREE_SITTER_ONLY
def test_an_ambiguous_enclosing_class_with_different_member_types_resolves_through_the_ring() -> None:
    files: dict[str, str] = {}
    for m, t in (("a", "X"), ("b", "Y")):
        files[f"{m}/types.h"] = f"#pragma once\nclass {t} {{ public: void go() {{}} }};\n"
        files[f"{m}/widget.h"] = f'#pragma once\n#include "types.h"\nclass Widget {{ public: void run(); private: {t}* m_t; }};\n'
        files[f"{m}/all.h"] = '#pragma once\n#include "widget.h"\n'
        files[f"impl/{m}/widget.cpp"] = f'#include "../../{m}/all.h"\nvoid Widget::run() {{ m_t->go(); }}\n'

    _, edges = _graph(files)

    for m in ("a", "b"):
        edge = _call(edges, "go", f"impl/{m}/widget.cpp")
        assert edge.target == f"symbol:{m}/types.h:go", (m, edge.target, edge.metadata)


@TREE_SITTER_ONLY
def test_classes_beyond_three_include_hops_stay_ambiguous() -> None:
    files = {
        "a/bar.h": BAR.format(m="a"),
        "b/bar.h": BAR.format(m="b"),
        "a/foo.h": '#pragma once\n#include "bar.h"\nclass Foo { public: void run(); private: Bar* m_bar; };\n',
        "impl/user.cpp": '#include "h1.h"\nvoid Foo::run() { m_bar->common(); }\n',
        "impl/h1.h": '#pragma once\n#include "h2.h"\n',
        "impl/h2.h": '#pragma once\n#include "h3.h"\n',
        "impl/h3.h": '#pragma once\n#include "h4.h"\n',
        "impl/h4.h": '#pragma once\n#include "../a/foo.h"\n',
    }

    _, edges = _graph(files)

    edge = _call(edges, "common", "impl/user.cpp")
    assert edge.target == "name:common" and edge.metadata["ambiguous"] is True


@TREE_SITTER_ONLY
def test_neither_class_in_reach_and_equally_near_stays_ambiguous() -> None:
    files = {
        "x/bar.h": BAR.format(m="x"),
        "y/bar.h": BAR.format(m="y"),
        "z/use.cpp": "void use(Bar* b) { b->common(); }\n",
    }

    _, edges = _graph(files)

    edge = _call(edges, "common", "z/use.cpp")
    assert edge.target == "name:common" and edge.metadata["ambiguous"] is True
    assert sorted(edge.metadata["candidates"]) == ["symbol:x/bar.h:common", "symbol:y/bar.h:common"]


@TREE_SITTER_ONLY
def test_a_call_that_already_resolved_to_the_only_class_with_the_member_is_unchanged() -> None:
    # `only_a` exists on a's Bar alone, so the union lookup resolved it before; the ring must not move it
    files = _modules(lambda m: f'#include "../../{m}/foo.h"\nvoid Foo::run() {{ m_bar->only_a(); }}\n')

    _, edges = _graph(files)

    assert _call(edges, "only_a", "impl/a/foo.cpp").target == "symbol:a/bar.h:only_a"
    assert _call(edges, "only_a", "impl/b/foo.cpp").target == "symbol:a/bar.h:only_a"


@TREE_SITTER_ONLY
def test_a_changed_include_line_re_resolves_callers_that_did_not_change(tmp_path) -> None:
    import json

    from cortex.ingest import ingest_repository
    from cortex.store import CortexStore
    from tests.test_call_resolution import _commit, _repo

    repo = _repo(tmp_path)
    files = _modules(lambda m: f'#include "../../{m}/all.h"\nvoid Foo::run() {{ m_bar->common(); }}\n')
    for m in ("a", "b"):
        files[f"{m}/all.h"] = '#pragma once\n#include "foo.h"\n'
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    _commit(repo, "init")
    db = tmp_path / "inc.db"
    ingest_repository(repo, commit_limit=20, db_path=db)

    def state(store_db):
        _, edges = CortexStore(store_db).fetch_graph(repo)
        return {e.edge_id: (e.target, json.dumps(e.metadata, sort_keys=True)) for e in edges if e.relation == "calls"}

    before = state(db)
    assert {t for t, _ in before.values() if t.startswith("symbol:")} == {"symbol:a/bar.h:common", "symbol:b/bar.h:common"}

    # a/all.h (no class in it) stops including foo.h: impl/a/foo.cpp did not change, but its include ring no
    # longer reaches a/foo.h and a/bar.h
    (repo / "a" / "all.h").write_text("#pragma once\n")
    _commit(repo, "drop the include")
    ingest_repository(repo, commit_limit=20, db_path=db, incremental=True)
    incremental = state(db)
    full_db = tmp_path / "full.db"
    ingest_repository(repo, commit_limit=20, db_path=full_db)

    assert incremental == state(full_db)
    stale = [v for k, v in incremental.items() if "impl/a/foo.cpp" in k]
    assert stale and stale[0][0] == "name:common" and json.loads(stale[0][1]).get("ambiguous") is True
