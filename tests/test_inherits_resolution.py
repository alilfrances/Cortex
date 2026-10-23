"""B.1 (0.12.2): `inherits` edges are resolved to `symbol:` ids in the graph, like `calls` edges."""
from __future__ import annotations

import json

from cortex.callresolve import MAX_CANDIDATES, resolve_call_endpoints
from cortex.ingest import ingest_repository
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore, default_db_path
from tests.bundle_shapes import make_repo


def test_inherits_resolution_rate(field_shape_repo):
    """Hard gate: at least 80% of the `inherits` edges whose base class is defined in the repository resolve to
    a `symbol:` target on the --field-shape repository (0% in 0.12.1)."""
    nodes, edges = CortexStore(default_db_path(field_shape_repo)).fetch_graph(field_shape_repo)
    class_labels = {node.label for node in nodes if node.kind == "class" and node.granularity == "symbol"}
    in_repo = []
    for edge in edges:
        if edge.relation != "inherits":
            continue
        original = edge.metadata.get("unresolved_target") or edge.target
        label = original.split(":", 1)[1].split("::")[-1]
        if original.startswith("name:") and label in class_labels:
            in_repo.append(edge)
    assert len(in_repo) >= 10, "the profile must contain in-repo bases"
    resolved = [edge for edge in in_repo if edge.target.startswith("symbol:")]
    assert len(resolved) / len(in_repo) >= 0.8, (len(resolved), len(in_repo))


def test_status_reports_the_resolution_block(field_shape_repo):
    from cortex.mcp.tools import call_tool

    result = call_tool("cortex_status", {"repo_path": str(field_shape_repo), "response_format": "detailed"})
    payload = json.loads(result["content"][0]["text"])

    resolution = payload["resolution"]
    assert {"symbol", "name", "external"} <= set(resolution["calls"])
    assert {"symbol", "name", "module"} <= set(resolution["inherits"])
    assert resolution["inherits"]["symbol"] > 0 and resolution["calls"]["symbol"] > 0
    assert {"name_external", "in_repo_symbol_share", "name_by_family"} <= set(resolution["calls"])
    assert {"name_external", "in_repo_symbol_share"} <= set(resolution["inherits"])
    assert resolution["inherits"]["in_repo_symbol_share"] >= 0.95, resolution["inherits"]
    assert resolution["calls"]["in_repo_symbol_share"] >= 0.6, resolution["calls"]


# --- the resolver on hand-built graphs --------------------------------------------------------------

def cls(path: str, name: str, *, span=(1, 5)) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="class", label=name, source_ref=path, granularity="symbol",
        span_start=span[0], span_end=span[1],
    )


def inherits(path: str, derived: str, base: str, *, target: str | None = None, confidence="EXTRACTED") -> GraphEdge:
    return GraphEdge(
        edge_id=f"treesitter:{path}:inherits:{derived}:{base}", source=f"symbol:{path}:{derived}",
        target=target or f"name:{base}", relation="inherits", layer="STRUCTURAL", confidence=confidence,
        metadata={"lineno": 1, "source_file": path},
    )


def resolved(nodes, edges, **scope):
    return {e.edge_id: e for e in resolve_call_endpoints(nodes, edges, **scope) if e.relation == "inherits"}


def test_a_unique_base_becomes_its_symbol_and_keeps_the_label():
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:src/b.h:Base"
    assert out.metadata["unresolved_target"] == "name:Base"
    assert out.metadata["resolution"] == "label"
    assert out.confidence == "EXTRACTED" and "ambiguous" not in out.metadata


def test_the_inheritance_pass_runs_without_any_call_edge_and_in_a_scoped_pass_with_none_affected():
    """The first version returned early when the graph had no contract call."""
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    assert resolved(nodes, [edge])[edge.edge_id].target == "symbol:src/b.h:Base"

    edge = inherits("src/d.h", "Derived", "Base")
    out = resolved(nodes, [edge], affected_files={"src/d.h"}, changed_labels=set())[edge.edge_id]
    assert out.target == "symbol:src/b.h:Base"


