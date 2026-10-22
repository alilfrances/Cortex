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
    assert got["symbol:base.cpp:run"][:3] == expected
    assert sorted(k for k in got if k != "symbol:base.cpp:run") == sorted(ranked[:OVERRIDES_PER_METHOD])
    assert all(got[o][:3] == expected for o in ranked[:OVERRIDES_PER_METHOD])
    assert routes == {o: "symbol:base.cpp:run" for o in ranked[:OVERRIDES_PER_METHOD]}


def test_an_overrider_is_walked_at_its_base_methods_hop_and_its_callees_name_it_as_their_origin():
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {
        "symbol:a.cpp:top": ["symbol:base.cpp:run"],
        "symbol:d0.cpp:run": ["symbol:e.cpp:assign", "symbol:d0.cpp:helper"],
        "symbol:d0.cpp:helper": ["symbol:f.cpp:deep"],
        "symbol:e.cpp:assign": ["symbol:g.cpp:tail"],
    }
    routes: dict[str, str] = {}

    got = _walk(
        [("symbol:a.cpp:top", 100.0)], calls, routes=routes,
        overriders=lambda sid: ["symbol:d0.cpp:run"] if sid == "symbol:base.cpp:run" else [],
    )

    # The virtual call is one call: the overrider stays at hop 1 with the route through its base.
    assert got["symbol:d0.cpp:run"][:3] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)
    assert routes == {"symbol:d0.cpp:run": "symbol:base.cpp:run"}
    # Its callees follow the normal budget: hop 2 into another file, the same-file helper walked through.
    assert got["symbol:e.cpp:assign"][:3] == (100.0 * CALLEE_HOP_SHARE[1], "symbol:d0.cpp:run", 2)
    assert got["symbol:f.cpp:deep"][:3] == (100.0 * CALLEE_HOP_SHARE[1], "symbol:d0.cpp:run", 2)
    assert "symbol:g.cpp:tail" not in got  # a third hop is still out of reach


def test_an_overrider_reached_through_a_same_file_call_is_walked_once():
    calls = {"symbol:a.cpp:top": ["symbol:base.cpp:run"], "symbol:base.cpp:run": ["symbol:d0.cpp:run"]}

    got = _walk(
        [("symbol:a.cpp:top", 100.0)], calls,
        overriders=lambda sid: ["symbol:d0.cpp:run"] if sid == "symbol:base.cpp:run" else [],
    )

    assert got["symbol:d0.cpp:run"][1:3] == ("symbol:a.cpp:top", 1)  # kept the route of the first offer


def test_each_callee_and_overrider_carries_its_own_relevance_for_tie_breaks():
    calls = {"symbol:a.cpp:top": ["symbol:base.cpp:run", "symbol:c.cpp:work"]}
    weights = {"symbol:base.cpp:run": 0.25, "symbol:d0.cpp:run": 1.0, "symbol:c.cpp:work": 0.5}

    got = _walk(
        [("symbol:a.cpp:top", 100.0)], calls,
        relevance=lambda sid: weights.get(sid, 0.0), overriders=lambda sid: ["symbol:d0.cpp:run", "symbol:d1.cpp:run"]
        if sid == "symbol:base.cpp:run" else [],
    )

    assert {sid: entry[3] for sid, entry in got.items()} == {
        "symbol:base.cpp:run": 0.25, "symbol:d0.cpp:run": 1.0, "symbol:d1.cpp:run": 0.0, "symbol:c.cpp:work": 0.5,
    }
    assert _walk([("symbol:a.cpp:top", 100.0)], calls)["symbol:c.cpp:work"][3] == 0.0  # no relevance function


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


def test_the_best_span_of_each_of_the_five_best_files_is_a_seed():
    paths = [f"f{i}.cpp" for i in range(7)]
    nodes = {
        n.node_id: n for n in [
            *[method(path, "big", span=(1, 8)) for path in paths[:2]],
            *[method(path, "run", span=(1, 8)) for path in paths[2:]],
            *[method("f0.cpp", f"work{i}", span=(10 * i, 10 * i + 6)) for i in range(1, 9)],
        ]
    }
    items = [_span_item("f0.cpp", f"work{i}", 500.0 - i) for i in range(1, 9)]
    items += [_span_item(path, "big" if i < 2 else "run", 100.0 - i) for i, path in enumerate(paths)]
    files = {path: 1000.0 - 10 * i for i, path in enumerate(paths)}

    names = {item.item_id for item in _select_seeds(items, nodes, files)}

    for path in paths[:5]:
        assert any(f":{path}:" in name for name in names), (path, names)
    assert not any(":f5.cpp:" in name or ":f6.cpp:" in name for name in names)


# --- Task 5.4: constructors do not seed, and `new X` (an edge to the class node) reaches the constructors


