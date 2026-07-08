# tests/test_ast_extract.py
from __future__ import annotations
from cortex.ast_extract import extract_python_edges


SAMPLE = '''\
import os
from pathlib import Path
from .models import GraphNode, GraphEdge

class Extractor:
    def parse(self, text: str) -> int:
        return len(text)

def run(path: str) -> None:
    result = Extractor()
'''


def test_extracts_import_edges():
    nodes, edges = extract_python_edges("src/extractor.py", SAMPLE, known_paths={"src/models.py"})
    relations = [e.relation for e in edges]
    assert "imports" in relations


def test_intra_project_import_resolves_to_file_node():
    nodes, edges = extract_python_edges("src/cortex/extractor.py", SAMPLE, known_paths={"src/cortex/models.py"})
    targets = [e.target for e in edges]
    assert "file:src/cortex/models.py" in targets


def test_absolute_local_import_resolves_to_file_node():
    nodes, edges = extract_python_edges("src/extractor.py", "import cortex.models\n", known_paths={"cortex/models.py"})
    targets = [e.target for e in edges]
    assert "file:cortex/models.py" in targets


def test_absolute_local_from_import_resolves_to_file_node():
    nodes, edges = extract_python_edges(
        "src/extractor.py", "from cortex.models import GraphNode\n", known_paths={"cortex/models.py"}
    )
    targets = [e.target for e in edges]
    assert "file:cortex/models.py" in targets


def test_extracts_class_node():
    nodes, edges = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    node_kinds = [n.kind for n in nodes]
    assert "class" in node_kinds


def test_extracts_func_node():
    nodes, edges = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    node_kinds = [n.kind for n in nodes]
    assert "func" in node_kinds


def test_all_structural_edges_are_extracted():
    nodes, edges = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    for edge in edges:
        assert edge.layer == "STRUCTURAL"
        assert edge.confidence == "EXTRACTED"
        assert edge.weight == 1.0


def test_syntax_error_returns_empty():
    nodes, edges = extract_python_edges("bad.py", "def (broken:", known_paths=set())
    assert nodes == []
    assert edges == []


def test_symbol_node_ids_use_qualnames():
    nodes, _ = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    ids = {n.node_id for n in nodes}
    assert "symbol:src/extractor.py:Extractor" in ids
    assert "symbol:src/extractor.py:Extractor.parse" in ids
    assert "symbol:src/extractor.py:run" in ids


def test_symbol_nodes_carry_signature_span_granularity():
    nodes, _ = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    by_id = {n.node_id: n for n in nodes}
    run_node = by_id["symbol:src/extractor.py:run"]
    assert run_node.signature == "def run(path: str) -> None:"
    assert run_node.granularity == "symbol"
    assert run_node.span_start == 9
    assert run_node.span_end == 10
    cls_node = by_id["symbol:src/extractor.py:Extractor"]
    assert cls_node.signature == "class Extractor:"


def test_nested_symbol_contained_by_parent_symbol():
    _, edges = extract_python_edges("src/extractor.py", SAMPLE, known_paths=set())
    contains = {(e.source, e.target) for e in edges if e.relation == "contains"}
    assert ("file:src/extractor.py", "symbol:src/extractor.py:Extractor") in contains
    assert ("symbol:src/extractor.py:Extractor", "symbol:src/extractor.py:Extractor.parse") in contains


CALLS_SAMPLE = '''\
def helper(x):
    return x + 1


def run(value):
    total = helper(value)
    return len(str(total))


def outer():
    def inner():
        return helper(1)
    return inner
'''


def test_extracts_call_edges_name_based():
    _, edges = extract_python_edges("src/calls.py", CALLS_SAMPLE, known_paths=set())
    calls = {(e.source, e.target) for e in edges if e.relation == "calls"}
    assert ("symbol:src/calls.py:run", "name:helper") in calls
    assert ("symbol:src/calls.py:run", "name:len") in calls
    assert ("symbol:src/calls.py:run", "name:str") in calls


def test_call_edges_attributed_to_enclosing_scope_not_parent():
    # helper() is called inside `inner`, so the edge is owned by inner, not outer.
    _, edges = extract_python_edges("src/calls.py", CALLS_SAMPLE, known_paths=set())
    calls = {(e.source, e.target) for e in edges if e.relation == "calls"}
    assert ("symbol:src/calls.py:outer.inner", "name:helper") in calls
    assert ("symbol:src/calls.py:outer", "name:helper") not in calls


def test_call_edges_are_structural_extracted():
    _, edges = extract_python_edges("src/calls.py", CALLS_SAMPLE, known_paths=set())
    call_edges = [e for e in edges if e.relation == "calls"]
    assert call_edges
    for edge in call_edges:
        assert edge.layer == "STRUCTURAL"
        assert edge.confidence == "EXTRACTED"
        assert edge.weight == 1.0
