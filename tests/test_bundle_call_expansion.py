from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex.bundle import CALL_EXPANSION_BONUS, generate_bundle
from cortex.ingest import ingest_repository
from tests.golden_utils import TREE_SITTER_ONLY

FILES = {
    # Keyword hit: the manager talks about the task in its own words.
    "src/manager.cpp": (
        "#include \"base.hpp\"\n"
        "// device card state wrong after reconnect: the manager restores the card state on reconnect\n"
        "void Manager::reconnect(Widget* widget) {\n"
        "    widget->apply(1);\n"
        "}\n"
    ),
    # The behaviour lives here, in words the task never uses.
    "src/base.hpp": "class Base {\npublic:\n    virtual void apply(int v);\nprotected:\n    int slot_ = 0;\n};\n",
    "src/base.cpp": "#include \"base.hpp\"\nvoid Base::apply(int v) { slot_ = v; }\n",
    "src/widget.hpp": "#include \"base.hpp\"\nclass Widget : public Base {\npublic:\n    void apply(int v) override;\n};\n",
    "src/widget.cpp": "#include \"widget.hpp\"\nvoid Widget::apply(int v) { Base::apply(v); }\n",
    # Unrelated noise.
    "src/other.cpp": "void unrelated() { int x = 1; }\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    for rel, content in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def test_files_linked_by_calls_and_inheritance_are_lifted_by_the_expansion(repo):
    bundle = generate_bundle(
        repo_path=repo, task="device card state wrong after reconnect", budget=4000, output_format="json"
    )
    scores = {item["path"]: item["score"] for item in bundle["items"] if item["kind"] == "code"}
    assert "src/manager.cpp" in scores
    # Neither file shares a word with the task; both are one resolved call edge from the manager.
    assert scores["src/widget.cpp"] >= CALL_EXPANSION_BONUS
    assert scores["src/widget.cpp"] > scores.get("src/other.cpp", 0.0)
    # The expansion lifts files linked to the best match, not the whole repository.
    assert scores.get("src/other.cpp", 0.0) < CALL_EXPANSION_BONUS


# --- 0.12.0 review: callees of the top match (docs/plan-0.12.1-review.md, Task B.0) ---


@pytest.mark.parametrize("budget", [600, 900])
def test_callees_of_the_top_match_are_packed(tmp_path, monkeypatch, budget):
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    task = COMPANION_GOLD_TASKS[0]
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)

    bundle = generate_bundle(repo, task=task.description, budget=budget, output_format="json")

    paths = {item["path"] for item in bundle["items"]}
    assert set(task.expected_files) <= paths, sorted(paths)


def test_the_companion_fixture_has_the_resolved_call_chain(tmp_path, monkeypatch):
    """Guards the fixture itself: if these edges stop resolving, the test above proves nothing."""
    from cortex.store import CortexStore, default_db_path
    from evals.run_evals import _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    resolved = {(e.source, e.target) for e in edges if e.relation == "calls" and e.target.startswith("symbol:")}

    assert ("symbol:src/device_manager.cpp:reportDevice", "symbol:src/peripheral_core.cpp:assignCompanion") in resolved
    assert ("symbol:src/peripheral_core.cpp:releaseCompanion", "symbol:src/registry.cpp:drop") in resolved


# --- the callee walk (docs/plan-0.12.1-review.md, Task B.1) ---


def _walk(seeds, calls, files=None, bases=None, owner=None):
    from cortex.bundle import _callee_scores

    file_of = lambda sid: (files or {}).get(sid, sid.split(":")[1] if sid.count(":") >= 2 else None)  # noqa: E731
    return _callee_scores(seeds, calls, file_of, lambda sid: (owner or {}).get(sid), bases or {})


