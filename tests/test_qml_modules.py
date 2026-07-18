from __future__ import annotations

from cortex.structural.qml_modules import parse_cmake_qml_metadata, parse_qmldir, parse_qmltypes


def test_qmldir_exports_singletons_and_internal_types():
    nodes, edges = parse_qmldir("qmldir", "module Demo.Controls\nsingleton Theme 1.0 Theme.qml\ninternal Hidden 1.0 Hidden.qml\n")
    assert any(node.label == "Theme" and node.metadata.get("singleton") and node.span_start == 2 for node in nodes)
    assert any(node.label == "Hidden" and node.metadata.get("internal") for node in nodes)
    assert any(edge.relation == "exports" for edge in edges)


def test_qmltypes_components_and_members():
    nodes, edges = parse_qmltypes("types.qmltypes", 'Module { Component { name: "Card" Property { name: "value" type: "int" } Method { name: "update" } } }')
    assert {node.label for node in nodes} >= {"Card", "value", "update"}
    assert any(edge.relation == "contains" for edge in edges)


def test_cmake_qml_module_balanced_metadata():
    nodes, edges = parse_cmake_qml_metadata("CMakeLists.txt", "qt_add_qml_module(app URI Demo.Controls VERSION 1.0 QML_FILES qml/Main.qml IMPORTS QtQuick)", {"qml/Main.qml"})
    assert nodes[0].label == "app"
    assert any(edge.relation == "registers" and edge.target == "file:qml/Main.qml" for edge in edges)
    assert any(edge.target == "module:Demo.Controls" for edge in edges)


def test_cmake_non_qml_commands_do_not_create_qml_targets():
    nodes, edges = parse_cmake_qml_metadata(
        "CMakeLists.txt",
        "target_sources(app PRIVATE qml/Main.qml)\nadd_executable(app qml/Main.qml)",
        {"qml/Main.qml"},
    )
    assert nodes == []
    assert edges == []


def test_qt_target_qml_sources_registers_qml_files():
    _nodes, edges = parse_cmake_qml_metadata(
        "qml/CMakeLists.txt",
        "qt_target_qml_sources(app QML_FILES Main.qml)",
        {"qml/Main.qml"},
    )
    assert any(edge.relation == "registers" and edge.target == "file:qml/Main.qml" for edge in edges)
