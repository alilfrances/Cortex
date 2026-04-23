# tests/test_ast_extract.py
from __future__ import annotations
from cortex.ast_extract import extract_python_edges


SAMPLE = '''\
import os
from pathlib import Path
from .models import GraphNode, GraphEdge

class Extractor:
    pass

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