def test_walk_scores_callees_from_the_seed_and_passes_through_same_file_calls() -> None:
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {
        "symbol:a.cpp:top": ["symbol:b.cpp:direct"],
        "symbol:b.cpp:direct": ["symbol:b.cpp:sibling"],      # same file: no hop spent
        "symbol:b.cpp:sibling": ["symbol:c.cpp:far"],         # second crossing
        "symbol:c.cpp:far": ["symbol:d.cpp:too_far"],         # third crossing: out of range
    }
    got = _walk([("symbol:a.cpp:top", 100.0)], calls)

    assert got["symbol:b.cpp:direct"][:3] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)
    assert got["symbol:b.cpp:sibling"][2] == 1
    assert got["symbol:c.cpp:far"][:3] == (100.0 * CALLEE_HOP_SHARE[1], "symbol:a.cpp:top", 2)
    assert "symbol:d.cpp:too_far" not in got
    assert "symbol:a.cpp:top" not in got


def test_walk_keeps_the_best_route_over_several_seeds_and_never_expands_callers() -> None:
    calls = {"symbol:a.cpp:s1": ["symbol:x.cpp:shared"], "symbol:b.cpp:s2": ["symbol:x.cpp:shared"]}
    got = _walk([("symbol:a.cpp:s1", 80.0), ("symbol:b.cpp:s2", 120.0)], calls)

    assert got["symbol:x.cpp:shared"][1:3] == ("symbol:b.cpp:s2", 1)
    # A caller of a seed is not a callee of anything: nothing lifts it.
    got = _walk([("symbol:x.cpp:shared", 100.0)], calls)
    assert got == {}


def test_walk_caps_other_file_callees_per_function() -> None:
    from cortex.bundle import CALLEES_PER_SEED

    targets = [f"symbol:f{i}.cpp:t{i}" for i in range(10)]
    got = _walk([("symbol:a.cpp:top", 100.0)], {"symbol:a.cpp:top": targets})

    assert sorted(got) == sorted(targets[:CALLEES_PER_SEED])  # call-site order decides


def test_walk_scores_the_bases_of_the_seeds_class() -> None:
    from cortex.bundle import BASE_CLASS_SHARE

    got = _walk(
        [("symbol:a.cpp:method", 100.0)], {}, owner={"symbol:a.cpp:method": "symbol:a.h:Derived"},
        bases={"symbol:a.h:Derived": ["symbol:base.h:Base"]},
    )

    assert got == {"symbol:base.h:Base": (100.0 * BASE_CLASS_SHARE, "symbol:a.cpp:method", 1, 0.0)}


def test_expanded_items_say_why_they_were_selected(tmp_path, monkeypatch) -> None:
    from cortex.mcp.tools import _bundle_why, _compact_why
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)
    bundle = generate_bundle(repo, task=COMPANION_GOLD_TASKS[0].description, budget=900, output_format="json")

    routed = [item for item in bundle["items"] if item["metadata"].get("call_expansion")]
    assert routed, "the callee must carry its route"
    why = _bundle_why(routed[0], set(), set(), [])
    assert any(entry["type"] == "call" and entry["hop"] >= 1 for entry in why)
    assert _compact_why(why).startswith("calls: ")


def test_walk_keeps_the_shortest_route_when_a_longer_one_is_met_first() -> None:
    from cortex.bundle import CALLEE_HOP_SHARE

    calls = {
        # The seed calls y.cpp first: a FIFO walk reaches x.cpp:deep from there at hop 2 before
        # it expands x.cpp:entry, whose same-file call reaches the same function at hop 1.
        "symbol:a.cpp:top": ["symbol:y.cpp:p", "symbol:x.cpp:entry"],
        "symbol:y.cpp:p": ["symbol:x.cpp:deep"],
        "symbol:x.cpp:entry": ["symbol:x.cpp:deep"],
    }
    got = _walk([("symbol:a.cpp:top", 100.0)], calls)

    assert got["symbol:x.cpp:deep"][:3] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)


