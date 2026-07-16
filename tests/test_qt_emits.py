from cortex.structural.regex_backend import extract_regex_edges


def _emits(edges):
    return [edge for edge in edges if edge.relation == "emits"]


def test_emit_inside_method_is_anchored_to_function_symbol():
    content = "void Controller::run() {\n    emit started();\n}\n"

    nodes, edges = extract_regex_edges("controller.cpp", content, set())

    assert "symbol:controller.cpp:run" in {node.node_id for node in nodes}
    assert _emits(edges)[0].source == "symbol:controller.cpp:run"


def test_file_scope_emit_stays_anchored_to_file():
    _nodes, edges = extract_regex_edges("controller.cpp", "emit started();\n", set())

    assert _emits(edges)[0].source == "file:controller.cpp"


def test_qml_handler_without_matching_signal_is_unverified():
    _nodes, edges = extract_regex_edges(
        "Button.qml",
        "Button {\n    onMissingChanged: console.log(\"missing\")\n}\n",
        set(),
    )

    handles = [edge for edge in edges if edge.relation == "handles"]
    assert handles[0].metadata["unverified"] is True
