from __future__ import annotations

import os
import subprocess

import pytest

from cortex.ingest import ingest_repository
from cortex.models import GraphEdge, GraphNode
from cortex.structural import extract_structural_edges
from cortex.store import CortexStore
from cortex.structural.qml_modules import parse_qmldir
from cortex.structural.qml_resolver import build_qml_symbol_index, resolve_qml_edges

pytestmark = pytest.mark.skipif(
    os.environ.get("CORTEX_FORCE_REGEX") == "1",
    reason="QML component resolution requires full-parser declarations",
)


def test_local_component_instantiation_resolves_without_basename_guess():
    known = {"Main.qml", "Card.qml"}
    result = extract_structural_edges("Main.qml", "Item { Card { id: card } }", known)
    resolve_qml_edges(result.nodes, result.edges, known, build_qml_symbol_index(result.nodes))
    assert any(edge.relation == "instantiates" and edge.target == "file:Card.qml" for edge in result.edges)


def test_ambiguous_duplicate_components_remain_unverified():
    known = {"Main.qml", "one/Card.qml", "two/Card.qml"}
    result = extract_structural_edges("Main.qml", "Item { Card {} }", known)
    instantiates = [edge for edge in result.edges if edge.relation == "instantiates"]
    assert instantiates
    assert all(edge.target != "file:one/Card.qml" and edge.target != "file:two/Card.qml" for edge in instantiates)


def test_imported_module_disambiguates_duplicate_component_names():
    known = {"Main.qml", "one/qmldir", "one/Card.qml", "two/qmldir", "two/Card.qml"}
    result = extract_structural_edges("Main.qml", "import One 1.0\nItem { Card {} }", known)
    one_nodes, one_edges = parse_qmldir("one/qmldir", "module One\nCard 1.0 Card.qml\n")
    two_nodes, two_edges = parse_qmldir("two/qmldir", "module Two\nCard 1.0 Card.qml\n")
    nodes = [*result.nodes, *one_nodes, *two_nodes]
    edges = [*result.edges, *one_edges, *two_edges]

    resolve_qml_edges(nodes, edges, known)

    instantiates = [edge for edge in result.edges if edge.relation == "instantiates" and edge.metadata.get("type_name") == "Card"]
    assert len(instantiates) == 1
    assert instantiates[0].target == "file:one/Card.qml"
    assert "unverified" not in instantiates[0].metadata


def test_cpp_registered_type_resolves_qml_instantiation():
    known = {"Main.qml", "registrations.cpp"}
    qml = extract_structural_edges("Main.qml", "Item { Widget {} }", known)
    cpp = extract_structural_edges(
        "registrations.cpp",
        'class Widget {}; void registerTypes() { qmlRegisterType<Widget>("Demo", 1, 0, "Widget"); }',
        known,
    )
    nodes = [*qml.nodes, *cpp.nodes]
    edges = [*qml.edges, *cpp.edges]

    resolve_qml_edges(nodes, edges, known)

    instantiates = [edge for edge in qml.edges if edge.relation == "instantiates" and edge.metadata.get("type_name") == "Widget"]
    assert len(instantiates) == 1
    assert instantiates[0].target == "symbol:registrations.cpp:Widget"


def test_non_qml_class_does_not_resolve_qml_component():
    result = extract_structural_edges("Main.qml", "Item { Card {} }", {"Main.qml"})
    foreign = GraphNode("symbol:Card.java:Card", "class", "Card", "Card.java", "symbol")
    nodes = [*result.nodes, foreign]

    resolve_qml_edges(nodes, result.edges, {"Main.qml"})

    instantiates = [edge for edge in result.edges if edge.relation == "instantiates" and edge.metadata.get("type_name") == "Card"]
    assert instantiates
    assert all(edge.target == "module:Card" and edge.metadata.get("unverified") for edge in instantiates)


def test_incremental_qml_signal_deletion_matches_clean_ingest(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "Device.qml").write_text("Item { signal value() }\n", encoding="utf-8")
    (repo / "Main.qml").write_text("Item { Device { onValueChanged: console.log('changed') } }\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    incremental_db = tmp_path / "incremental.db"
    clean_db = tmp_path / "clean.db"
    ingest_repository(repo, db_path=incremental_db)

    (repo / "Device.qml").write_text("Item { signal other() }\n", encoding="utf-8")
    ingest_repository(repo, db_path=incremental_db, incremental=True)
    ingest_repository(repo, db_path=clean_db)

    def handles(db_path):
        _nodes, edges = CortexStore(db_path).fetch_graph(repo)
        return sorted(
            (edge.source, edge.target, edge.relation, bool(edge.metadata.get("unverified")))
            for edge in edges if edge.relation == "handles"
        )

    assert handles(incremental_db) == handles(clean_db)
    assert handles(clean_db) == [
        ("symbol:Main.qml:Main.Device.onValueChanged", "module:valueChanged", "handles", True)
    ]


def test_deleted_signal_target_is_downgraded_during_incremental_resolution():
    handler_id = "symbol:Main.qml:Main.Device.onValueChanged"
    handler = GraphNode(
        handler_id,
        "func",
        "onValueChanged",
        "Main.qml",
        "symbol",
        metadata={"qml_kind": "handler", "qml_owner": "Main.Device"},
    )
    edge = GraphEdge(
        "edge:handler",
        handler_id,
        "symbol:Device.qml:Device.value",
        "handles",
        metadata={"source_file": "Main.qml", "signal_name": "valueChanged", "component_path": "Device.qml"},
    )

    resolve_qml_edges([handler], [edge], {"Main.qml"})

    assert edge.target == "module:valueChanged"
    assert edge.metadata["unverified"] is True