@pytest.mark.parametrize("mode", ["pagerank", "bfs", "skeleton_on", "skeleton_off"])
@pytest.mark.parametrize("which", ["companion", "spread"])
def test_focused_gold_tasks_reach_full_recall_in_every_mode(tmp_path, monkeypatch, which, mode):
    """Plan 0.12.1 Task B.1 acceptance: the companion task at recall 1.0 in all four modes, and the
    0.12 spread task still at 1.0 without the reserve it was built for."""
    from evals.run_evals import COMPANION_GOLD_TASKS, SPREAD_GOLD_TASKS, _build_companion_repo, _build_spread_repo, _score_task
    from tests.baseline_helpers import fixed_git_env

    fixed_git_env(monkeypatch)
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    task, build = {
        "companion": (COMPANION_GOLD_TASKS[0], _build_companion_repo),
        "spread": (SPREAD_GOLD_TASKS[0], _build_spread_repo),
    }[which]
    repo = build(tmp_path)
    db_path = tmp_path / f"{which}.db"
    ingest_repository(repo, commit_limit=20, db_path=db_path)

    row = _score_task(task, mode, repo, db_path)

    assert row["file_recall"] == 1.0, row["files"]


# --- 0.12.2 field review, Issue B (docs: field-shape fixtures in tests/bundle_shapes.py) ---


def _items_by_path(bundle: dict) -> dict[str, dict]:
    return {item["path"]: item for item in bundle["items"] if item["kind"] in ("code", "symbol")}


@pytest.mark.parametrize("budget", [600, 900])
def test_derived_override_is_packed(tmp_path, monkeypatch, budget):
    """The manager calls `Base::apply` through a `Base*`; the behaviour is in `Derived::apply`, in a
    file that shares no word with the task. `Base` is also a mock class and a JS helper, and
    `Derived`'s base is the unresolved `name:Base` the C++ extractor writes."""
    from tests.bundle_shapes import OVERRIDE_TASK, build_override_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = build_override_repo(tmp_path / "repo")

    bundle = generate_bundle(repo, task=OVERRIDE_TASK, budget=budget, output_format="json")

    from cortex.mcp.tools import _bundle_why, _compact_why

    packed = _items_by_path(bundle)
    assert "src/manager.cpp" in packed
    assert "src/derived.cpp" in packed, sorted(packed)
    why = _bundle_why(packed["src/derived.cpp"], set(), set(), [])
    overrides = [entry for entry in why if entry["type"] == "override"]
    assert overrides, why
    assert overrides[0]["via"] == "symbol:src/base.cpp:apply"
    assert overrides[0]["to"] == "symbol:src/derived.cpp:apply"
    assert _compact_why(why).startswith("override: ")


def test_callees_are_not_accessors(tmp_path, monkeypatch):
    """Ten accessors are called before the callee that matters: that callee is selected, no accessor is."""
    from tests.bundle_shapes import ACCESSOR_TASK, build_accessor_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = build_accessor_repo(tmp_path / "repo")

    bundle = generate_bundle(repo, task=ACCESSOR_TASK, budget=900, output_format="json")

    routed = {
        item["path"]: item["metadata"]["call_expansion"]
        for item in bundle["items"]
        if item["metadata"].get("call_expansion")
    }
    assert "src/engine.cpp" in routed, routed
    assert not [path for path in routed if path.startswith("src/props_")], routed


@pytest.mark.parametrize("budget", [600, 900])
def test_a_handler_without_a_task_word_in_its_name_leads_to_the_file_it_calls(tmp_path, monkeypatch, budget):
    """`dispatch` has no task word in its name, its body matches the task and calls a resolved function in a
    second file. Class nodes and one-line factories score higher; they must not take every seed slot."""
    from tests.bundle_shapes import HANDLER_TASK, build_handler_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = build_handler_repo(tmp_path / "repo")

    bundle = generate_bundle(repo, task=HANDLER_TASK, budget=budget, output_format="json")

    packed = _items_by_path(bundle)
    route = (packed.get("src/registry_store.cpp") or {}).get("metadata", {}).get("call_expansion")
    assert route and route["from"] == "symbol:src/hub.cpp:dispatch", (sorted(packed), route)


# --- relevance is a small score factor and IDF-weighted (docs/plan-0.14-field-review.md, Task 5.2) ---


