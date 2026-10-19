"""`cortex_references` graph tier for resolved `calls` edges: a read *at the call site*."""

from __future__ import annotations

from cortex.models import GraphEdge, GraphNode
from cortex.references import _graph_hits_from_edges


def _node(path: str, label: str, line: int) -> GraphNode:
    return GraphNode(f"symbol:{path}:{label}", "func", label, path, "symbol", "", line, line + 2, {})


def _edge(relation: str, source: str, target: str, **metadata) -> GraphEdge:
    return GraphEdge(f"e:{relation}:{source}:{target}", source, target, relation, "STRUCTURAL", "EXTRACTED", 1.0, metadata)


def test_resolved_call_is_a_read_at_the_caller_line_and_definition_stays_a_definition() -> None:
    callee, caller = _node("lib/repo.ts", "save", 2), _node("app.ts", "start", 10)
    edges = [
        _edge("calls", caller.node_id, callee.node_id, lineno=18, source_file="app.ts"),
        _edge("calls", caller.node_id, callee.node_id, lineno=37, source_file="app.ts"),
        _edge("contains", "file:lib/repo.ts", callee.node_id, lineno=2, source_file="lib/repo.ts"),
    ]
    hits, covered = _graph_hits_from_edges(edges, {n.node_id: n for n in (callee, caller)}, "save")
    assert [(h["text"], h["access"]) for h in hits] == [
        ("app.ts:18", "read"), ("app.ts:37", "read"), ("lib/repo.ts:2", "definition"),
    ]
    assert covered == {("app.ts", 18), ("app.ts", 37), ("lib/repo.ts", 2)}


def test_a_call_without_site_metadata_and_other_relations_keep_the_previous_behaviour() -> None:
    callee = _node("lib/repo.ts", "save", 2)
    legacy = _edge("calls", "symbol:app.ts:start", callee.node_id)  # indexed before the call-site contract
    hits, _ = _graph_hits_from_edges([legacy], {callee.node_id: callee}, "save")
    assert [(h["text"], h["access"]) for h in hits] == [("lib/repo.ts:2", "read")]
    ref = _edge("references", "symbol:app.ts:start", callee.node_id, lineno=5, source_file="app.ts")
    hits, _ = _graph_hits_from_edges([ref], {callee.node_id: callee}, "save")
    assert [(h["text"], h["access"]) for h in hits] == [("lib/repo.ts:2", "write")]


def test_unresolved_name_targets_add_no_graph_hit() -> None:
    caller = _node("app.ts", "start", 10)
    edge = _edge("calls", caller.node_id, "name:save", lineno=18, source_file="app.ts")
    assert _graph_hits_from_edges([edge], {caller.node_id: caller}, "save")[0] == []