def test_mocks_fixtures_other_languages_and_file_local_classes_lose_to_the_real_base():
    nodes = [
        cls("src/field/d.h", "Derived"),
        cls("src/field/coredevice.h", "CoreDevice"),
        cls("tests/mocks/mock_a/coredevice.h", "CoreDevice"),
        cls("tests/mocks/mock_b/coredevice.h", "CoreDevice"),
        cls("mocks/coredevice.h", "CoreDevice"),
        cls("src/field/unrelated.cpp", "CoreDevice"),
        cls("js/coredevice.js", "CoreDevice"),
    ]
    edge = inherits("src/field/d.h", "Derived", "CoreDevice")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:src/field/coredevice.h:CoreDevice"
    assert out.metadata["resolution"] == "preferred"


def test_a_test_class_may_derive_from_a_mock():
    nodes = [
        cls("tests/unit/t.cpp", "Probe"), cls("src/b.h", "Base"), cls("mocks/base.h", "Base"),
    ]
    edge = inherits("tests/unit/t.cpp", "Probe", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    # neither shares a directory with the caller (a mock under tests/ would win, see the nearest-directory tests)
    assert out.target.startswith("name:")  # both stay candidates: a test may mean either
    assert out.metadata["ambiguous"] is True and len(out.metadata["candidates"]) == 2


def test_an_ambiguous_base_keeps_name_and_records_its_candidates_like_a_call():
    paths = [f"lib{i}/base.h" for i in range(MAX_CANDIDATES + 3)]
    nodes = [cls("src/d.h", "Derived"), *[cls(p, "Base") for p in paths]]
    edge = inherits("src/d.h", "Derived", "Base")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "name:Base"
    assert out.metadata["ambiguous"] is True
    assert out.metadata["candidates"] == sorted(f"symbol:{p}:Base" for p in paths)[:MAX_CANDIDATES]
    assert out.confidence == "LOW" and out.metadata["extracted_confidence"] == "EXTRACTED"
    assert "unresolved_target" not in out.metadata


def test_a_base_outside_the_repository_and_a_class_deriving_from_itself_stay_as_written():
    nodes = [cls("src/d.h", "Derived"), cls("src/q.h", "Widget")]
    external = inherits("src/d.h", "Derived", "QObject")
    own = inherits("src/q.h", "Widget", "Widget")  # `class Widget : public ns::Widget`, base elsewhere

    out = resolved(nodes, [external, own])

    assert out[external.edge_id].target == "name:QObject"
    assert out[own.edge_id].target == "name:Widget"


def test_resolution_is_per_language_family_and_module_targets_resolve_in_their_family():
    nodes = [cls("qml/View.qml", "View"), cls("qml/Item.qml", "Item"), cls("src/item.h", "Item")]
    edge = inherits("qml/View.qml", "View", "Item", target="module:Item")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:qml/Item.qml:Item"  # not the C++ class of the same label
    assert out.metadata["unresolved_target"] == "module:Item"


def test_resolving_twice_or_after_the_base_vanished_gives_what_a_fresh_resolution_gives():
    nodes = [cls("src/d.h", "Derived"), cls("lib1/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    resolved(nodes, [edge])
    resolved(nodes, [edge])
    assert (edge.target, edge.metadata["unresolved_target"]) == ("symbol:lib1/b.h:Base", "name:Base")

    gone = resolved([cls("src/d.h", "Derived")], [edge], affected_files=set(), changed_labels=set(), widen_typed=False)
    assert gone[edge.edge_id].target == "name:Base"  # the stored target vanished: back to the label
    assert "unresolved_target" not in edge.metadata and "resolution" not in edge.metadata

    resolved(nodes, [edge], changed_labels={"Base"})
    again = resolved([*nodes, cls("lib2/b.h", "Base")], [edge], changed_labels={"Base"})[edge.edge_id]
    assert again.metadata["ambiguous"] is True  # a new same-label class in the batch re-opens the choice
    assert again.target == "name:Base" and "unresolved_target" not in again.metadata


def test_an_unrelated_scoped_batch_leaves_resolved_inheritance_alone():
    nodes = [cls("src/d.h", "Derived"), cls("src/b.h", "Base")]
    edge = inherits("src/d.h", "Derived", "Base")
    resolved(nodes, [edge])
    edge.metadata["marker"] = True  # a reset would drop this

    resolved(nodes, [edge], affected_files={"src/x.cpp"}, changed_labels={"Other"})

    assert edge.target == "symbol:src/b.h:Base" and edge.metadata["marker"] is True


# --- incremental ingest -------------------------------------------------------------------------------


def _inherit_targets(repo):
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    return {e.source: (e.target, e.metadata.get("unresolved_target")) for e in edges if e.relation == "inherits"}


FILES = {
    "src/base.h": "class Base {\npublic:\n    virtual void run();\n};\n",
    "src/derived.h": '#include "base.h"\nclass Derived : public Base {\npublic:\n    void run() override;\n};\n',
    "src/derived.cpp": '#include "derived.h"\nvoid Derived::run() { int x = 1; }\n',
}


def _commit(repo, message):
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", message], cwd=repo, check=True)


def _fresh_targets(repo, tmp_path, monkeypatch, name):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / name))
    ingest_repository(repo, incremental=False)
    return _inherit_targets(repo)


def test_renaming_the_base_class_does_not_leave_a_dangling_edge(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"] == ("symbol:src/base.h:Base", "name:Base")

    (repo / "src/base.h").write_text("class Foundation {\npublic:\n    virtual void run();\n};\n")
    _commit(repo, "rename")
    ingest_repository(repo)  # incremental

    incremental = _inherit_targets(repo)
    assert incremental["symbol:src/derived.h:Derived"] == ("name:Base", None)  # no class named Base any more
    assert incremental == _fresh_targets(repo, tmp_path, monkeypatch, "full-1")


def test_deleting_and_recreating_the_base_converges_with_a_full_ingest(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)

    (repo / "src/base.h").unlink()
    _commit(repo, "delete")
    ingest_repository(repo)
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"] == ("name:Base", None)

    (repo / "src/core").mkdir()
    (repo / "src/core/base.h").write_text("class Base {\npublic:\n    virtual void run();\n};\n")
    _commit(repo, "move")
    ingest_repository(repo)

    incremental = _inherit_targets(repo)
    assert incremental["symbol:src/derived.h:Derived"] == ("symbol:src/core/base.h:Base", "name:Base")
    assert incremental == _fresh_targets(repo, tmp_path, monkeypatch, "full-2")


def test_a_second_class_with_the_label_in_an_unrelated_file_reopens_then_closes_the_choice(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", FILES)

    (repo / "src/other").mkdir()
    (repo / "src/other/base.h").write_text("class Base {};\n")
    _commit(repo, "second Base")
    ingest_repository(repo)
    # the real base shares the derived class's directory: the choice stays unique
    assert _inherit_targets(repo)["symbol:src/derived.h:Derived"][0] == "symbol:src/base.h:Base"
    assert _inherit_targets(repo) == _fresh_targets(repo, tmp_path, monkeypatch, "full-3")

    (repo / "src/other/base.h").unlink()
    _commit(repo, "gone")
    ingest_repository(repo)
    assert _inherit_targets(repo) == _fresh_targets(repo, tmp_path, monkeypatch, "full-4")


def _resolution_state(repo):
    nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    edge_state = {
        e.edge_id: (e.target, e.confidence, {k: e.metadata.get(k) for k in ("unresolved_target", "resolution", "candidates", "ambiguous", "external")})
        for e in edges if e.relation in ("calls", "inherits")
    }
    alias_state = {n.node_id: n.metadata.get("path_aliases") for n in nodes if n.kind == "file"}
    return edge_state, alias_state


def test_an_incremental_refresh_of_a_field_shape_repository_converges_with_a_full_one(tmp_path, monkeypatch):
    """Delete a mock and a derived class, add a second real-looking base and a new derived class, edit the
    manager: calls, inherits and path aliases must be exactly what a fresh full index writes."""
    from tests.field_shape import build_field_shape_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "incremental-data"))
    repo = build_field_shape_repo(tmp_path / "repo", modules=12, commits=30)

    (repo / "tests/mocks/mock_a/coredevice.h").unlink()
    (repo / "src/field/widgetwaitready.h").unlink()
    (repo / "src/field/widgetwaitready.cpp").unlink()
    (repo / "src/field/extra").mkdir()
    (repo / "src/field/extra/coredevice.h").write_text("class CoreDevice {\npublic:\n    virtual bool isReady() const;\n};\n")
    (repo / "src/field/extra/gadgetspinlock.h").write_text(
        '#include "coredevice.h"\nclass GadgetSpinLockStep : public CoreDevice\n{\npublic:\n    bool isReady() const override;\n};\n'
    )
    (repo / "src/field/extra/gadgetspinlock.cpp").write_text(
        '#include "gadgetspinlock.h"\nbool GadgetSpinLockStep::isReady() const { return true; }\n'
    )
    manager = repo / "src/field/devicemanager.cpp"
    manager.write_text(manager.read_text() + "\nvoid DeviceManager::extra()\n{\n    auto *g = qobject_cast<GadgetSpinLockStep*>(m_devices.value(0));\n    g->isReady();\n}\n")
    _commit(repo, "churn")
    ingest_repository(repo, commit_limit=35)  # incremental
    incremental = _resolution_state(repo)

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "full-data"))
    ingest_repository(repo, commit_limit=35, incremental=False)

    assert incremental == _resolution_state(repo)