def test_walk_scales_a_callee_score_by_a_small_relevance_factor() -> None:
    from cortex.bundle import CALLEE_HOP_SHARE, CALLEE_RELEVANCE_MIN, RELEVANCE_FLOOR, _callee_scores

    calls = {"symbol:a.cpp:top": ["symbol:b.cpp:hit", "symbol:c.cpp:miss"]}
    levels = {"symbol:b.cpp:hit": 1.0, "symbol:c.cpp:miss": RELEVANCE_FLOOR}
    got = _callee_scores(
        [("symbol:a.cpp:top", 100.0)], calls, lambda sid: sid.split(":")[1], lambda sid: None, {},
        relevance=lambda sid: levels[sid],
    )

    share = 100.0 * CALLEE_HOP_SHARE[0]
    assert got["symbol:b.cpp:hit"][0] == pytest.approx(share)
    assert got["symbol:c.cpp:miss"][0] == pytest.approx(share * (CALLEE_RELEVANCE_MIN + (1 - CALLEE_RELEVANCE_MIN) * RELEVANCE_FLOOR))
    # A callee that shares no word keeps most of its share: the 0.13.0 range 0.25-1 lost the companion fixture.
    assert 0.75 <= CALLEE_RELEVANCE_MIN <= 0.95
    assert got["symbol:c.cpp:miss"][0] >= 0.85 * share


def test_walk_does_not_scale_the_bases_of_the_seeds_class() -> None:
    from cortex.bundle import BASE_CLASS_SHARE, _callee_scores

    got = _callee_scores(
        [("symbol:a.cpp:method", 100.0)], {}, lambda sid: sid.split(":")[1],
        lambda sid: "symbol:a.h:Derived" if sid == "symbol:a.cpp:method" else None,
        {"symbol:a.h:Derived": ["symbol:base.h:Base"]}, relevance=lambda sid: 0.25,
    )

    assert got["symbol:base.h:Base"][0] == 100.0 * BASE_CLASS_SHARE


def test_body_relevance_weighs_the_rare_term_above_the_common_one() -> None:
    from cortex.bundle import RELEVANCE_FILE_HIT, _body_relevance

    weights = {"rare": 8.0, "common": 1.0, "mid": 4.0}
    assert _body_relevance({"rare"}, weights) == 1.0                          # the heaviest term alone
    assert _body_relevance({"common"}, weights) == RELEVANCE_FILE_HIT         # 1/8 floors at the file hit
    assert _body_relevance({"mid"}, weights) == pytest.approx(0.5)
    assert _body_relevance({"mid", "common"}, weights) == pytest.approx(5 / 8)
    assert _body_relevance({"rare", "mid", "common"}, weights) == 1.0         # capped


# --- 0.15.1 Task 1: what the walk did, in the bundle (docs/plan-0.15.1-fixes.md) ---

EXPANSION_KEYS = {
    "seeds", "seeds_cross_file", "seed_refusals", "callees_computed", "callee_files", "callees_packed", "callees_compact",
    "callees_dropped", "reserved",
}


def _balanced(expansion: dict) -> bool:
    """Every computed callee is packed, shown as a compact entry, or has exactly one reason for being left out."""
    shown = expansion["callees_packed"] + expansion["callees_compact"]
    return expansion["callees_computed"] == shown + sum(expansion["callees_dropped"].values())


def test_the_expansion_block_counts_the_companion_callees(tmp_path, monkeypatch):
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    task = COMPANION_GOLD_TASKS[0]
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)

    expansion = generate_bundle(repo, task=task.description, budget=900, output_format="json")["expansion"]

    assert set(expansion) == EXPANSION_KEYS
    assert expansion["callees_computed"] >= 1
    assert expansion["callees_packed"] >= 1
    assert expansion["callee_files"] >= 1
    assert expansion["seeds"] and {"id", "path", "score"} == set(expansion["seeds"][0])
    assert _balanced(expansion), expansion


