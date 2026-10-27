"""Synthetic fixtures for the 0.13.0 field review, section F (docs/plan-0.14-field-review.md, Task 5.0).

Five small repositories reproduce the query-ranking defects behind the T1-T4 recall misses without any
field code. Each task is run in all four eval modes and asserts the property the review describes; each
property is expected to fail until the Phase 5 task named in its `xfail` reason lands (strict: the mark
must be removed with that task). The last tests guard the fixtures themselves.
"""
from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from cortex.bundle import _match_terms, _tokenize_query, _tokenize_text
from cortex.ingest import ingest_repository
from cortex.mcp.tools import _bundle_why
from cortex.store import CortexStore
from evals.run_evals import (
    FIELD_GOLD_TASKS,
    _build_field_callee_fanout_repo,
    _build_field_ctor_repo,
    _build_field_hub_repo,
    _build_field_override_pure_repo,
    _build_field_override_repo,
    _build_field_seed_markup_calls_repo,
    _build_field_seed_markup_flood_repo,
    _build_field_seed_markup_local_repo,
    _build_field_seed_markup_repo,
    _build_field_tiebreak_repo,
    _build_field_vocab_repo,
    _run_bundle,
    _selected_file_list,
)
from tests.baseline_helpers import fixed_git_env
from tests.golden_utils import TREE_SITTER_ONLY

MODES = ["pagerank", "bfs", "skeleton_on", "skeleton_off"]
BUILDERS = {
    "field_hub_repo": _build_field_hub_repo,
    "field_tiebreak_repo": _build_field_tiebreak_repo,
    "field_override_repo": _build_field_override_repo,
    "field_override_pure_repo": _build_field_override_pure_repo,
    "field_ctor_repo": _build_field_ctor_repo,
    "field_vocab_repo": _build_field_vocab_repo,
    "field_seed_markup_repo": _build_field_seed_markup_repo,
    "field_seed_markup_calls_repo": _build_field_seed_markup_calls_repo,
    "field_seed_markup_local_repo": _build_field_seed_markup_local_repo,
    "field_seed_markup_flood_repo": _build_field_seed_markup_flood_repo,
    "field_callee_fanout_repo": _build_field_callee_fanout_repo,
}
TASKS = {task.repo: task for task in FIELD_GOLD_TASKS}


@pytest.fixture(scope="module")
def _field_built(tmp_path_factory):
    """The eleven fixture repositories (the override chain in two variants, the seed markup in five), built and indexed once per module (eval settings: 20 commits)."""
    root = tmp_path_factory.mktemp("field-tasks")
    data_dir = root / "data"
    built: dict[str, tuple[Path, Path]] = {}
    with pytest.MonkeyPatch.context() as patch:
        fixed_git_env(patch)
        patch.setenv("CORTEX_DATA_DIR", str(data_dir))
        patch.setenv("CORTEX_SEMANTIC", "0")
        for name, build in BUILDERS.items():
            repo = build(root)
            db_path = root / f"{name}.db"
            ingest_repository(repo, commit_limit=20, db_path=db_path)
            built[name] = (repo, db_path)
    return built, data_dir


@pytest.fixture
def field(_field_built, monkeypatch):
    built, data_dir = _field_built
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data_dir))
    return built


def _run(field, name: str, mode: str, budget: int | None = None) -> tuple[dict, list[str]]:
    repo, db_path = field[name]
    task = TASKS[name]
    if budget is not None:
        task = dataclasses.replace(task, budget=budget, tight_budget=budget)
    bundle = _run_bundle(repo, task, mode, db_path)["bundle"]
    return bundle, _selected_file_list(bundle["items"])


def _items(bundle: dict, path: str) -> list[dict]:
    return [item for item in bundle["items"] if item.get("path") == path]


# --- hub flood: causes 1-3 (saturated name bonus, no length normalisation, spans spill at the file's score)


