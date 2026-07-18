from __future__ import annotations

import pytest

from cortex.graph import build_graph
from cortex.models import SourceRecord
from cortex.structural.regex_backend import extract_regex_edges

try:
    import tree_sitter_cpp  # noqa: F401
    _HAS_TS_CPP = True
except Exception:
    _HAS_TS_CPP = False


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(
        path=path,
        content=content,
        kind="code",
        size_bytes=len(content),
        modified_at=0.0,
        content_hash="",
    )


def _connects(edges):
    return [edge for edge in edges if edge.relation == "connects"]


def test_multiline_pointer_connect_is_parsed():
    content = (
        "void setup() {\n"
        "  connect(\n"
        "    sender,\n"
        "    &Alpha::changed,\n"
        "    receiver,\n"
        "    &Beta::onChanged);\n"
        "}\n"
    )
    _nodes, edges = extract_regex_edges("wiring.cpp", content, set())
    connects = _connects(edges)

    assert len(connects) == 1
    edge = connects[0]
    assert edge.source == "name:Alpha::changed"
    assert edge.target == "name:Beta::onChanged"
    assert edge.metadata["lineno"] == 2
    assert edge.metadata["sender"] == "sender"
    assert edge.metadata["receiver"] == "receiver"
    assert edge.metadata["sender_class"] == "Alpha"
    assert edge.metadata["receiver_class"] == "Beta"


def test_same_member_name_across_classes_is_not_a_self_loop():
    content = "void setup() {\n  connect(a, &Alpha::refresh, b, &Beta::refresh);\n}\n"
    _nodes, edges = extract_regex_edges("wiring.cpp", content, set())
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "name:Alpha::refresh"
    assert connects[0].target == "name:Beta::refresh"
    assert connects[0].source != connects[0].target


def test_same_class_same_member_self_loop_is_dropped():
    content = "void setup() {\n  connect(a, &Alpha::refresh, b, &Alpha::refresh);\n}\n"
    _nodes, edges = extract_regex_edges("wiring.cpp", content, set())
    assert _connects(edges) == []


def test_build_graph_resolves_connect_endpoint_to_defining_symbol():
    content = (
        "class Beta : public QObject {\n"
        "    Q_OBJECT\n"
        "public slots:\n"
        "    void onChanged();\n"
        "};\n"
        "\n"
        "void setup(Alpha *a, Beta *b) {\n"
        "    connect(a, &Alpha::changed, b, &Beta::onChanged);\n"
        "}\n"
    )
    nodes, edges = build_graph([_source("beta.cpp", content)], [])
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].target == "symbol:beta.cpp:onChanged"
    # Alpha is not defined anywhere, so the sender keeps its class-qualified form.
    assert connects[0].source == "name:Alpha::changed"


