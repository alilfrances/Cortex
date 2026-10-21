"""B.2 / B.3 (0.12.2): overriding methods derived at query time, relevance-ordered callees, real seeds."""
from __future__ import annotations

from cortex.bundle import (
    CALLEES_PER_SEED,
    OVERRIDES_PER_METHOD,
    UTILITY_MIN_CALLERS,
    _call_graph_links,
    _callee_scores,
    _select_seeds,
)
from cortex.models import BundleItem, GraphEdge, GraphNode
from cortex.overrides import find_overriders


def cls(path: str, name: str, span=(1, 20)) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="class", label=name, source_ref=path, granularity="symbol",
        span_start=span[0], span_end=span[1],
    )


def method(path: str, name: str, *, qualifier: str | None = None, span=(1, 1)) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="func", label=name, source_ref=path, granularity="symbol",
        span_start=span[0], span_end=span[1], metadata={"qualifier": qualifier} if qualifier else {},
    )


def test_methods_with_the_name_of_a_base_method_override_it_through_the_resolved_chain():
    nodes = [
        cls("src/base.h", "Base"), cls("src/mid.h", "Mid"), cls("src/leaf.h", "Leaf"), cls("src/other.h", "Other"),
        method("src/base.cpp", "run", qualifier="Base"), method("src/base.cpp", "Base", qualifier="Base"),
        method("src/mid.cpp", "run", qualifier="Mid"), method("src/mid.cpp", "Mid", qualifier="Mid"),
        method("src/leaf.cpp", "run", qualifier="Leaf"), method("src/leaf.cpp", "helper", qualifier="Leaf"),
        method("src/other.cpp", "run", qualifier="Other"),  # unrelated class, same name: not an override
    ]
    bases = {"symbol:src/mid.h:Mid": ["symbol:src/base.h:Base"], "symbol:src/leaf.h:Leaf": ["symbol:src/mid.h:Mid"]}

    over = find_overriders(nodes, bases)

    assert over["symbol:src/base.cpp:run"] == ["symbol:src/leaf.cpp:run", "symbol:src/mid.cpp:run"]
    assert over["symbol:src/mid.cpp:run"] == ["symbol:src/leaf.cpp:run"]
    assert "symbol:src/base.cpp:Base" not in over and "symbol:src/mid.cpp:Mid" not in over  # constructors
    assert "symbol:src/leaf.cpp:helper" not in over.get("symbol:src/base.cpp:run", [])
    assert not any("other.cpp" in target for targets in over.values() for target in targets)


def test_inline_members_are_found_by_span_containment_and_no_hierarchy_means_no_overrides():
    nodes = [
        cls("src/a.h", "Base", span=(1, 6)), method("src/a.h", "tick", span=(3, 3)),
        cls("src/b.h", "Derived", span=(1, 6)), method("src/b.h", "tick", span=(3, 3)),
    ]

    assert find_overriders(nodes, {"symbol:src/b.h:Derived": ["symbol:src/a.h:Base"]}) == {
        "symbol:src/a.h:tick": ["symbol:src/b.h:tick"]
    }
    assert find_overriders(nodes, {}) == {}


def test_call_links_carry_the_overriders_and_the_utilities():
    nodes = [
        cls("src/base.h", "Base"), cls("src/derived.h", "Derived"),
        method("src/base.cpp", "run", qualifier="Base"), method("src/derived.cpp", "run", qualifier="Derived"),
        method("src/util.cpp", "log"), method("src/util.cpp", "rare"), method("src/util.cpp", "rarer"),
        *[method(f"src/c{i}.cpp", f"caller{i}") for i in range(UTILITY_MIN_CALLERS + 5)],
    ]
    edges = [
        GraphEdge("i1", "symbol:src/derived.h:Derived", "symbol:src/base.h:Base", "inherits", "STRUCTURAL"),
        *[
            GraphEdge(f"c{i}", f"symbol:src/c{i}.cpp:caller{i}", "symbol:src/util.cpp:log", "calls", "STRUCTURAL",
                      metadata={"lineno": 1})
            for i in range(UTILITY_MIN_CALLERS + 5)
        ],
        GraphEdge("r1", "symbol:src/c0.cpp:caller0", "symbol:src/util.cpp:rare", "calls", "STRUCTURAL", metadata={"lineno": 2}),
        GraphEdge("r2", "symbol:src/c1.cpp:caller1", "symbol:src/util.cpp:rarer", "calls", "STRUCTURAL", metadata={"lineno": 2}),
    ]

    links = _call_graph_links(nodes, edges)

    assert links.overridden_by == {"symbol:src/base.cpp:run": ["symbol:src/derived.cpp:run"]}
    assert links.utilities == {"symbol:src/util.cpp:log"}
    assert links.bases_of == {"symbol:src/derived.h:Derived": ["symbol:src/base.h:Base"]}