# Task 5.8 (proportional name bonus) ranks the gold file first and packs it in every mode but skeleton_off,
# where it stays out at both budgets; Task 5.9 (the per-file span share scales with the matching files) packs it at 4000 there too.
# At 900 the six hub spans that outscore the 81-token gold file used to leave 22 tokens, which only a skeleton
# (the mode without them) could use; the per-file span decay (R3) lets the gold file rank above a hub's second span.
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("budget", [900, 4000])
def test_hub_flood_leaves_the_small_gold_file_in_the_bundle(field, mode, budget):
    task = TASKS["field_hub_repo"]
    bundle, files = _run(field, "field_hub_repo", mode, budget)

    hubs = [path for path in files if path.startswith("src/core_")]
    assert hubs, files  # the four hubs do match; the question is whether they take everything
    assert set(task.expected_files) <= set(files), f"hub spans filled the bundle: {sorted(set(files))}"


# The fixture was tuned on seed 5023 (the generated words and method names decide how many hub spans outscore the
# 81-token gold file); the same repository from neighbouring seeds shows the fix is not a property of that one draw.
@pytest.mark.parametrize("seed", range(5020, 5028))
def test_hub_flood_gold_file_is_packed_for_every_seed(seed, tmp_path, monkeypatch):
    fixed_git_env(monkeypatch)
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    repo = _build_field_hub_repo(tmp_path, seed=seed)
    db_path = tmp_path / "hub.db"
    ingest_repository(repo, commit_limit=20, db_path=db_path)

    task = TASKS["field_hub_repo"]
    missing = []
    for mode in MODES:
        for budget in (900, 4000):
            bundle = _run_bundle(repo, dataclasses.replace(task, budget=budget, tight_budget=budget), mode, db_path)["bundle"]
            if not set(task.expected_files) <= set(_selected_file_list(bundle["items"])):
                missing.append(f"{mode}@{budget}")
    assert not missing, f"seed {seed}: gold file not packed in {missing}"


# --- tie-break: cause 4 (callee scores are flat, ties break on path)


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("mode", MODES)
def test_tied_callees_do_not_hide_the_relevant_one(field, mode):
    bundle, files = _run(field, "field_tiebreak_repo", mode)

    tied = [path for path in files if path.startswith("a/")]
    assert tied, files  # the one-line virtuals under a/ do tie at the seed's score and fill the bundle
    assert "z/ops/resetop.cpp" in files, f"{len(tied)} paths under a/ won the tie on path"


# --- override chain: cause 6 (overriders were offered but never walked)


# `field_override_pure_repo`: the base method is `= 0` (the field's shape); the tree-sitter extractor
# used to emit no symbol for it, so the call stayed `name:` and the override walk never started.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("repo_name", ["field_override_repo", "field_override_pure_repo"])
@pytest.mark.parametrize("mode", MODES)
def test_the_override_chain_reaches_the_accessory(field, repo_name, mode):
    bundle, files = _run(field, repo_name, mode)

    assert "src/secondmanager.cpp" in files, files
    second = _bundle_why(_items(bundle, "src/secondmanager.cpp")[0], set(), set(), [])
    overrides = [entry for entry in second if entry["type"] == "override"]
    assert overrides, second
    assert overrides[0]["via"] == "symbol:src/basemanager.h:createForCompanion"
    assert overrides[0]["to"] == "symbol:src/secondmanager.cpp:createForCompanion"

    assert "src/accessory.cpp" in files, f"the overrider's callee was never reached: {files}"
    accessory = _bundle_why(_items(bundle, "src/accessory.cpp")[0], set(), set(), [])
    calls = [entry for entry in accessory if entry["type"] == "call"]
    assert calls, accessory
    assert calls[0]["from"] == "symbol:src/secondmanager.cpp:createForCompanion"
    assert calls[0]["to"] == "symbol:src/accessory.cpp:assignCompanion"


# --- constructor flood: cause 5 (constructors seed)


# skeleton_off keeps the room that skeletons take in the other modes; one of the hub file's 100 call neighbours
# used to fit into it (helper000.cpp, the first by path, at the flat file-level CALL_EXPANSION_BONUS). The
# relative score floor (R3) keeps a file whose score is a tenth of the best item's out of the bundle.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("mode", MODES)
def test_a_hub_constructor_does_not_flood_the_bundle_with_its_helpers(field, mode):
    task = TASKS["field_ctor_repo"]
    bundle, files = _run(field, "field_ctor_repo", mode)
    terms = _tokenize_query(task.description)

    assert set(task.expected_files) <= set(files), files  # the hub itself is packed
    helpers = [path for path in dict.fromkeys(files) if path.startswith("src/helpers/")]
    unrelated = [path for path in helpers if not terms & _tokenize_text(Path(path).stem)]
    assert not unrelated, f"helpers without a task term were packed: {unrelated}"