def test_build_graph_resolves_qualified_members_across_header_and_cpp_files(monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    header = "class Cls {\nsignals:\n    void thing();\n};\n"
    implementation = (
        "void Cls::slot() {\n}\n"
        "void wire(Cls *sender, Cls *receiver) {\n"
        "    connect(sender, &Cls::thing, receiver, &Cls::slot);\n"
        "}\n"
    )

    nodes, edges = build_graph(
        [_source("Cls.h", header), _source("Cls.cpp", implementation)],
        [],
    )
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "symbol:Cls.h:thing"
    assert connects[0].target == "symbol:Cls.cpp:slot"


@pytest.mark.skipif(not _HAS_TS_CPP, reason="tree-sitter C++ grammar unavailable")
def test_treesitter_cpp_qualified_member_metadata():
    from cortex.structural.treesitter_backend import extract_treesitter_edges

    nodes, _edges = extract_treesitter_edges(
        "members.cpp",
        "void Cls::slot() {}\nvoid A::B::member() {}\nvoid free_fn() {}\n",
        set(),
    )
    by_id = {node.node_id: node for node in nodes}

    assert by_id["symbol:members.cpp:slot"].label == "slot"
    assert by_id["symbol:members.cpp:slot"].metadata["qualifier"] == "Cls"
    assert by_id["symbol:members.cpp:member"].label == "member"
    assert by_id["symbol:members.cpp:member"].metadata["qualifier"] == "B"
    assert "qualifier" not in by_id["symbol:members.cpp:free_fn"].metadata


@pytest.mark.skipif(not _HAS_TS_CPP, reason="tree-sitter C++ grammar unavailable")
def test_build_graph_resolves_qualified_members_with_tree_sitter():
    header = "class Cls {\nsignals:\n    void thing();\n};\n"
    implementation = (
        "void Cls::slot() {}\n"
        "void wire(Cls *sender, Cls *receiver) {\n"
        "    connect(sender, &Cls::thing, receiver, &Cls::slot);\n"
        "}\n"
    )

    nodes, edges = build_graph(
        [_source("Cls.h", header), _source("Cls.cpp", implementation)],
        [],
    )
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "symbol:Cls.h:thing"
    assert connects[0].target == "symbol:Cls.cpp:slot"

    slot = next(node for node in nodes if node.node_id == "symbol:Cls.cpp:slot")
    assert slot.label == "slot"
    assert slot.metadata["qualifier"] == "Cls"


def _declaration_and_definition_sources():
    return [
        _source(
            "include/DeviceModel.hpp",
            "class DeviceModel {\n"
            "signals:\n"
            "    void deviceConnected();\n"
            "public slots:\n"
            "    void onDeviceConnected();\n"
            "};\n",
        ),
        _source("src/DeviceModel.cpp", "void DeviceModel::onDeviceConnected() {}\n"),
        _source(
            "wire.cpp",
            "void wire() {\n"
            "    connect(sender, &DeviceModel::deviceConnected, "
            "receiver, &DeviceModel::onDeviceConnected);\n"
            "}\n",
        ),
    ]


def _assert_declaration_precedes_definition(nodes, edges):
    connects = _connects(edges)
    assert len(connects) == 1
    assert connects[0].target == "symbol:include/DeviceModel.hpp:onDeviceConnected"
    slot = next(node for node in nodes if node.node_id == connects[0].target)
    assert slot.metadata["qt"] == "slot"


@pytest.mark.skipif(not _HAS_TS_CPP, reason="tree-sitter C++ grammar unavailable")
def test_qt_declaration_precedes_tree_sitter_definition():
    nodes, edges = build_graph(_declaration_and_definition_sources(), [])
    _assert_declaration_precedes_definition(nodes, edges)


def test_qt_declaration_precedes_definition_with_regex_fallback(monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)
    nodes, edges = build_graph(_declaration_and_definition_sources(), [])
    _assert_declaration_precedes_definition(nodes, edges)


def test_qualified_member_resolution_stays_unresolved_for_duplicate_declarations(monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)
    sources = [
        _source("first.hpp", "class Cls { public slots: void member(); };\n"),
        _source("second.hpp", "class Cls { public slots: void member(); };\n"),
        _source("wiring.cpp", "void wire() { connect(a, &Cls::member, b, &Other::slot); }\n"),
    ]

    _nodes, edges = build_graph(sources, [])
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "name:Cls::member"


def test_qualified_member_resolution_stays_unresolved_when_class_is_ambiguous(monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    sources = [
        _source("first.cpp", "void Cls::member() {}\n"),
        _source("second.cpp", "void Cls::member() {}\n"),
        _source("wiring.cpp", "void wire() { connect(a, &Cls::member, b, &Other::slot); }\n"),
    ]

    _nodes, edges = build_graph(sources, [])
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "name:Cls::member"


def test_custom_connect_wrapper_names():
    content = "void setup() {\n  safeConnect(a, SIGNAL(x()), b, SLOT(y()));\n}\n"

    _nodes, default_edges = extract_regex_edges("wiring.cpp", content, set())
    assert _connects(default_edges) == []

    _nodes, edges = extract_regex_edges(
        "wiring.cpp", content, set(), connect_names=["connect", "safeConnect"]
    )
    connects = _connects(edges)
    assert len(connects) == 1
    # Unresolved macro-form endpoints keep the module: placeholder so the
    # cross-file Qt resolution pass (graph.py::_resolve_qt_edges) can still
    # resolve them repo-wide via signal_name/slot_name metadata.
    assert connects[0].source == "module:x"
    assert connects[0].target == "module:y"


def test_single_line_signal_slot_macro_form_still_parses():
    content = "void setup() {\n  connect(this, SIGNAL(started(int)), this, SLOT(start()));\n}\n"
    _nodes, edges = extract_regex_edges("wiring.cpp", content, set())
    connects = _connects(edges)

    assert len(connects) == 1
    assert connects[0].source == "module:started"
    assert connects[0].target == "module:start"
    assert connects[0].metadata["sender"] == "this"
    assert connects[0].metadata["receiver"] == "this"
