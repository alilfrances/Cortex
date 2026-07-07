from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cortex.graph import build_graph
from cortex.ingest import ingest_repository
from cortex.models import SourceRecord
from cortex.store import CortexStore


FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "multilang_repo"


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(path=path, content=content, kind="code", size_bytes=len(content), modified_at=0)


def test_regex_fallback_when_tree_sitter_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tree_sitter", None)

    from cortex.structural import extract_structural_edges

    nodes, edges = extract_structural_edges("app.js", "import thing from './thing';\nfunction run() {}\n", set())

    assert "symbol:app.js:run" in {node.node_id for node in nodes}
    assert {edge.confidence for edge in edges} == {"LOW"}
    assert {"imports", "contains"}.issubset({edge.relation for edge in edges})


def test_python_files_are_not_routed_to_structural_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    import cortex.graph as graph

    def fail_dispatch(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise AssertionError("Python files must stay on ast_extract.extract_python_edges")

    monkeypatch.setattr(graph, "extract_structural_edges", fail_dispatch)

    nodes, edges = build_graph([_source("app.py", "def run():\n    return 1\n")], commits=[])

    assert "symbol:app.py:run" in {node.node_id for node in nodes}
    assert any(edge.confidence == "EXTRACTED" for edge in edges if edge.relation == "contains")


def test_dispatcher_falls_back_to_regex_when_tree_sitter_parse_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    nodes, edges = extract_structural_edges("service.go", "import \"fmt\"\nfunc Run() {}\n", set())

    assert "symbol:service.go:Run" in {node.node_id for node in nodes}
    assert edges
    assert {edge.confidence for edge in edges} == {"LOW"}


def test_c_regex_fallback_extracts_imports_and_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = '#include <stdio.h>\nstruct Counter { int value; };\nint add(int left, int right) {\n    return left + right;\n}\n'
    nodes, edges = extract_structural_edges("main.c", content, set())

    assert {"symbol:main.c:Counter", "symbol:main.c:add"}.issubset({node.node_id for node in nodes})
    assert "module:stdio.h" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"LOW"}


def test_cpp_regex_fallback_extracts_imports_and_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = '#include "engine.hpp"\nnamespace Engine {\nclass Runner { public: void start() {} };\nint Engine::run() {\n    return 1;\n}\n}\n'
    nodes, edges = extract_structural_edges("engine.cpp", content, set())

    assert {"symbol:engine.cpp:Engine", "symbol:engine.cpp:Runner", "symbol:engine.cpp:run"}.issubset(
        {node.node_id for node in nodes}
    )
    assert "module:engine.hpp" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"LOW"}


def test_cpp_regex_fallback_resolves_local_include_to_file_node(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = '#include "engine.hpp"\nclass Runner { public: void start() {} };\n'
    nodes, edges = extract_structural_edges("engine.cpp", content, {"engine.hpp"})

    assert "file:engine.hpp" in {edge.target for edge in edges if edge.relation == "imports"}


def test_cpp_regex_backend_extracts_inherits_edges_directly() -> None:
    from cortex.structural.regex_backend import extract_regex_edges

    content = (
        '#include "foo.hpp"\n\n'
        "class Foo : public Bar, private Baz { public: void run(); };\n"
    )
    nodes, edges = extract_regex_edges("inherit.hpp", content, set())

    assert "symbol:inherit.hpp:Foo" in {node.node_id for node in nodes}
    assert {
        (edge.source, edge.target, edge.confidence)
        for edge in edges
        if edge.relation == "inherits"
    } == {
        ("symbol:inherit.hpp:Foo", "name:Bar", "LOW"),
        ("symbol:inherit.hpp:Foo", "name:Baz", "LOW"),
    }


def test_qml_regex_fallback_extracts_imports_and_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = 'import QtQuick.Controls 2.15\nItem {\n    signal started()\n    function launch() {}\n}\n'
    nodes, edges = extract_structural_edges("Main.qml", content, set())

    assert {"symbol:Main.qml:Item", "symbol:Main.qml:started", "symbol:Main.qml:launch"}.issubset(
        {node.node_id for node in nodes}
    )
    assert "module:QtQuick.Controls" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"LOW"}