# --- vocabulary: cause 8 (tokeniser noise, no stemming)


@pytest.mark.parametrize("mode", MODES)
def test_the_vocabulary_of_the_task_reaches_the_gizmo_file(field, mode):
    task = TASKS["field_vocab_repo"]
    terms, _concatenations = _match_terms(task.description)  # the stemmed terms that meet the stemmed file text
    bundle, files = _run(field, "field_vocab_repo", mode)

    assert not {"os", "re"} & terms, sorted(terms)  # `iOS` and `re-enumerates` must not split into these
    assert "gizmo" in terms, sorted(terms)  # `Gizmos` must match the code's `Gizmo`
    assert set(task.expected_files) <= set(files), files


# --- call-expansion seeds: the seeds went to small UI spans that reach nothing (section 1.4)


# Six small QML screens are dense with the task's words, so their spans outscore the large model file's
# `onTypeChanged` on raw text, and the model file loses on length-normalised score. Seeds are picked from the
# best files by the normalised score, then the best spans in them: the screens' spans took every seed, and
# they have no resolved call (`field_seed_markup_repo`) or only a same-file helper (`..._calls_repo`), so the
# walk from the seeds returned nothing and the virtual `detach` with its two overriders was never reached.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("repo_name", ["field_seed_markup_repo", "field_seed_markup_calls_repo"])
@pytest.mark.parametrize("mode", MODES)
def test_the_seed_that_can_reach_a_callee_is_expanded(field, repo_name, mode):
    bundle, files = _run(field, repo_name, mode)

    expanded = [item for item in bundle["items"] if (item.get("metadata") or {}).get("call_expansion")]
    from_model = [
        item for item in expanded
        if item["metadata"]["call_expansion"]["from"].startswith("symbol:src/itemmodel.cpp:")
    ]
    assert from_model, f"no call-expansion item starts from src/itemmodel.cpp: {files}"
    packed = {item["path"] for item in expanded if (item["metadata"]["call_expansion"].get("type") == "override")}
    assert packed & {"src/kinds/textentry.cpp", "src/kinds/imageentry.cpp"}, f"no overrider packed through an override route: {sorted(packed)}"


# Ten screens with four functions each, every one calling a same-file helper: a QML same-file call resolves to an
# `external:qml:<screen>:hideItem` node, so each span "has an outgoing call" and passed the 0.15.0 filter, while the
# walk from it finds nothing in another file. Their spans score above `onTypeChanged` (118.6 against a much smaller
# model file), so all six seed slots went to screen spans and no call-expansion item started from the model file.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("mode", MODES)
def test_a_span_whose_calls_stay_in_its_file_does_not_take_a_seed_slot(field, mode):
    bundle, files = _run(field, "field_seed_markup_local_repo", mode)

    expanded = [item for item in bundle["items"] if (item.get("metadata") or {}).get("call_expansion")]
    from_model = [item for item in expanded if item["metadata"]["call_expansion"]["from"].startswith("symbol:src/itemmodel.cpp:")]
    seeds = [seed["id"] for seed in bundle["expansion"]["seeds"]]
    assert from_model, f"no call-expansion item starts from src/itemmodel.cpp; seeds {seeds}; refusals {bundle['expansion']['seed_refusals']}"
    assert any(seed.startswith("symbol:src/itemmodel.cpp:") for seed in seeds)


# Twelve large screens named after the task outscore the three callees of `onTypeChanged` (which score from their seed,
# x0.9 per hop) and fill the 1500-token budget: the walk computes `detach` and its two overriders and `pack` has no
# room left when their turn comes. A bundle that cannot hold one of them cannot show the cause the walk found.
# Reserving a share of the budget for the best call-expansion spans in every bundle lowered the mean precision@3 of the
# eval gate (Task 4, dropped), so the reservation runs only when the normal passes packed no computed callee (Task 4b).
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("mode", MODES)
def test_computed_callees_get_a_share_of_a_tight_budget(field, mode):
    bundle, files = _run(field, "field_seed_markup_flood_repo", mode)

    expansion = bundle["expansion"]
    assert expansion["callees_computed"] > 0, expansion  # the walk found them; the rest is about packing
    expanded = [item for item in bundle["items"] if (item.get("metadata") or {}).get("call_expansion")]
    assert expansion["callees_packed"] > 0 and expanded, f"computed callees, none packed: {expansion}"


