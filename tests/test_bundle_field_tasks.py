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
    _build_field_ctor_repo,
    _build_field_hub_repo,
    _build_field_override_pure_repo,
    _build_field_override_repo,
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
}
TASKS = {task.repo: task for task in FIELD_GOLD_TASKS}


@pytest.fixture(scope="module")
def _field_built(tmp_path_factory):
    """The six fixture repositories (the override chain in two variants), built and indexed once per module (eval settings: 20 commits)."""
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
