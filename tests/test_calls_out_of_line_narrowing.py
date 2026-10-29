"""Plan 0.16, Task 6b: an out-of-line member of a narrowed class.

Out-of-line members (`void Bar::common() {}` in a .cpp) are found by the class label (`qualified_defs`).  When two
modules each carry a `Bar` and the class lookup narrowed to exactly one of them, the definitions in the other
module's .cpp are not candidates: they are narrowed to the files that belong to the chosen class (its own file,
its implementation sibling, a file within the include ring of its header).  It resolves only when exactly one
definition remains.
"""
from __future__ import annotations

from pathlib import Path

from cortex.graph import build_graph
from cortex.ingest import _classify_path
from cortex.models import SourceRecord
from tests.golden_utils import TREE_SITTER_ONLY

HEADER = "#pragma once\nclass Bar {{ public: void common(); void only_{m}(); }};\n"
IMPL = '#include "bar.h"\nvoid Bar::common() {{}}\nvoid Bar::only_{m}() {{}}\n'


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


def _modules() -> dict[str, str]:
    files: dict[str, str] = {}
    for m in ("a", "b"):
        files[f"{m}/bar.h"] = HEADER.format(m=m)
        files[f"{m}/bar.cpp"] = IMPL.format(m=m)
    return files


@TREE_SITTER_ONLY
def test_the_narrowed_class_picks_its_own_out_of_line_definition() -> None:
    files = _modules()
    files["use/use.cpp"] = '#include "../a/bar.h"\nvoid use(Bar* bar) { bar->common(); }\n'

    _, edges = _graph(files)

    edge = _call(edges, "common", "use/use.cpp")
    assert edge.target == "symbol:a/bar.cpp:common", (edge.target, edge.metadata)
    assert edge.metadata["resolution"] == "declared_type" and "ambiguous" not in edge.metadata


@TREE_SITTER_ONLY
def test_each_caller_gets_the_definition_of_the_module_it_includes() -> None:
    files = _modules()
    for m in ("a", "b"):
        files[f"use/{m}.cpp"] = f'#include "../{m}/bar.h"\nvoid use_{m}(Bar* bar) {{ bar->common(); }}\n'

    _, edges = _graph(files)

    for m in ("a", "b"):
        assert _call(edges, "common", f"use/{m}.cpp").target == f"symbol:{m}/bar.cpp:common"


@TREE_SITTER_ONLY
def test_a_definition_in_a_file_of_the_header_include_ring_is_found() -> None:
    # the definitions sit in a file that is not the header's stem sibling but includes the header
    files = {
        "a/bar.h": HEADER.format(m="a"),
        "a/bar_impl.cpp": IMPL.format(m="a"),
        "b/bar.h": HEADER.format(m="b"),
        "b/bar_impl.cpp": IMPL.format(m="b"),
        "use/use.cpp": '#include "../a/bar.h"\nvoid use(Bar* bar) { bar->common(); }\n',
    }

    _, edges = _graph(files)

    assert _call(edges, "common", "use/use.cpp").target == "symbol:a/bar_impl.cpp:common"


@TREE_SITTER_ONLY
def test_a_definition_in_an_included_impl_header_the_header_includes_is_found() -> None:
    files = {
        "a/bar.h": HEADER.format(m="a") + '#include "bar_impl.hpp"\n',
        "a/bar_impl.hpp": "void Bar::common() {}\nvoid Bar::only_a() {}\n",
        "b/bar.h": HEADER.format(m="b") + '#include "bar_impl.hpp"\n',
        "b/bar_impl.hpp": "void Bar::common() {}\nvoid Bar::only_b() {}\n",
        "use/use.cpp": '#include "../b/bar.h"\nvoid use(Bar* bar) { bar->common(); }\n',
    }

    _, edges = _graph(files)

    assert _call(edges, "common", "use/use.cpp").target == "symbol:b/bar_impl.hpp:common"


@TREE_SITTER_ONLY
def test_no_narrowing_possible_stays_ambiguous() -> None:
    files = _modules()
    files["use/use.cpp"] = "void use(Bar* bar) { bar->common(); }\n"

    _, edges = _graph(files)

    edge = _call(edges, "common", "use/use.cpp")
    assert edge.target == "name:common" and edge.metadata["ambiguous"] is True
    assert sorted(edge.metadata["candidates"]) == ["symbol:a/bar.cpp:common", "symbol:b/bar.cpp:common"]


@TREE_SITTER_ONLY
def test_two_definitions_in_the_chosen_class_files_stay_ambiguous() -> None:
    # a's Bar::common is defined in two implementation siblings (a platform split): nothing says which one, so
    # the list stays what it was before the narrowing (both of a's files and b's)
    files = _modules()
    files["a/bar.cc"] = '#include "bar.h"\nvoid Bar::common() {}\n'
    files["use/use.cpp"] = '#include "../a/bar.h"\nvoid use(Bar* bar) { bar->common(); }\n'

    _, edges = _graph(files)

    edge = _call(edges, "common", "use/use.cpp")
    assert edge.target == "name:common" and edge.metadata["ambiguous"] is True
    assert sorted(edge.metadata["candidates"]) == ["symbol:a/bar.cc:common", "symbol:a/bar.cpp:common", "symbol:b/bar.cpp:common"]


@TREE_SITTER_ONLY
def test_a_unique_class_label_keeps_every_definition() -> None:
    # one Bar: the class lookup narrowed nothing, so the definitions are not filtered (today's result)
    files = {
        "a/bar.h": HEADER.format(m="a"),
        "a/bar.cpp": IMPL.format(m="a"),
        "a/extra.cpp": '#include "bar.h"\nvoid Bar::common() {}\n',
        "use/use.cpp": '#include "../a/bar.h"\nvoid use(Bar* bar) { bar->common(); }\n',
    }

    _, edges = _graph(files)

    edge = _call(edges, "common", "use/use.cpp")
    assert edge.metadata.get("ambiguous") is True
    assert sorted(edge.metadata["candidates"]) == ["symbol:a/bar.cpp:common", "symbol:a/extra.cpp:common"]


@TREE_SITTER_ONLY
def test_this_calls_inside_the_class_use_the_same_narrowing() -> None:
    files = _modules()
    for m in ("a", "b"):
        files[f"{m}/bar.cpp"] = IMPL.format(m=m) + "void Bar::run() { common(); }\n"
        files[f"{m}/bar.h"] = f"#pragma once\nclass Bar {{ public: void common(); void run(); void only_{m}(); }};\n"

    _, edges = _graph(files)

    for m in ("a", "b"):
        assert _call(edges, "common", f"{m}/bar.cpp").target == f"symbol:{m}/bar.cpp:common"