def test_qt_cpp_regex_fallback_extracts_signals_slots_and_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = """
class Controller : public QObject {
    Q_OBJECT
signals:
    void started(int value);
public slots:
    void start();
};

void Controller::run() {
    emit started(1);
    Q_EMIT started(2);
    connect(this, &Controller::started, this, &Controller::start);
    connect(this, SIGNAL(started(int)), this, SLOT(start()));
    // emit ignoredComment();
    const char *text = "emit ignoredString()";
}
"""
    nodes, edges = extract_structural_edges("controller.hpp", content, set())
    by_id = {node.node_id: node for node in nodes}

    assert by_id["symbol:controller.hpp:Controller"].metadata["qt"] == "qobject"
    assert by_id["symbol:controller.hpp:started"].metadata["qt"] == "signal"
    assert by_id["symbol:controller.hpp:start"].metadata["qt"] == "slot"
    assert "symbol:controller.hpp:ignoredComment" not in by_id
    assert "symbol:controller.hpp:ignoredString" not in by_id

    emits = [edge for edge in edges if edge.relation == "emits"]
    connects = [edge for edge in edges if edge.relation == "connects"]

    assert [edge.target for edge in emits] == ["symbol:controller.hpp:started", "symbol:controller.hpp:started"]
    assert {edge.confidence for edge in emits + connects} == {"LOW"}
    assert any(
        edge.source == "symbol:controller.hpp:started"
        and edge.target == "symbol:controller.hpp:start"
        and edge.metadata.get("sender_class") == "Controller"
        and edge.metadata.get("receiver_class") == "Controller"
        for edge in connects
    )
    assert any(
        edge.source == "symbol:controller.hpp:started"
        and edge.target == "symbol:controller.hpp:start"
        and edge.metadata.get("sender") == "this"
        and edge.metadata.get("receiver") == "this"
        for edge in connects
    )


def test_qml_regex_fallback_extracts_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    content = "import QtQuick.Controls 2.15\nButton {\n    signal saved()\n    onClicked: saved()\n}\n"
    nodes, edges = extract_structural_edges("ButtonView.qml", content, set())

    assert "symbol:ButtonView.qml:saved" in {node.node_id for node in nodes}
    handles = [edge for edge in edges if edge.relation == "handles"]
    assert len(handles) == 1
    assert handles[0].source == "file:ButtonView.qml"
    assert handles[0].target == "module:onClicked"
    assert handles[0].confidence == "LOW"


def test_c_regex_fallback_ignores_prototypes(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    nodes, _ = extract_structural_edges("engine.h", "int foo(int x);\n", set())

    assert "symbol:engine.h:foo" not in {node.node_id for node in nodes}


def test_c_tree_sitter_extracts_imports_and_symbols() -> None:
    pytest.importorskip("tree_sitter_c")
    from cortex.structural import extract_structural_edges

    content = '#include <stdio.h>\nstruct Counter { int value; };\nint add(int left, int right) {\n    return left + right;\n}\n'
    nodes, edges = extract_structural_edges("main.c", content, set())

    assert {"symbol:main.c:Counter", "symbol:main.c:add"}.issubset({node.node_id for node in nodes})
    assert "module:stdio.h" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"EXTRACTED"}


def test_cpp_tree_sitter_extracts_imports_and_symbols() -> None:
    pytest.importorskip("tree_sitter_cpp")
    from cortex.structural import extract_structural_edges

    content = '#include "engine.hpp"\nnamespace Engine {\nclass Runner { public: void start() {} };\nint compute(int value) {\n    return value + 1;\n}\n}\n'
    nodes, edges = extract_structural_edges("engine.cpp", content, set())

    assert {"symbol:engine.cpp:Engine", "symbol:engine.cpp:Runner", "symbol:engine.cpp:compute"}.issubset(
        {node.node_id for node in nodes}
    )
    assert "module:engine.hpp" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"EXTRACTED"}


def test_cpp_tree_sitter_resolves_local_include_to_file_node() -> None:
    pytest.importorskip("tree_sitter_cpp")
    from cortex.structural import extract_structural_edges

    content = '#include "engine.hpp"\nnamespace Engine {\nclass Runner { public: void start() {} };\n}\n'
    nodes, edges = extract_structural_edges("engine.cpp", content, {"engine.hpp"})

    assert "file:engine.hpp" in {edge.target for edge in edges if edge.relation == "imports"}


def test_cpp_tree_sitter_extracts_inherits_edges() -> None:
    pytest.importorskip("tree_sitter_cpp")
    from cortex.structural import extract_structural_edges

    nodes, edges = extract_structural_edges("inherit.cpp", "class Foo : public Bar { public: void run(); };\n", set())

    assert "symbol:inherit.cpp:Foo" in {node.node_id for node in nodes}
    assert any(
        edge.source == "symbol:inherit.cpp:Foo"
        and edge.target == "name:Bar"
        and edge.relation == "inherits"
        and edge.confidence == "EXTRACTED"
        for edge in edges
    )


