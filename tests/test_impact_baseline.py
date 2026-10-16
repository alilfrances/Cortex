"""Exact-value characterization of ``cortex.impact.rank_file_impact`` at 7b0e6e0.

Hand-built nodes/edges, no git, no parser.  Pins scores, ordering, tie-breaks,
``why`` ordering, truncation and error behaviour so Task 9 (call edges feed impact)
can only change what it intends to.  The test marked ``TASK 9`` is the deliberate
flip point.
"""

from __future__ import annotations

import json

import pytest

from cortex.impact import (
    COCHANGE_IMPACT_MULTIPLIER,
    IMPACT_LAYERS,
    STRUCTURAL_IMPACT_MULTIPLIER,
    UnknownPathError,
    rank_file_impact,
)
from cortex.models import GraphEdge, GraphNode
from tests.golden_utils import force_stdlib_tokenizer


@pytest.fixture(autouse=True)
def _stdlib_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    force_stdlib_tokenizer(monkeypatch)


def file_node(path: str, **metadata) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path, metadata=dict(metadata))


def symbol_node(path: str, name: str) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind="func",
        label=name,
        source_ref=path,
        granularity="symbol",
        span_start=1,
        span_end=2,
    )


def commit_node(sha: str) -> GraphNode:
    # Commit nodes default to granularity="file" -- the reason impact must exclude them.
    return GraphNode(node_id=f"commit:{sha}", kind="commit", label="msg", source_ref=sha)


def edge(source: str, target: str, relation: str, layer: str, weight: float = 1.0, **metadata) -> GraphEdge:
    return GraphEdge(
        edge_id=f"{layer}:{relation}:{source}->{target}",
        source=source,
        target=target,
        relation=relation,
        layer=layer,
        weight=weight,
        metadata=dict(metadata),
    )


def imports(a: str, b: str, weight: float = 1.0) -> GraphEdge:
    return edge(f"file:{a}", f"file:{b}", "imports", "STRUCTURAL", weight)


def cochange(a: str, b: str, weight: float, count: int = 1) -> GraphEdge:
    return edge(f"file:{a}", f"file:{b}", "cochange", "COCHANGE", weight, count=count, max_count=4)


def test_layer_constants() -> None:
    assert IMPACT_LAYERS == {"COCHANGE", "STRUCTURAL"}
    assert COCHANGE_IMPACT_MULTIPLIER == 2.0
    assert STRUCTURAL_IMPACT_MULTIPLIER == 1.0


def mixed_graph() -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes = [
        file_node("src/core.py", hotspot={"churn": 4, "complexity": 50, "score": 200}),
        file_node("src/util.py"),
        file_node("src/api.py", hotspot={"churn": 2, "complexity": 10}),  # score derived: 20
        file_node("src/zeta.py"),
        file_node("src/alpha.py"),
        file_node("docs/readme.md", hotspot="not-a-dict"),
        file_node("src/lonely.py"),
    ]
    edges = [
        # util: structural 1.0 (as importer) + cochange 0.5*2 => 2.0, two why entries
        imports("src/util.py", "src/core.py", 1.0),
        cochange("src/core.py", "src/util.py", 0.5, count=2),
        # api: cochange 1.0*2 => 2.0 (tie with util), edge stored with core as target
        cochange("src/api.py", "src/core.py", 1.0, count=4),
        # zeta / alpha tie at 1.0: path order decides (alpha before zeta)
        imports("src/core.py", "src/zeta.py"),
        imports("src/alpha.py", "src/core.py"),
        # readme: heading edge is ignored, cochange 0.25*2 => 0.5
        edge("file:src/core.py", "file:docs/readme.md", "contains", "HEADING", 1.0),
        cochange("src/core.py", "docs/readme.md", 0.25),
        # unrelated pair must never appear
        imports("src/alpha.py", "src/lonely.py"),
    ]
    return nodes, edges