# `onTypeChanged` reaches twenty functions in twenty files of their own (and the virtual with two overriders); twelve large
# screens named after the task fill the budget first, so on 0.15.1 the whole bodies are dropped for `budget_exhausted` (20 of
# 23; the three the reservation of Task 4b of 0.15.1 packs are all it shows). A compact entry (declaration line and route)
# lets the bundle show the call structure across the callee files without pushing the keyword files out.
# The reservation for compact entries used to cost the bundle the seed's own file at these budgets (and at 3000 on the
# CI runner): its callees score from the seed's span and outscore the file around it. A file the bundle holds without
# compact entries must stay when they are on.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("budget", [1800, 2300, 3000, 3700, 3800, 4200])
def test_compact_entries_never_cost_the_bundle_a_seed_file(field, monkeypatch, budget):
    import cortex.bundle as bundle_module

    task = TASKS["field_callee_fanout_repo"]
    bundle, files = _run(field, "field_callee_fanout_repo", "pagerank", budget)
    monkeypatch.setattr(bundle_module, "COMPACT_CALLEES_MAX", 0)
    _plain, plain_files = _run(field, "field_callee_fanout_repo", "pagerank", budget)

    seed_paths = {seed["path"] for seed in bundle["expansion"]["seeds"]}
    assert seed_paths & set(plain_files) <= set(files), (budget, sorted(seed_paths), sorted(set(files)))
    assert set(task.expected_files) & set(plain_files) <= set(files), (budget, sorted(set(files)))
    assert bundle["total_tokens"] <= budget


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("budget", [3000, 6000])
@pytest.mark.parametrize("mode", MODES)
def test_callees_that_do_not_fit_whole_still_show_the_call_structure(field, mode, budget):
    task = TASKS["field_callee_fanout_repo"]
    bundle, files = _run(field, "field_callee_fanout_repo", mode, budget)

    expansion = bundle["expansion"]
    assert expansion["callees_computed"] >= 20, expansion
    shown = expansion["callees_packed"] + expansion.get("callees_compact", 0)
    assert shown >= 12, f"{shown} of {expansion['callees_computed']} callees shown: {expansion}"
    routes = [(item.get("metadata") or {}).get("call_expansion") or {} for item in bundle["items"]]
    assert any(route.get("type") == "override" for route in routes), "no override route among the items"
    assert set(task.expected_files) <= set(files), f"the keyword files left the bundle: {sorted(set(files))}"


# The reservation is a second packing, run only when the first packed no computed callee. These two tests pin that:
# a bundle that already holds a callee is the first packing's, and a repacked one is a consistent bundle.
@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("repo_name", ["field_seed_markup_repo", "field_seed_markup_calls_repo"])
@pytest.mark.parametrize("mode", MODES)
def test_a_bundle_that_already_packs_a_callee_is_not_repacked(field, monkeypatch, repo_name, mode):
    import cortex.bundle as bundle_module

    bundle, _files = _run(field, repo_name, mode)
    assert bundle["expansion"]["callees_packed"] > 0, bundle["expansion"]  # the premise
    assert bundle["expansion"]["reserved"] is False

    monkeypatch.setattr(bundle_module, "EXPANSION_RESERVED_ITEMS", 0)
    without, _files = _run(field, repo_name, mode)

    assert [(item["item_id"], item["content"]) for item in bundle["items"]] == [(item["item_id"], item["content"]) for item in without["items"]]
    assert bundle["total_tokens"] == without["total_tokens"]
    assert without["expansion"] == bundle["expansion"]


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
@pytest.mark.parametrize("mode", MODES)
def test_the_repacked_bundle_is_a_consistent_bundle(field, monkeypatch, mode):
    import cortex.bundle as bundle_module

    bundle, _files = _run(field, "field_seed_markup_flood_repo", mode)
    expansion = bundle["expansion"]
    assert expansion["reserved"] is True, expansion
    assert 0 < expansion["callees_packed"] <= expansion["callees_computed"]
    ids = [item["item_id"] for item in bundle["items"]]
    assert len(ids) == len(set(ids)), "an item was selected twice"
    assert bundle["total_tokens"] == sum(item["token_count"] for item in bundle["items"])
    assert bundle["total_tokens"] <= bundle["budget"]

    monkeypatch.setattr(bundle_module, "EXPANSION_RESERVED_ITEMS", 0)  # the first packing alone: no callee, as on 0.15.1 Task 3
    first_only, _files = _run(field, "field_seed_markup_flood_repo", mode)
    assert first_only["expansion"]["callees_packed"] == 0 and first_only["expansion"]["reserved"] is False