def test_the_expansion_block_names_the_seed_of_the_field_seed_fixture(tmp_path, monkeypatch):
    from evals.run_evals import FIELD_GOLD_TASKS, _build_field_seed_markup_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    task = next(t for t in FIELD_GOLD_TASKS if t.repo == "field_seed_markup_repo")
    repo = _build_field_seed_markup_repo(tmp_path)
    ingest_repository(repo)

    expansion = generate_bundle(repo, task=task.description, budget=task.budget, output_format="json")["expansion"]

    assert any(seed["id"].startswith("symbol:src/itemmodel.cpp:") for seed in expansion["seeds"]), expansion["seeds"]
    # The screens' spans reach nothing, the model file's onTypeChanged does: refusals say which was which.
    assert expansion["seed_refusals"]["no_reach"] > 0
    assert set(expansion["seed_refusals"]) == {"class", "constructor", "trivial", "no_reach"}
    assert expansion["seeds_cross_file"] >= 1
    assert _balanced(expansion), expansion


def test_a_repository_without_resolved_calls_gets_the_empty_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "config.py").write_text("RETRY_LIMIT = 3\nTIMEOUT = 30\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)

    expansion = generate_bundle(root, task="retry limit timeout", budget=2000, output_format="json")["expansion"]

    assert expansion == {
        "seeds": [],
        "seeds_cross_file": 0,
        "seed_refusals": {"class": 0, "constructor": 0, "trivial": 0, "no_reach": 0},
        "callees_computed": 0,
        "callee_files": 0,
        "callees_packed": 0,
        "callees_compact": 0,
        "callees_dropped": {
            "too_large": 0, "budget_exhausted": 0, "file_cap": 0, "inside_selected_span": 0, "not_reached": 0, "no_candidate": 0,
        },
        "reserved": False,
    }


def test_the_concise_summary_says_so_only_when_the_budget_was_reserved():
    from cortex.mcp.tools import _expansion_summary

    expansion = {"seeds": [{}], "seeds_cross_file": 1, "callees_computed": 3, "callees_packed": 3, "callees_dropped": {}, "reserved": False}

    assert _expansion_summary(expansion) == "seeds 1 (cross-file 1), callees 3, packed 3"
    assert _expansion_summary({**expansion, "reserved": True}) == "seeds 1 (cross-file 1), callees 3, packed 3, reserved"
    assert _expansion_summary({**expansion, "callees_compact": 5}) == "seeds 1 (cross-file 1), callees 3, packed 3, compact 5"
    assert _expansion_summary({**expansion, "callees_compact": 5, "callees_dropped": {"budget_exhausted": 2}}) == (
        "seeds 1 (cross-file 1), callees 3, packed 3, compact 5 (dropped: budget_exhausted 2)"
    )


def test_concise_cortex_query_carries_the_expansion_summary(tmp_path, monkeypatch):
    import json

    from cortex.mcp.tools import call_tool
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    task = COMPANION_GOLD_TASKS[0]
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)
    args = {"repo_path": str(repo), "task": task.description, "budget": 900}

    concise = json.loads(call_tool("cortex_query", args)["content"][0]["text"])
    detailed = json.loads(call_tool("cortex_query", {**args, "response_format": "detailed"})["content"][0]["text"])

    assert "expansion" not in concise
    assert concise["expansion_summary"].startswith("seeds ") and ", callees " in concise["expansion_summary"]
    assert ", packed " in concise["expansion_summary"]
    assert set(detailed["expansion"]) == EXPANSION_KEYS
    assert f"callees {detailed['expansion']['callees_computed']}," in concise["expansion_summary"]


def test_a_callee_that_is_larger_than_one_item_may_be_counts_as_too_large(tmp_path, monkeypatch):
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)

    expansion = generate_bundle(repo, task=COMPANION_GOLD_TASKS[0].description, budget=250, output_format="json")["expansion"]

    assert expansion["callees_dropped"]["too_large"] >= 1, expansion
    assert expansion["callees_dropped"]["budget_exhausted"] == 0, expansion
    assert expansion["callees_packed"] < expansion["callees_computed"]
    assert _balanced(expansion), expansion


