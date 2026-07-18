from __future__ import annotations

import os
from pathlib import Path

import pytest

from cortex.structural import extract_structural_edges


FIXTURE = Path(__file__).parent / "fixtures" / "qml_full_repo"
pytestmark = pytest.mark.skipif(
    os.environ.get("CORTEX_FORCE_REGEX") == "1",
    reason="full QML declaration assertions require the managed parser",
)


def test_qml_declarations_and_relations_are_structural():
    path = "Main.qml"
    content = (FIXTURE / path).read_text(encoding="utf-8")
    result = extract_structural_edges(path, content, {p.name for p in FIXTURE.iterdir()})
    assert result.backend in {"treesitter", "regex"}
    kinds = {node.metadata.get("qml_kind") for node in result.nodes}
    assert {"component", "property", "id", "signal"} <= kinds
    assert any(node.metadata.get("qml_kind") == "inline_component" for node in result.nodes)
    assert any(edge.relation == "binds" for edge in result.edges)
    assert any(edge.relation == "handles" for edge in result.edges)


def test_qml_duplicate_names_keep_owner_qualified_ids():
    content = "Item { property int value: 1; Rectangle { property int value: 2 } }"
    result = extract_structural_edges("Duplicate.qml", content, {"Duplicate.qml"})
    properties = [node for node in result.nodes if node.metadata.get("qml_kind") == "property"]
    assert len(properties) == 2
    assert len({node.node_id for node in properties}) == 2
    assert all("." in node.node_id.rsplit(":", 1)[-1] for node in properties)


def test_function_parameters_are_local_and_owner_qualified():
    content = "Item { property int value: 1; function read(value) { return value } function other() { return value } }"
    result = extract_structural_edges("Scope.qml", content, {"Scope.qml"})
    parameter_id = "symbol:Scope.qml:Scope.read.value"
    property_id = "symbol:Scope.qml:Scope.value"

    assert parameter_id in {node.node_id for node in result.nodes}
    read_targets = {(edge.source, edge.target) for edge in result.edges if edge.relation == "reads"}
    assert ("symbol:Scope.qml:Scope.read", parameter_id) in read_targets
    assert ("symbol:Scope.qml:Scope.other", property_id) in read_targets


def test_assignment_targets_are_writes_not_reads():
    content = "Item { property int value: 1; function setValue() { value = 2 } }"
    result = extract_structural_edges("Writes.qml", content, {"Writes.qml"})
    source = "symbol:Writes.qml:Writes.setValue"
    target = "symbol:Writes.qml:Writes.value"

    assert any(edge.source == source and edge.target == target and edge.relation == "writes" for edge in result.edges)
    assert not any(edge.source == source and edge.target == target and edge.relation == "reads" for edge in result.edges)


def test_cross_sibling_id_reference_resolves_unique_component_id():
    content = "Item { Rectangle { id: first; property color tone: 'red' } Rectangle { property color copy: first.tone } }"
    result = extract_structural_edges("Ids.qml", content, {"Ids.qml"})

    assert any(
        edge.relation == "reads" and edge.target == "symbol:Ids.qml:Ids.Rectangle.tone"
        for edge in result.edges
    )


def test_external_placeholders_and_handlers_do_not_collide():
    content = "Item { MouseArea { onClicked: missing.call() } Rectangle { onClicked: missing.call() } }"
    result = extract_structural_edges("Owners.qml", content, {"Owners.qml"})
    handlers = [node for node in result.nodes if node.metadata.get("qml_kind") == "handler"]

    assert {node.node_id for node in handlers} == {
        "symbol:Owners.qml:Owners.MouseArea.onClicked",
        "symbol:Owners.qml:Owners.Rectangle.onClicked",
    }
    assert all(node.metadata.get("compat_alias") is not True for node in handlers)
    assert any(node.node_id.startswith("external:qml:Owners.qml:") for node in result.nodes)