# --- the fixtures themselves


@TREE_SITTER_ONLY  # resolved calls: the regex fallback emits name-only call edges
def test_the_fixtures_resolve_the_call_chains_the_tasks_depend_on(_field_built):
    built, _data_dir = _field_built

    def graph(name: str):
        repo, db_path = built[name]
        nodes, edges = CortexStore(db_path).fetch_graph(repo)
        return nodes, {(e.relation, e.source, e.target) for e in edges}

    _nodes, edges = graph("field_tiebreak_repo")
    stage_calls = {t for r, s, t in edges if r == "calls" and s.startswith("symbol:src/sequencer.cpp:stage") and t.startswith("symbol:a/v")}
    assert len(stage_calls) == 30
    assert ("calls", "symbol:src/sequencer.cpp:stage9", "symbol:z/ops/resetop.cpp:execute") in edges
    assert sum(1 for r, s, t in edges if r == "inherits" and s.startswith("symbol:a/w") and t.startswith("symbol:a/v")) == 30

    for name in ("field_override_repo", "field_override_pure_repo"):
        _nodes, edges = graph(name)
        assert ("calls", "symbol:src/manager.cpp:onReattach", "symbol:src/basemanager.h:createForCompanion") in edges, name
        assert ("inherits", "symbol:src/secondmanager.h:SecondManager", "symbol:src/basemanager.h:BaseManager") in edges, name
        assert ("calls", "symbol:src/secondmanager.cpp:createForCompanion", "symbol:src/accessory.cpp:assignCompanion") in edges, name

    nodes, edges = graph("field_ctor_repo")
    constructor = next(n for n in nodes if n.node_id == "symbol:src/hub_controller.cpp:HubController")
    assert constructor.span_end - constructor.span_start + 1 >= 300
    resolved = {t for r, s, t in edges if r == "calls" and s == constructor.node_id and t.startswith("symbol:src/helpers/")}
    assert len(resolved) == 100

    for name, calls_helper in (("field_seed_markup_repo", False), ("field_seed_markup_calls_repo", True)):
        _nodes, edges = graph(name)
        assert ("calls", "symbol:src/itemmodel.cpp:onTypeChanged", "symbol:src/baseitem.cpp:detach") in edges, name
        assert sum(1 for r, s, t in edges if r == "inherits" and s.startswith("symbol:src/kinds/") and t == "symbol:src/baseitem.h:BaseItem") == 2, name
        screen_calls = {t for r, s, t in edges if r == "calls" and s.startswith("symbol:ui/ItemScreen")}
        assert bool(screen_calls) == calls_helper, (name, screen_calls)  # the screens of the plain variant call nothing


    # The local variant: the screens call only a same-file helper (an `external:qml:` node), the model file calls `detach`.
    _nodes, edges = graph("field_seed_markup_local_repo")
    assert ("calls", "symbol:src/itemmodel.cpp:onTypeChanged", "symbol:src/baseitem.cpp:detach") in edges
    screen_calls = {t for r, s, t in edges if r == "calls" and s.startswith("symbol:ui/ItemScreen")}
    assert screen_calls and all(t.startswith("external:qml:ui/ItemScreen") for t in screen_calls), screen_calls


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def test_the_fixtures_are_deterministic_and_small(_field_built, tmp_path, monkeypatch):
    built, _data_dir = _field_built
    fixed_git_env(monkeypatch)
    total = 0
    for name, build in BUILDERS.items():
        first = _tree_digest(built[name][0])
        second = _tree_digest(build(tmp_path))
        assert first == second, name
        total += sum(path.stat().st_size for path in built[name][0].rglob("*") if path.is_file() and ".git" not in path.parts)

    assert total < 2_000_000, total