def _walk(seeds, calls, **kwargs):
    return _callee_scores(
        seeds, calls, lambda sid: sid.split(":")[1] if sid.count(":") >= 2 else None, lambda sid: None, {}, **kwargs
    )


def test_a_reached_callee_offers_its_overriders_at_its_own_score_and_hop_best_four_only():
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {"symbol:a.cpp:top": ["symbol:base.cpp:run"]}
    ranked = [f"symbol:d{i}.cpp:run" for i in range(OVERRIDES_PER_METHOD + 3)]
    routes: dict[str, str] = {}

    got = _walk([("symbol:a.cpp:top", 100.0)], calls, overriders=lambda sid: ranked, routes=routes)

    expected = (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)
    assert got["symbol:base.cpp:run"] == expected
    assert sorted(k for k in got if k != "symbol:base.cpp:run") == sorted(ranked[:OVERRIDES_PER_METHOD])
    assert all(got[o] == expected for o in ranked[:OVERRIDES_PER_METHOD])
    assert routes == {o: "symbol:base.cpp:run" for o in ranked[:OVERRIDES_PER_METHOD]}


def test_the_most_relevant_callees_are_followed_not_the_first_in_call_order():
    targets = [f"symbol:f{i}.cpp:t{i}" for i in range(10)]
    relevant = {targets[7], targets[9]}

    got = _walk(
        [("symbol:a.cpp:top", 100.0)], {"symbol:a.cpp:top": targets},
        relevance=lambda sid: 1.0 if sid in relevant else 0.25,
    )

    assert len(got) == CALLEES_PER_SEED
    assert relevant <= set(got)  # then call-site order: t0 and t1
    assert set(got) == relevant | {targets[0], targets[1]}


def test_callees_with_zero_relevance_are_not_followed_or_walked_through():
    calls = {
        "symbol:a.cpp:top": ["symbol:b.cpp:getX", "symbol:c.cpp:work"],
        "symbol:b.cpp:getX": ["symbol:d.cpp:behind"],
    }

    got = _walk([("symbol:a.cpp:top", 100.0)], calls, relevance=lambda sid: 0.0 if "getX" in sid else 1.0)

    assert sorted(got) == ["symbol:c.cpp:work"]


def _span_item(path: str, name: str, score: float) -> BundleItem:
    return BundleItem(
        item_id=f"symbol-span:symbol:{path}:{name}", kind="symbol", title=name, path=path, content="x",
        token_count=1, score=score, metadata={"node_id": f"symbol:{path}:{name}"},
    )


def test_seeds_are_functions_of_more_than_three_lines_with_one_from_each_of_the_best_files():
    nodes = {
        n.node_id: n for n in [
            cls("a.cpp", "Hub", span=(1, 90)),
            method("a.cpp", "factory", span=(2, 3)),
            *[method("a.cpp", f"work{i}", span=(10 * i, 10 * i + 6)) for i in range(1, 8)],
            method("b.cpp", "dispatch", span=(1, 8)),
            method("c.cpp", "other", span=(1, 8)),
        ]
    }
    items = [_span_item("a.cpp", "Hub", 300.0), _span_item("a.cpp", "factory", 290.0)]
    items += [_span_item("a.cpp", f"work{i}", 200.0 - i) for i in range(1, 8)]
    items += [_span_item("b.cpp", "dispatch", 40.0), _span_item("c.cpp", "other", 30.0)]
    files = {"a.cpp": 200.0, "b.cpp": 80.0, "c.cpp": 60.0}

    seeds = _select_seeds(items, nodes, files)

    names = [item.metadata["node_id"].rsplit(":", 1)[-1] for item in seeds]
    assert "Hub" not in names and "factory" not in names
    assert len(names) == 6
    assert "dispatch" in names and "other" in names  # the best span of the 2nd and 3rd best files
    assert names[:3] == ["work1", "work2", "work3"]  # then by score