def test_the_server_answers_status_with_the_resolution_block_from_its_reader_thread(tmp_path):
    """`cortex_status` is answered while another call may be running, outside the work queue."""
    import io

    from cortex.mcp.server import serve

    repo = make_repo(tmp_path / "repo", FILES)
    frame = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "cortex_status", "arguments": {"repo_path": str(repo), "response_format": "detailed"}},
    }
    plain = {**frame, "id": 2, "params": {"name": "cortex_status", "arguments": {"repo_path": str(repo)}}}
    out = io.StringIO()

    serve(iter([json.dumps(frame) + "\n", json.dumps(plain) + "\n"]), out)

    replies = {r["id"]: json.loads(r["result"]["content"][0]["text"]) for r in map(json.loads, out.getvalue().splitlines())}
    assert replies[1]["resolution"]["inherits"]["symbol"] == 1
    assert "resolution" not in replies[2]


# --- out-of-repo bases are classified, not rewritten (0.14.0, plan Task 3.1) -------------------------------


def file_import(path: str, target: str) -> GraphEdge:
    return GraphEdge(
        edge_id=f"treesitter:{path}:import:1:{target}", source=f"file:{path}", target=target, relation="imports",
        layer="STRUCTURAL", confidence="EXTRACTED", metadata={"lineno": 1, "source_file": path},
    )