def test_cpp_tree_sitter_preserves_qt_emit_and_connect_edges() -> None:
    pytest.importorskip("tree_sitter_cpp")
    from cortex.structural import extract_structural_edges

    content = """
#define emit
int connect(...);
class Controller : public QObject {
public:
    void started(int value);
    void start();
    void run();
};

void Controller::run() {
    emit started(1);
    connect(this, &Controller::started, this, &Controller::start);
}
"""
    nodes, edges = extract_structural_edges("controller.cpp", content, set())
    node_ids = {node.node_id for node in nodes}

    assert "symbol:controller.cpp:Controller" in node_ids
    assert any(edge.relation == "emits" and edge.target == "module:started" for edge in edges)
    assert any(
        edge.relation == "connects"
        and edge.source == "module:started"
        and edge.target == "module:start"
        and edge.metadata.get("sender_class") == "Controller"
        and edge.metadata.get("receiver_class") == "Controller"
        for edge in edges
    )


def test_qml_tree_sitter_extracts_imports_and_symbols() -> None:
    pytest.importorskip("tree_sitter_language_pack")
    from cortex.structural import extract_structural_edges

    content = 'import QtQuick.Controls 2.15\nItem {\n    signal started()\n    function launch() {}\n}\n'
    nodes, edges = extract_structural_edges("Main.qml", content, set())

    assert {"symbol:Main.qml:Item", "symbol:Main.qml:launch"}.issubset({node.node_id for node in nodes})
    assert "module:QtQuick.Controls" in {edge.target for edge in edges if edge.relation == "imports"}
    assert {edge.confidence for edge in edges} == {"EXTRACTED"}


def test_c_tree_sitter_ignores_struct_usages() -> None:
    pytest.importorskip("tree_sitter_c")
    from cortex.structural import extract_structural_edges

    nodes, _ = extract_structural_edges("usage.c", "struct Foo x;\n", set())

    assert "symbol:usage.c:Foo" not in {node.node_id for node in nodes}


def test_resolve_local_import_matches_exact_path_and_unique_basename() -> None:
    from cortex.structural.regex_backend import resolve_local_import

    known_paths = {"src/hw/airpod.h", "src/hw/airpod.cpp"}

    assert resolve_local_import("src/hw/airpod.h", known_paths) == "src/hw/airpod.h"
    assert resolve_local_import("airpod.h", known_paths) == "src/hw/airpod.h"
    assert resolve_local_import("<stdio.h>".strip("<>"), known_paths) is None


def test_resolve_local_import_returns_none_on_ambiguous_basename() -> None:
    from cortex.structural.regex_backend import resolve_local_import

    known_paths = {"src/a/util.h", "src/b/util.h"}

    assert resolve_local_import("util.h", known_paths) is None


def test_dispatcher_returns_empty_when_regex_fallback_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.structural import extract_structural_edges
    import cortex.structural.regex_backend as regex_backend
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail(*args: object, **kwargs: object) -> tuple[list[object], list[object]]:
        raise RuntimeError("boom")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail)
    monkeypatch.setattr(regex_backend, "extract_regex_edges", fail)

    assert extract_structural_edges("broken.rs", "pub fn run( {", set()) == ([], [])


def test_ingest_multilang_fixture_uses_low_confidence_regex_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "tree_sitter", None)
    repo = tmp_path / "multilang_repo"
    shutil.copytree(FIXTURE_REPO, repo)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixtures"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    db_path = tmp_path / "cortex.db"
    ingest_repository(repo, commit_limit=0, db_path=db_path)

    nodes, edges = CortexStore(db_path).fetch_graph(repo)
    node_ids = {node.node_id for node in nodes}

    assert "symbol:app.js:start" in node_ids
    assert "symbol:client.ts:Client" in node_ids
    assert "symbol:main.go:Run" in node_ids
    assert "symbol:lib.rs:run" in node_ids
    assert "symbol:App.java:App" in node_ids
    assert "symbol:worker.rb:Worker" in node_ids
    assert "symbol:main.c:Counter" in node_ids
    assert "symbol:engine.cpp:Runner" in node_ids
    assert "symbol:engine.hpp:EngineCore" in node_ids
    assert "symbol:Main.qml:Item" in node_ids

    fixture_paths = {path.name for path in FIXTURE_REPO.iterdir()}
    structural = [
        edge
        for edge in edges
        if edge.layer == "STRUCTURAL" and edge.metadata.get("source_file") in fixture_paths
    ]
    assert structural
    assert {edge.confidence for edge in structural} == {"LOW"}