def test_scores_order_tiebreak_and_why_ordering() -> None:
    nodes, edges = mixed_graph()

    result, truncated = rank_file_impact("src/core.py", nodes, edges, limit=10, budget=100_000)

    assert truncated is False
    assert [(item["path"], item["score"]) for item in result] == [
        ("src/api.py", 2.0),  # tie at 2.0 with util: path ascending
        ("src/util.py", 2.0),
        ("src/alpha.py", 1.0),  # tie at 1.0: alpha < zeta
        ("src/zeta.py", 1.0),
        ("docs/readme.md", 0.5),
    ]
    by_path = {item["path"]: item for item in result}
    # `why` sorted by (-weight, relation); `cochange_count` only on cochange entries.
    assert by_path["src/util.py"]["why"] == [
        {"relation": "imports", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"},
        {"relation": "cochange", "weight": 0.5, "layer": "COCHANGE", "confidence": "EXTRACTED", "cochange_count": 2},
    ]
    assert by_path["src/api.py"]["why"] == [
        {"relation": "cochange", "weight": 1.0, "layer": "COCHANGE", "confidence": "EXTRACTED", "cochange_count": 4}
    ]
    assert all(item["node_id"] == f"file:{item['path']}" for item in result)
    assert "src/lonely.py" not in by_path and "src/core.py" not in by_path


def test_hotspot_defaults_and_derived_score() -> None:
    nodes, edges = mixed_graph()
    # Make every neighbour appear.
    result, _ = rank_file_impact("src/core.py", nodes, edges, limit=10, budget=100_000)
    by_path = {item["path"]: item["hotspot"] for item in result}

    assert by_path["src/api.py"] == {"churn": 2, "complexity": 10, "score": 20}  # churn * complexity
    assert by_path["src/util.py"] == {"churn": 0, "complexity": 0, "score": 0}  # no metadata
    assert by_path["docs/readme.md"] == {"churn": 0, "complexity": 0, "score": 0}  # non-dict ignored
    seed_view, _ = rank_file_impact("src/util.py", nodes, edges, budget=100_000)
    assert [i["path"] for i in seed_view] == ["src/core.py"]
    assert seed_view[0]["hotspot"] == {"churn": 4, "complexity": 50, "score": 200}  # explicit score wins


def test_limit_applies_after_ranking() -> None:
    nodes, edges = mixed_graph()

    top2, truncated = rank_file_impact("src/core.py", nodes, edges, limit=2, budget=100_000)

    assert [i["path"] for i in top2] == ["src/api.py", "src/util.py"]
    assert truncated is False  # `limit` cuts silently; only the token budget sets truncated
    assert rank_file_impact("src/core.py", nodes, edges, limit=0, budget=100_000) == ([], False)


def test_path_accepts_file_prefix_and_is_direction_agnostic() -> None:
    nodes, edges = mixed_graph()

    plain, _ = rank_file_impact("src/core.py", nodes, edges, budget=100_000)
    prefixed, _ = rank_file_impact("file:src/core.py", nodes, edges, budget=100_000)

    assert plain == prefixed
    # Edges written with the seed as *target* and as *source* both count.
    assert {"src/util.py", "src/api.py", "src/zeta.py", "src/alpha.py"} <= {i["path"] for i in plain}


def test_unknown_path_raises() -> None:
    nodes, edges = mixed_graph()

    with pytest.raises(UnknownPathError) as excinfo:
        rank_file_impact("src/missing.py", nodes, edges)

    assert excinfo.value.path == "src/missing.py"
    assert str(excinfo.value) == "path not found in graph: src/missing.py"
    assert isinstance(excinfo.value, ValueError)
    with pytest.raises(UnknownPathError):
        rank_file_impact("file:src/missing.py", nodes, edges)


def test_commit_nodes_are_excluded_from_results_and_as_seed() -> None:
    nodes, edges = mixed_graph()
    sha = "a" * 40
    nodes.append(commit_node(sha))
    edges.append(edge(f"commit:{sha}", "file:src/core.py", "touches", "COCHANGE", 1.0))

    result, _ = rank_file_impact("src/core.py", nodes, edges, budget=100_000)

    assert all(sha not in item["path"] and not item["node_id"].startswith("commit:") for item in result)
    assert [i["path"] for i in result] == ["src/api.py", "src/util.py", "src/alpha.py", "src/zeta.py", "docs/readme.md"]
    with pytest.raises(UnknownPathError):
        rank_file_impact(f"commit:{sha}", nodes, edges)


def test_non_impact_layers_self_edges_and_dangling_edges_are_ignored() -> None:
    nodes = [file_node("a.py"), file_node("b.py")]
    edges = [
        edge("file:a.py", "file:b.py", "contains", "HEADING", 5.0),
        edge("file:a.py", "file:a.py", "imports", "STRUCTURAL", 3.0),  # self edge
        edge("file:a.py", "file:ghost.py", "imports", "STRUCTURAL", 3.0),  # target not in graph
        edge("file:ghost.py", "file:a.py", "cochange", "COCHANGE", 3.0),
    ]

    assert rank_file_impact("a.py", nodes, edges) == ([], False)


def test_symbol_level_structural_edges_are_aggregated_per_file() -> None:
    """TASK 9 FLIP (was ``test_symbol_level_structural_edges_contribute_nothing_today``).

    At 7b0e6e0 only file<->file edges counted, so a caller in ``a.cpp`` of a
    function in ``b.cpp`` gave ``b.cpp`` no impact score at all (both calls
    returned ``([], False)``).  Task 9 aggregates every cross-file symbol-level
    STRUCTURAL edge onto the two files (``weight * STRUCTURAL_IMPACT_MULTIPLIER``
    per distinct (source, target, relation)), with a ``symbol`` why-entry.
    ``contains`` and same-file edges still contribute nothing.
    """
    nodes = [
        file_node("a.cpp"),
        file_node("b.cpp"),
        symbol_node("a.cpp", "caller"),
        symbol_node("b.cpp", "callee"),
    ]
    edges = [
        edge("symbol:a.cpp:caller", "symbol:b.cpp:callee", "calls", "STRUCTURAL", 1.0),
        edge("file:a.cpp", "symbol:b.cpp:callee", "references", "STRUCTURAL", 1.0),
        edge("symbol:a.cpp:caller", "file:b.cpp", "references", "STRUCTURAL", 1.0),
        edge("file:a.cpp", "symbol:a.cpp:caller", "contains", "STRUCTURAL", 1.0),
    ]

    ranked, truncated = rank_file_impact("a.cpp", nodes, edges)
    assert truncated is False
    assert [(i["path"], i["score"]) for i in ranked] == [("b.cpp", 3.0)]
    assert ranked[0]["why"] == [
        {"relation": "calls", "symbol": "caller -> callee", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"},
        {"relation": "references", "symbol": "a.cpp -> callee", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"},
        {"relation": "references", "symbol": "caller -> b.cpp", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"},
    ]
    # Direction-agnostic, like file edges.
    reverse, _ = rank_file_impact("b.cpp", nodes, edges)
    assert [(i["path"], i["score"]) for i in reverse] == [("a.cpp", 3.0)]

    # File-level scoring is untouched: the file edge adds its usual 1.0 and its
    # why-entry keeps the 7b0e6e0 shape (no ``symbol`` key).
    ranked, _ = rank_file_impact("a.cpp", nodes, [*edges, imports("a.cpp", "b.cpp")])
    assert [(i["path"], i["score"]) for i in ranked] == [("b.cpp", 4.0)]
    assert {"relation": "imports", "weight": 1.0, "layer": "STRUCTURAL", "confidence": "EXTRACTED"} in ranked[0]["why"]


def test_symbol_edges_dedupe_call_sites_cap_why_and_skip_same_file() -> None:
    nodes = [file_node("a.cpp"), file_node("b.cpp"), symbol_node("a.cpp", "caller"), symbol_node("a.cpp", "local")]
    nodes += [symbol_node("b.cpp", f"f{i}") for i in range(7)]
    edges = [
        # Same caller->callee at three lines: one contribution.
        GraphEdge(edge_id=f"calls:{line}", source="symbol:a.cpp:caller", target="symbol:b.cpp:f0",
                  relation="calls", layer="STRUCTURAL", weight=1.0)
        for line in range(3)
    ]
    edges += [edge("symbol:a.cpp:caller", f"symbol:b.cpp:f{i}", "calls", "STRUCTURAL", 1.0) for i in range(1, 7)]
    edges.append(edge("symbol:a.cpp:caller", "symbol:a.cpp:local", "calls", "STRUCTURAL", 5.0))  # same file
    edges.append(edge("symbol:a.cpp:caller", "name:external", "calls", "STRUCTURAL", 5.0))  # unresolved

    ranked, _ = rank_file_impact("a.cpp", nodes, edges, budget=100_000)

    assert [(i["path"], i["score"]) for i in ranked] == [("b.cpp", 7.0)]
    assert [w["symbol"] for w in ranked[0]["why"]] == [f"caller -> f{i}" for i in range(5)]


def test_unresolved_name_targets_never_count() -> None:
    nodes = [file_node("a.py"), symbol_node("a.py", "f")]
    edges = [edge("symbol:a.py:f", "name:helper", "calls", "STRUCTURAL", 1.0)]

    assert rank_file_impact("a.py", nodes, edges) == ([], False)


def test_budget_truncation_pops_lowest_ranked_first() -> None:
    nodes, edges = mixed_graph()
    full, _ = rank_file_impact("src/core.py", nodes, edges, budget=100_000)
    from cortex.tokenizer import count_text_tokens

    sizes = [count_text_tokens(json.dumps(full[: n])) for n in range(len(full) + 1)]
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)

    # A budget that just fits the first three entries truncates the last two.
    budget = sizes[3]
    result, truncated = rank_file_impact("src/core.py", nodes, edges, budget=budget)
    assert truncated is True
    assert result == full[:3]

    # One token short of that drops one more.
    result, truncated = rank_file_impact("src/core.py", nodes, edges, budget=sizes[3] - 1)
    assert truncated is True
    assert result == full[:2]

    # Exactly enough for everything: not truncated.
    result, truncated = rank_file_impact("src/core.py", nodes, edges, budget=sizes[-1])
    assert truncated is False and result == full

    # Nothing fits: empty list, truncated.
    assert rank_file_impact("src/core.py", nodes, edges, budget=0) == ([], True)
    assert rank_file_impact("src/core.py", nodes, edges, budget=1) == ([], True)


def test_ranking_is_independent_of_edge_and_node_order() -> None:
    nodes, edges = mixed_graph()
    baseline, _ = rank_file_impact("src/core.py", nodes, edges, budget=100_000)

    reordered, _ = rank_file_impact("src/core.py", list(reversed(nodes)), list(reversed(edges)), budget=100_000)

    assert reordered == baseline
