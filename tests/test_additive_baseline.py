"""Additive-only guard for call extraction (Tasks 6-8).

``tests/goldens/extraction/baseline-7b0e6e0`` is a *frozen* copy of the extraction goldens as
they were before any `calls` edge existed for non-Python/non-QML code (identical to the Task 0
goldens at 1f8ac2b).  It is never regenerated.  Call extraction must be purely additive over
it, for both backends: every frozen node (id, span, signature, metadata) is unchanged, no node
appears or disappears, and the non-`calls` edges are exactly the frozen edge list.  Only `calls`
edges may be new.  Call-specific fixtures (``tests/fixtures/calls_*``) carry the call sites that
the frozen fixtures do not have, so definitions in frozen fixtures are never edited.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.golden_utils import GOLDEN_ROOT, TREE_SITTER_ONLY, canonical_graph
from tests.test_golden_extraction import _layer, _qt_graph, _whole_fixture_layer

BASELINE = GOLDEN_ROOT / "extraction" / "baseline-7b0e6e0"
# annotate_degree counts every edge, so degree-derived node metadata legitimately follows
# new `calls` edges in full build_graph results; nothing else about a node may move.
_DEGREE_KEYS = {"degree"}
# The one metadata key call extraction may *add* to an existing node: C/C++ class nodes whose
# body declares typed fields record `member_types` so the resolver can type `m_x->f()` in an
# out-of-line `Foo::bar()` defined in another file.  Ids, spans, signatures, labels and every
# other metadata key must stay identical.
_ALLOWED_NEW_NODE_KEYS = {"member_types"}


def _frozen(rel: str) -> dict:
    return json.loads((BASELINE / rel).read_text(encoding="utf-8"))


def _strip(nodes: list[dict], drop: set[str]) -> list[dict]:
    out = []
    for node in nodes:
        node = dict(node)
        node["metadata"] = {k: v for k, v in node["metadata"].items() if k not in drop}
        out.append(node)
    return out


def _ruby_method_exception(old: dict, new: dict) -> tuple[set[str], set[str]]:
    """The one approved, narrow exception to node-equality: Ruby `def`/`def self.x` symbols.

    The generic tree-sitter table had no Ruby `method` node type, so frozen Ruby files lack their
    method symbols.  Allowed, and nothing else: new nodes that are ``func`` symbols of a ``.rb``
    file (id scheme unchanged, ``symbol:<path>:<name>``, not previously present) and the
    ``contains`` edge from that same file to each.  Every frozen node and edge must still be
    present unchanged; non-Ruby files get no exemption at all."""
    old_ids = {n["node_id"] for n in old["nodes"]}
    added_nodes = {
        n["node_id"]
        for n in new["nodes"]
        if n["node_id"] not in old_ids
        and n["kind"] == "func"
        and n["source_ref"].endswith(".rb")
        and n["node_id"] == f"symbol:{n['source_ref']}:{n['label']}"
    }
    old_edge_ids = {e["edge_id"] for e in old["edges"]}
    added_edges = {
        e["edge_id"]
        for e in new["edges"]
        if e["edge_id"] not in old_edge_ids
        and e["relation"] == "contains"
        and e["target"] in added_nodes
        and e["source"] == "file:" + e["target"].split(":", 2)[1]
    }
    return added_nodes, added_edges


def _assert_additive(old: dict, new: dict, *, drop: set[str] = frozenset()) -> None:
    ruby_nodes, ruby_edges = _ruby_method_exception(old, new)
    new = {
        "nodes": [n for n in new["nodes"] if n["node_id"] not in ruby_nodes],
        "edges": [e for e in new["edges"] if e["edge_id"] not in ruby_edges],
    }
    ignored = set(drop) | _ALLOWED_NEW_NODE_KEYS
    assert _strip(new["nodes"], ignored) == _strip(old["nodes"], ignored), "call extraction changed nodes"
    for node in old["nodes"]:
        assert "member_types" not in node["metadata"], "frozen baseline never carried member_types"
    # Every frozen edge (QML files already carried `calls` edges) survives unchanged and the
    # only new edges are `calls`; non-call edges are exactly the frozen non-call edges.
    old_edges = {e["edge_id"]: e for e in old["edges"]}
    new_edges = {e["edge_id"]: e for e in new["edges"]}
    assert len(new_edges) == len(new["edges"]), "duplicate edge ids"
    assert {k: v for k, v in new_edges.items() if k in old_edges} == old_edges, "a frozen edge changed or vanished"
    added = [e for k, e in new_edges.items() if k not in old_edges]
    assert all(e["relation"] == "calls" for e in added), "call extraction added a non-call edge"
    assert [e for e in new["edges"] if e["relation"] != "calls"] == [e for e in old["edges"] if e["relation"] != "calls"]


def test_ruby_exception_is_narrow() -> None:
    """The exemption admits exactly Ruby func symbols + their file `contains` edge, nothing else."""
    old = {"nodes": [{"node_id": "symbol:a.rb:A", "kind": "class", "source_ref": "a.rb", "label": "A", "metadata": {}}], "edges": []}

    def node(nid, kind="func", ref="a.rb", label="m"):
        return {"node_id": nid, "kind": kind, "source_ref": ref, "label": label, "metadata": {}}

    def edge(eid, rel, src, dst):
        return {"edge_id": eid, "relation": rel, "source": src, "target": dst, "metadata": {}}

    ok = {"nodes": old["nodes"] + [node("symbol:a.rb:m")], "edges": [edge("c", "contains", "file:a.rb", "symbol:a.rb:m")]}
    _assert_additive(old, ok)
    for bad_node in (
        node("symbol:a.py:m", ref="a.py"),            # not Ruby
        node("symbol:a.rb:m", kind="class"),          # not a func symbol
        node("symbol:a.rb:Other:m"),                  # id scheme changed
    ):
        with pytest.raises(AssertionError):
            _assert_additive(old, {"nodes": old["nodes"] + [bad_node], "edges": []})
    with pytest.raises(AssertionError):                # an extra edge of another kind
        _assert_additive(old, {"nodes": ok["nodes"], "edges": ok["edges"] + [edge("i", "imports", "file:a.rb", "file:b.rb")]})
    with pytest.raises(AssertionError):                # a changed frozen node
        _assert_additive(old, {"nodes": [dict(old["nodes"][0], label="B")] + ok["nodes"][1:], "edges": ok["edges"]})


def _backend_cases(backend: str):
    root = BASELINE / backend
    cases = []
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(root).as_posix()
        fixture, _, name = rel.partition("/")
        if "graph/" in rel:
            continue
        cases.append(pytest.param(fixture, name, id=rel))
    return cases


@TREE_SITTER_ONLY
@pytest.mark.parametrize("fixture,name", _backend_cases("treesitter"))
def test_treesitter_calls_are_additive_over_baseline(fixture: str, name: str) -> None:
    if name == "__whole_fixture__.json":
        nodes, edges = _whole_fixture_layer(fixture)
    else:
        nodes, edges = _layer(fixture, name.removesuffix(".json"))
    _assert_additive(_frozen(f"treesitter/{fixture}/{name}"), canonical_graph(nodes, edges))


@pytest.mark.parametrize("fixture,name", _backend_cases("regex"))
def test_regex_calls_are_additive_over_baseline(fixture: str, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    if name == "__whole_fixture__.json":
        nodes, edges = _whole_fixture_layer(fixture)
    else:
        nodes, edges = _layer(fixture, name.removesuffix(".json"))
    _assert_additive(_frozen(f"regex/{fixture}/{name}"), canonical_graph(nodes, edges))


@TREE_SITTER_ONLY
def test_treesitter_qt_app_graph_is_additive_over_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodes, edges = _qt_graph(tmp_path, monkeypatch)
    _assert_additive(_frozen("graph/treesitter/qt_app.json"), canonical_graph(nodes, edges), drop=_DEGREE_KEYS)


def test_regex_qt_app_graph_is_additive_over_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    nodes, edges = _qt_graph(tmp_path, monkeypatch)
    _assert_additive(_frozen("graph/regex/qt_app.json"), canonical_graph(nodes, edges), drop=_DEGREE_KEYS)


def test_baseline_has_no_code_calls_edges() -> None:
    """The frozen copy really is the pre-call baseline: only QML sources carried `calls`."""
    for path in BASELINE.rglob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for edge in data["edges"]:
            if edge["relation"] == "calls":
                assert edge["metadata"].get("source_file", "").endswith(".qml") or edge["source"].startswith("file:") or ".qml" in edge["edge_id"], (path, edge["edge_id"])