def test_call_links_map_each_class_to_its_constructors_inline_and_out_of_line():
    inline = GraphNode(  # an inline constructor that kept its own id (the extractor merges most into the class node)
        node_id="symbol:src/foo.h:Foo.Foo", kind="func", label="Foo", source_ref="src/foo.h", granularity="symbol",
        span_start=3, span_end=5,
    )
    nodes = [
        cls("src/foo.h", "Foo", span=(1, 12)), inline, method("src/foo.h", "tick", span=(7, 9)),
        method("src/foo.cpp", "Foo", qualifier="Foo", span=(4, 20)),  # out of line, by qualifier
        method("src/foo.cpp", "~Foo", qualifier="Foo", span=(22, 30)),  # a destructor is not one
        method("src/foo.cpp", "tick", qualifier="Foo", span=(32, 40)),
        cls("src/bar.h", "Bar"), cls("src/bar.cpp", "Bar"),  # ambiguous label: the qualifier names no class
        method("src/baz.cpp", "Bar", qualifier="Bar"),
        method("src/util.cpp", "Foo", span=(1, 9)),  # a free function that happens to share the label
        cls("src/lone.h", "Lone"),  # no constructor symbol
    ]

    links = _call_graph_links(nodes, [])

    assert links.constructors_of == {"symbol:src/foo.h:Foo": ["symbol:src/foo.cpp:Foo", "symbol:src/foo.h:Foo.Foo"]}
    assert _call_graph_links(nodes, [], with_overrides=False).constructors_of == {}  # file links only


def test_constructors_are_never_seeds_but_the_other_methods_of_their_class_are():
    nodes = {
        n.node_id: n for n in [
            cls("src/hub.h", "Hub", span=(1, 6)),
            method("src/hub.cpp", "Hub", qualifier="Hub", span=(3, 300)),
            method("src/hub.cpp", "retry", qualifier="Hub", span=(302, 310)),
        ]
    }
    items = [_span_item("src/hub.cpp", "Hub", 300.0), _span_item("src/hub.cpp", "retry", 120.0)]
    files = {"src/hub.cpp": 300.0}

    seeds = _select_seeds(items, nodes, files, frozenset({"symbol:src/hub.cpp:Hub"}))

    assert [item.metadata["node_id"] for item in seeds] == ["symbol:src/hub.cpp:retry"]
    assert len(_select_seeds(items, nodes, files)) == 2  # without the set nothing changes


def test_a_callee_that_is_a_class_is_replaced_by_its_constructors_at_the_same_hop():
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {
        "symbol:a.cpp:top": ["symbol:w.h:Widget", "symbol:plain.h:Plain"],
        "symbol:w.cpp:Widget": ["symbol:z.cpp:build"],
    }
    ctors = {"symbol:w.h:Widget": ["symbol:w.cpp:Widget", "symbol:w.cpp:Widget2"]}

    got = _walk([("symbol:a.cpp:top", 100.0)], calls, constructors_of=ctors)

    first = 100.0 * CALLEE_HOP_SHARE[0]
    assert "symbol:w.h:Widget" not in got  # the class node itself is not offered
    assert got["symbol:w.cpp:Widget"][:3] == (first, "symbol:a.cpp:top", 1)
    assert got["symbol:w.cpp:Widget2"][:3] == (first, "symbol:a.cpp:top", 1)
    assert got["symbol:plain.h:Plain"][:3] == (first, "symbol:a.cpp:top", 1)  # no constructor symbol: as today
    assert got["symbol:z.cpp:build"][:3] == (100.0 * CALLEE_HOP_SHARE[1], "symbol:a.cpp:top", 2)


def test_a_class_node_that_carries_calls_stays_a_candidate_next_to_its_out_of_line_constructors():
    calls = {"symbol:a.cpp:top": ["symbol:w.h:Widget"], "symbol:w.h:Widget": ["symbol:z.cpp:inline_init"]}

    got = _walk([("symbol:a.cpp:top", 100.0)], calls, constructors_of={"symbol:w.h:Widget": ["symbol:w.cpp:Widget"]})

    assert {"symbol:w.cpp:Widget", "symbol:w.h:Widget", "symbol:z.cpp:inline_init"} <= set(got)  # its inline constructor


def test_a_constructor_in_the_callers_file_is_walked_through_without_a_hop():
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {
        "symbol:a.cpp:top": ["symbol:a.cpp:Widget"],
        "symbol:a.cpp:Widget": ["symbol:z.cpp:build"],
    }

    got = _walk([("symbol:a.cpp:top", 100.0)], calls, constructors_of={"symbol:a.cpp:Widget": ["symbol:a.cpp:Widget"]})

    assert got["symbol:z.cpp:build"][:3] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)