def file_node(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path, granularity="file")


def test_a_qt_base_outside_the_repository_is_classified_and_keeps_its_target():
    nodes = [cls("src/w.h", "Worker")]
    edge = inherits("src/w.h", "Worker", "QObject")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "name:QObject"
    assert out.metadata["external"] == "qt"
    assert "unresolved_target" not in out.metadata and "resolution" not in out.metadata


def test_prefixed_and_std_bases_are_classified_by_how_they_are_written():
    nodes = [cls("tests/t.cpp", "T"), cls("src/e.h", "Failure")]
    gtest = inherits("tests/t.cpp", "T", "Test", target="name:::testing::Test")
    boost = inherits("tests/t.cpp", "T", "noncopyable", target="name:boost::noncopyable")
    std = inherits("src/e.h", "Failure", "runtime_error", target="name:std::runtime_error")
    templated = inherits("src/e.h", "Failure", "variant", target="name:std::variant<int, double>")

    out = resolved(nodes, [gtest, boost, std, templated])

    assert out[gtest.edge_id].metadata["external"] == "prefix" and out[gtest.edge_id].target == "name:::testing::Test"
    assert out[boost.edge_id].metadata["external"] == "prefix"
    assert out[std.edge_id].metadata["external"] == "std"
    assert out[templated.edge_id].metadata["external"] == "std"


def test_a_bare_gtest_base_is_external_only_when_the_file_includes_a_gtest_header():
    nodes = [cls("tests/with.cpp", "WithHeader"), cls("tests/without.cpp", "WithoutHeader")]
    with_header = inherits("tests/with.cpp", "WithHeader", "Test")
    without = inherits("tests/without.cpp", "WithoutHeader", "Test")
    imports = [file_import("tests/with.cpp", "module:gtest/gtest.h"), file_import("tests/without.cpp", "module:vector")]

    out = resolved(nodes, [with_header, without, *imports])

    assert out[with_header.edge_id].metadata["external"] == "gtest"
    assert "external" not in out[without.edge_id].metadata