def _step_repo(tmp_path: Path, monkeypatch, *, fillers: int, body_lines: int) -> Path:
    """`Manager::reconnect` calls six `stepN` functions, each in its own file with `body_lines` lines; `fillers` files
    repeat the task's words and rank above the callees."""
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    calls = "".join(f"    step{i}(widget);\n" for i in range(6))
    (root / "src" / "manager.cpp").write_text(
        '#include "steps.hpp"\n// device card reconnect: restore the card state after the device reconnect\n'
        f"void Manager::reconnect(Widget* widget) {{\n{calls}}}\n"
    )
    (root / "src" / "steps.hpp").write_text("".join(f"void step{i}(Widget* w);\n" for i in range(6)))
    for i in range(6):
        body = "".join(f"    w->slot{j} = {j} + {i};\n" for j in range(body_lines))
        (root / "src" / f"step{i}.cpp").write_text(f'#include "steps.hpp"\nvoid step{i}(Widget* w) {{\n{body}}}\n')
    for i in range(fillers):
        body = "".join(f"    // device card reconnect handling {i} line {j}\n" for j in range(30))
        (root / "src" / f"card{i}.cpp").write_text(f"void cardNote{i}() {{\n{body}}}\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def test_a_small_callee_that_ranks_below_items_filling_the_budget_counts_as_budget_exhausted(tmp_path, monkeypatch):
    import cortex.bundle as bundle_module

    monkeypatch.setattr(bundle_module, "COMPACT_CALLEES_MAX", 0)  # a callee with a compact entry is not a drop (Task 2)
    repo = _step_repo(tmp_path, monkeypatch, fillers=6, body_lines=12)

    expansion = generate_bundle(repo, task="device card reconnect state", budget=1500, output_format="json")["expansion"]

    assert expansion["callees_dropped"]["budget_exhausted"] >= 1, expansion
    assert expansion["callees_dropped"]["too_large"] == 0, expansion
    assert expansion["callees_packed"] >= 1  # each step span fits an empty bundle; the budget went to the filler files
    assert _balanced(expansion), expansion


def test_a_callee_bigger_than_the_per_item_cap_counts_as_too_large_even_with_budget_to_spare(tmp_path, monkeypatch):
    import cortex.bundle as bundle_module

    monkeypatch.setattr(bundle_module, "COMPACT_CALLEES_MAX", 0)  # a callee with a compact entry is not a drop (Task 2)
    repo = _step_repo(tmp_path, monkeypatch, fillers=0, body_lines=40)

    expansion = generate_bundle(repo, task="device card reconnect state", budget=300, output_format="json")["expansion"]

    assert expansion["callees_dropped"]["too_large"] >= 1, expansion
    assert expansion["callees_dropped"]["budget_exhausted"] == 0, expansion
    assert _balanced(expansion), expansion


# --- 0.15.2 Task 2: compact entries for callees that do not fit whole (docs/plan-0.15.2-callee-packing.md) ---


def _fanout_bundle(tmp_path, monkeypatch, budget=3000, *, output_format="json"):
    from evals.run_evals import FIELD_GOLD_TASKS, _build_field_callee_fanout_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    task = next(t for t in FIELD_GOLD_TASKS if t.repo == "field_callee_fanout_repo")
    repo = _build_field_callee_fanout_repo(tmp_path)
    ingest_repository(repo)
    return repo, task, generate_bundle(repo, task=task.description, budget=budget, output_format=output_format)


def test_the_route_comment_is_worded_like_the_compact_why_of_the_mcp_tools():
    from cortex.bundle import _route_comment
    from cortex.mcp.tools import _compact_why

    call = {"from": "symbol:src/model.cpp:onTypeChanged", "hop": 1}
    override = {"from": "symbol:src/model.cpp:onTypeChanged", "hop": 1, "type": "override", "via": "symbol:src/base.cpp:detach"}
    callee = "symbol:src/kinds/text.cpp:detach"

    assert _route_comment(call, callee, "src/kinds/text.cpp") == "// calls: onTypeChanged -> detach"
    assert _route_comment(override, callee, "src/kinds/text.cpp") == "// override: src/base.cpp:detach -> src/kinds/text.cpp:detach"
    assert _route_comment(call, "symbol:app/steps.py:apply", "app/steps.py") == "# calls: onTypeChanged -> apply"
    # The same words as the `why` the MCP tool computes for the item.
    assert _compact_why([{"type": "call", "from": call["from"], "to": callee}]) == _route_comment(call, callee, "x.cpp")[3:]
    assert _compact_why([{"type": "override", "from": override["from"], "via": override["via"], "to": callee}]) == (
        _route_comment(override, callee, "x.cpp")[3:]
    )


def test_a_compact_entry_for_an_override_names_the_base_method_and_keeps_python_comments_hashed():
    from cortex.bundle import ELISION_MARKER, _compact_callee_item
    from cortex.models import GraphNode

    lines = ["#include \"base.h\"", "void TextEntry::detach(int slot) {", "    int cell = slot;", "}"]
    node = GraphNode(
        node_id="symbol:src/kinds/text.cpp:detach", kind="func", label="detach", source_ref="src/kinds/text.cpp",
        span_start=2, span_end=4,
    )
    route = {"from": "symbol:src/model.cpp:onTypeChanged", "hop": 1, "type": "override", "via": "symbol:src/base.cpp:detach"}

    item = _compact_callee_item(node, route, 12.5, "code", lines)

    assert item is not None and item.item_id == "callee-compact:symbol:src/kinds/text.cpp:detach"
    assert item.content == (
        "// override: src/base.cpp:detach -> src/kinds/text.cpp:detach\n"
        f"void TextEntry::detach(int slot) {{\n    {ELISION_MARKER}"
    )
    assert item.score == 12.5 and item.metadata["compact"] is True and item.metadata["call_expansion"] is route
    assert (item.metadata["span_start"], item.metadata["span_end"]) == (2, 4)

    py = GraphNode(node_id="symbol:app/steps.py:apply", kind="func", label="apply", source_ref="app/steps.py", span_start=1, span_end=3)
    py_item = _compact_callee_item(py, {"from": "symbol:app/main.py:run", "hop": 1}, 1.0, "code", ["def apply(x):", "    return x", ""])
    assert py_item is not None and py_item.content.splitlines()[0] == "# calls: run -> apply"

    blank = GraphNode(node_id="symbol:a.cpp:f", kind="func", label="f", source_ref="a.cpp", span_start=1, span_end=1)
    assert _compact_callee_item(blank, route, 1.0, "code", [""]) is None  # no declaration line to show


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
def test_a_compact_entry_is_a_route_comment_a_declaration_and_an_elision_marker(tmp_path, monkeypatch):
    from cortex.bundle import COMPACT_LINES_MAX, ELISION_MARKER

    _repo, _task, bundle = _fanout_bundle(tmp_path, monkeypatch)

    entries = [item for item in bundle["items"] if item["metadata"].get("compact")]
    assert entries
    for item in entries:
        lines = item["content"].splitlines()
        assert lines[0].startswith(("// calls: ", "// override: ")), lines
        assert lines[-1].strip() == ELISION_MARKER
        assert 3 <= len(lines) <= 2 + COMPACT_LINES_MAX
        assert item["item_id"] == f"callee-compact:{item['metadata']['node_id']}"
        assert item["kind"] == "symbol" and item["token_count"] < 40
        assert item["metadata"]["call_expansion"] and item["metadata"]["span_start"] < item["metadata"]["span_end"]


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("budget", [3000, 6000])
def test_compact_entries_come_last_and_the_bundle_stays_consistent(tmp_path, monkeypatch, budget):
    _repo, _task, bundle = _fanout_bundle(tmp_path, monkeypatch, budget)

    items, expansion = bundle["items"], bundle["expansion"]
    flags = [bool(item["metadata"].get("compact")) for item in items]
    assert any(flags) and flags == sorted(flags), "a compact entry sits before a normal item"
    assert expansion["callees_compact"] == sum(flags) <= 24
    assert sum(item["token_count"] for item, flag in zip(items, flags) if flag) <= int(budget * 0.1)
    assert bundle["total_tokens"] == sum(item["token_count"] for item in items) <= budget
    ids = [item["item_id"] for item in items]
    assert len(ids) == len(set(ids))
    assert _balanced(expansion), expansion
    # An entry stands for a callee that is not in the bundle any other way.
    for item in items:
        if item["metadata"].get("compact"):
            others = [other for other in items if other["path"] == item["path"] and not other["metadata"].get("compact")]
            assert not any(other["kind"] == "code" for other in others), item["path"]
            assert not any(
                (other["metadata"].get("span_start") or 0) <= item["metadata"]["span_start"]
                and item["metadata"]["span_end"] <= (other["metadata"].get("span_end") or 0)
                for other in others
            ), item["path"]


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("budget", [3000, 6000])
def test_the_compact_pass_shows_more_callees_and_keeps_the_keyword_files(tmp_path, monkeypatch, budget):
    import cortex.bundle as bundle_module

    repo, task, with_entries = _fanout_bundle(tmp_path, monkeypatch, budget)
    monkeypatch.setattr(bundle_module, "COMPACT_CALLEES_MAX", 0)
    without = generate_bundle(repo, task=task.description, budget=budget, output_format="json")

    shown = with_entries["expansion"]["callees_packed"] + with_entries["expansion"]["callees_compact"]
    assert without["expansion"]["callees_compact"] == 0 and without["expansion"]["callees_packed"] < 12
    assert shown >= 12 and shown > without["expansion"]["callees_packed"]
    assert set(task.expected_files) <= {item["path"] for item in with_entries["items"]}
    assert with_entries["total_tokens"] <= budget and without["total_tokens"] <= budget


def test_a_bundle_whose_share_cannot_hold_three_entries_is_the_bundle_without_the_pass(tmp_path, monkeypatch):
    import cortex.bundle as bundle_module
    from evals.run_evals import COMPANION_GOLD_TASKS, _build_companion_repo

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _build_companion_repo(tmp_path)
    ingest_repository(repo)
    task = COMPANION_GOLD_TASKS[0].description

    for budget in (180, 250):  # 10 percent of these hold fewer than three entries
        bundle = generate_bundle(repo, task=task, budget=budget, output_format="json")
        monkeypatch.setattr(bundle_module, "COMPACT_CALLEES_MAX", 0)
        without = generate_bundle(repo, task=task, budget=budget, output_format="json")
        monkeypatch.undo()
        monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.setenv("CORTEX_SEMANTIC", "0")

        assert [(i["item_id"], i["content"]) for i in bundle["items"]] == [(i["item_id"], i["content"]) for i in without["items"]], budget
        assert bundle["expansion"]["callees_compact"] == 0 and bundle["expansion"] == without["expansion"], budget


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
def test_cortex_query_shows_a_compact_entry_with_its_route_and_flags_it_in_the_detailed_form(tmp_path, monkeypatch):
    import json

    from cortex.mcp.tools import call_tool

    repo, task, _bundle = _fanout_bundle(tmp_path, monkeypatch)
    args = {"repo_path": str(repo), "task": task.description, "budget": 3000}

    concise = json.loads(call_tool("cortex_query", args)["content"][0]["text"])
    detailed = json.loads(call_tool("cortex_query", {**args, "response_format": "detailed"})["content"][0]["text"])

    entries = [item for item in concise["items"] if item["content"].startswith(("// calls: ", "// override: "))]
    assert entries and all(item["why"].startswith(("calls: ", "override: ")) for item in entries)
    assert all("compact" not in item for item in concise["items"])
    assert ", compact " in concise["expansion_summary"]
    flagged = [item for item in detailed["items"] if item.get("compact") is True]
    assert len(flagged) == detailed["expansion"]["callees_compact"] == len(entries)
    assert all("compact" not in item for item in detailed["items"] if not item["metadata"].get("compact"))


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
def test_markdown_renders_a_compact_entry_like_any_other_item(tmp_path, monkeypatch):
    _repo, _task, text = _fanout_bundle(tmp_path, monkeypatch, output_format="markdown")

    assert "### src/steps/applykind0_0.cpp:applyKind0_0\n" in text
    assert "// calls: onTypeChanged -> applyKind0_0\nvoid applyKind0_0(int v) {\n    [body elided] ..." in text