def test_a_qml_root_without_a_repository_component_is_a_builtin_or_an_imported_type():
    nodes = [cls("qml/A.qml", "A"), cls("qml/B.qml", "B"), cls("qml/C.qml", "C"), cls("qml/D.qml", "D")]
    builtin = inherits("qml/A.qml", "A", "Item", target="module:Item")
    aliased = inherits("qml/B.qml", "B", "TextField", target="module:Controls.TextField")
    vendor = inherits("qml/C.qml", "C", "VendorPanel", target="module:VendorPanel")
    local_only = inherits("qml/D.qml", "D", "Nowhere", target="module:Nowhere")
    imports = [
        file_import("qml/C.qml", "module:QtQuick"),
        file_import("qml/D.qml", "module:components"),  # `import "components"`: a repository directory
    ]
    nodes += [file_node("qml/components/Row.qml"), file_node("qml/C.qml"), file_node("qml/D.qml")]
    nodes.append(cls("qml/components/Row.qml", "Row"))

    out = resolved(nodes, [builtin, aliased, vendor, local_only, *imports])

    assert out[builtin.edge_id].metadata["external"] == "qml_builtin" and out[builtin.edge_id].target == "module:Item"
    assert out[aliased.edge_id].metadata["external"] == "qml_import"
    assert out[vendor.edge_id].metadata["external"] == "qml_import"
    assert "external" not in out[local_only.edge_id].metadata  # a repository directory is not an outside module


def test_a_repository_class_wins_over_every_external_rule():
    nodes = [
        cls("qml/View.qml", "View"), cls("qml/Item.qml", "Item"),
        cls("src/d.h", "Derived"), cls("src/q.h", "QWidgetBase"),
    ]
    qml = inherits("qml/View.qml", "View", "Item", target="module:Item")
    qt_shaped = inherits("src/d.h", "Derived", "QWidgetBase")

    out = resolved(nodes, [qml, qt_shaped])

    assert out[qml.edge_id].target == "symbol:qml/Item.qml:Item" and "external" not in out[qml.edge_id].metadata
    assert out[qt_shaped.edge_id].target == "symbol:src/q.h:QWidgetBase"
    assert "external" not in out[qt_shaped.edge_id].metadata


def test_an_ambiguous_base_is_not_external_and_other_languages_are_left_alone():
    nodes = [cls("src/d.h", "Derived"), cls("a/q.h", "QThing"), cls("b/q.h", "QThing"), cls("py/m.py", "Model")]
    ambiguous = inherits("src/d.h", "Derived", "QThing")
    python = inherits("py/m.py", "Model", "QObject")

    out = resolved(nodes, [ambiguous, python])

    assert out[ambiguous.edge_id].metadata["ambiguous"] is True and "external" not in out[ambiguous.edge_id].metadata
    assert "external" not in out[python.edge_id].metadata


def test_the_external_mark_is_cleared_when_the_base_appears_and_back_when_it_goes():
    nodes = [cls("src/w.h", "Worker")]
    edge = inherits("src/w.h", "Worker", "QObject")
    assert resolved(nodes, [edge])[edge.edge_id].metadata["external"] == "qt"

    with_base = [*nodes, cls("src/fake.h", "QObject")]
    out = resolved(with_base, [edge], changed_labels={"QObject"})[edge.edge_id]
    assert out.target == "symbol:src/fake.h:QObject" and "external" not in out.metadata

    back = resolved(nodes, [edge], changed_labels={"QObject"})[edge.edge_id]
    assert back.target == "name:QObject" and back.metadata["external"] == "qt"


def test_classify_base_returns_the_rule_or_none():
    from cortex.externals import EXTERNAL_PREFIXES, QML_BUILTINS, QT_CLASS_RE, classify_base

    assert QT_CLASS_RE.match("QAbstractListModel") and not QT_CLASS_RE.match("Quality") and not QT_CLASS_RE.match("Qt") and not QT_CLASS_RE.match("Foo")
    assert isinstance(QML_BUILTINS, frozenset) and {"Item", "QtObject", "ColumnLayout"} <= QML_BUILTINS
    assert "std::" in EXTERNAL_PREFIXES
    assert classify_base("QThread", "QThread", [], "c") == "qt"
    assert classify_base("Foo", "Foo", [], "c") is None
    assert classify_base("Test", "Test", ["gtest/gtest.h"], "c") == "gtest"
    assert classify_base("Test", "Test", ["gmock/gmock.h"], "c") == "gtest"
    assert classify_base("Test", "Test", ["vector"], "c") is None
    assert classify_base("Test", "testing::Test", [], "c") == "prefix"
    assert classify_base("vector", "std::vector<int>", [], "c") == "std"
    assert classify_base("Item", "Item", [], ".qml") == "qml_builtin"
    assert classify_base("Foo", "Foo", [], ".qml") is None
    assert classify_base("Foo", "Foo", ["QtQuick"], ".qml") == "qml_import"
    assert classify_base("Foo", "Lib.Foo", [], ".qml") == "qml_import"
    assert classify_base("Item", "QtQuick.Item", [], ".qml") == "prefix"
    assert classify_base("QObject", "QObject", [], ".py") is None  # only the C and QML families are classified
    assert classify_base("QObject", "QObject", [], ".qml") is None  # a Qt C++ class is not a QML type


# --- through the real extractors ---------------------------------------------------------------------------


import os  # noqa: E402

import pytest  # noqa: E402

needs_parsers = pytest.mark.skipif(os.environ.get("CORTEX_FORCE_REGEX") == "1", reason="needs the full parsers")


def _inherit_edges(repo):
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    return {e.source: e for e in edges if e.relation == "inherits"}


@needs_parsers
def test_cpp_bases_as_the_extractor_writes_them_are_classified(tmp_path):
    repo = make_repo(tmp_path / "repo", {
        "src/worker.h": "#include <QObject>\nclass Worker : public QObject\n{\n    Q_OBJECT\n};\n",
        "src/slow.h": "#include <QThread>\nclass Slow : public QThread {};\n",
        "tests/t.cpp": "#include <gtest/gtest.h>\nclass Fixture : public ::testing::Test {};\nclass Plain : public Test {};\n",
        "tests/u.cpp": "class Lonely : public Test {};\n",
        "src/own.h": "class Own {};\nclass Child : public Own {};\n",
    })

    edges = _inherit_edges(repo)

    assert edges["symbol:src/worker.h:Worker"].metadata["external"] == "qt"
    assert edges["symbol:src/slow.h:Slow"].metadata["external"] == "qt"
    assert edges["symbol:tests/t.cpp:Fixture"].metadata["external"] == "prefix"
    assert edges["symbol:tests/t.cpp:Plain"].metadata["external"] == "gtest"
    assert "external" not in edges["symbol:tests/u.cpp:Lonely"].metadata
    child = edges["symbol:src/own.h:Child"]
    assert child.target == "symbol:src/own.h:Own" and "external" not in child.metadata


@needs_parsers
def test_a_qml_root_item_is_a_builtin_through_the_real_extractor_and_a_repository_item_replaces_it(tmp_path, monkeypatch):
    qml = {
        "qml/Main.qml": "import QtQuick 2.15\nItem {\n    id: root\n}\n",
        "qml/Panel.qml": "import QtQuick 2.15\nimport QtQuick.Controls 2.15 as Controls\nControls.Pane {}\n",
        "qml/Vendor.qml": "import QtQuick 2.15\nimport com.vendor.ctl 1.0\nVendorButton {}\n",
        "qml/Local.qml": 'import "components"\nGizmo {}\n',
        "qml/components/Row.qml": "import QtQuick 2.15\nItem {}\n",
    }
    repo = make_repo(tmp_path / "repo", qml)

    edges = _inherit_edges(repo)

    assert edges["symbol:qml/Main.qml:Main"].target == "module:Item"
    assert edges["symbol:qml/Main.qml:Main"].metadata["external"] == "qml_builtin"
    assert edges["symbol:qml/Panel.qml:Panel"].metadata["external"] == "qml_import"
    assert edges["symbol:qml/Vendor.qml:Vendor"].metadata["external"] == "qml_import"
    assert "external" not in edges["symbol:qml/Local.qml:Local"].metadata

    (repo / "qml/Item.qml").write_text("import QtQuick 2.15\nQtObject {}\n")
    _commit(repo, "a repository Item")
    ingest_repository(repo)  # incremental

    incremental = _inherit_edges(repo)
    main = incremental["symbol:qml/Main.qml:Main"]
    assert main.target == "symbol:qml/Item.qml:Item" and "external" not in main.metadata
    assert incremental["symbol:qml/Panel.qml:Panel"].metadata["external"] == "qml_import"


@needs_parsers
def test_adding_a_repository_class_with_an_external_label_drops_the_mark_incrementally(tmp_path, monkeypatch):
    """The old fake-node case: a repository class named like the Qt base re-resolves the edge."""
    repo = make_repo(tmp_path / "repo", {
        "src/worker.h": "class Worker : public QObject\n{\n};\n",
        "src/other.h": "class Other : public QObject\n{\n};\n",
    })
    assert _inherit_edges(repo)["symbol:src/worker.h:Worker"].metadata["external"] == "qt"

    (repo / "src/qobject.h").write_text("class QObject {};\n")
    _commit(repo, "add QObject")
    ingest_repository(repo)  # incremental
    state = {s: (e.target, e.metadata.get("external")) for s, e in _inherit_edges(repo).items()}
    assert state["symbol:src/worker.h:Worker"] == ("symbol:src/qobject.h:QObject", None)
    assert state["symbol:src/other.h:Other"] == ("symbol:src/qobject.h:QObject", None)
    assert state == {s: (e.target, e.metadata.get("external")) for s, e in _fresh_inherit_edges(repo, tmp_path, monkeypatch, "f1").items()}

    (repo / "src/qobject.h").unlink()
    _commit(repo, "remove QObject")
    ingest_repository(repo)
    back = {s: (e.target, e.metadata.get("external")) for s, e in _inherit_edges(repo).items()}
    assert back["symbol:src/worker.h:Worker"] == ("name:QObject", "qt")
    assert back == {s: (e.target, e.metadata.get("external")) for s, e in _fresh_inherit_edges(repo, tmp_path, monkeypatch, "f2").items()}


def _fresh_inherit_edges(repo, tmp_path, monkeypatch, name):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / name))
    ingest_repository(repo, incremental=False)
    return _inherit_edges(repo)


# --- locality step 4: the nearest directory, not only the same one (plan 0.14, Task 4.2) ----------------

def test_a_mock_base_one_directory_above_its_user_beats_the_production_base():
    """Review 0.13.0 section D: the caller is a test header, so the production-only step does not apply, and the
    mock base sits in the parent directory of the caller."""
    nodes = [
        cls("tests/header-mocks/steps/stepa.h", "StepA"),
        cls("tests/header-mocks/stepbase.h", "StepBase"),
        cls("src/stepbase.h", "StepBase"),
    ]
    edge = inherits("tests/header-mocks/steps/stepa.h", "StepA", "StepBase")

    out = resolved(nodes, [edge])[edge.edge_id]

    assert out.target == "symbol:tests/header-mocks/stepbase.h:StepBase"
    assert out.metadata["resolution"] == "preferred"
    assert "ambiguous" not in out.metadata


def test_the_nearest_directory_is_the_longest_common_prefix_and_a_tie_changes_nothing():
    from cortex.callresolve import _Index

    def pick(paths, caller):
        index = _Index([cls(p, "B") for p in paths], [])
        return [index.symbols[c].source_ref for c in index.locality([f"symbol:{p}:B" for p in paths], caller)]

    assert pick(["a/b/x.h", "a/y.h", "c/z.h"], "a/b/c/d.h") == ["a/b/x.h"]  # prefix 2 beats 1 and 0
    assert pick(["a/b/x.h", "a/c/x.h"], "a/d/e.h") == ["a/b/x.h", "a/c/x.h"]  # same prefix length: unchanged
    assert pick(["a/x.h", "b/x.h"], "c/d.h") == ["a/x.h", "b/x.h"]  # no common prefix at all
    assert pick(["a/b/x.h", "a/x.h"], "a/b/d.h") == ["a/b/x.h"]  # the caller's own directory is still the closest


def test_a_mock_base_above_its_user_resolves_through_the_real_extractor_with_an_angle_include(tmp_path):
    base = "class StepBase {\npublic:\n    virtual void run();\n};\n"
    files = {
        "src/stepbase.h": base,
        "tests/header-mocks/stepbase.h": base,
        "tests/header-mocks/steps/stepa.h": "#include <stepbase.h>\nclass StepA : public StepBase {\npublic:\n    void run() override;\n};\n",
        "tests/header-mocks/steps/stepb.h": "class StepB : public StepBase {\npublic:\n    void run() override;\n};\n",
    }
    repo = make_repo(tmp_path / "repo", files)

    targets = _inherit_targets(repo)

    assert targets["symbol:tests/header-mocks/steps/stepa.h:StepA"][0] == "symbol:tests/header-mocks/stepbase.h:StepBase"
    assert targets["symbol:tests/header-mocks/steps/stepb.h:StepB"][0] == "symbol:tests/header-mocks/stepbase.h:StepBase"
