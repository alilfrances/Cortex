from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex.bundle import CALL_EXPANSION_BONUS, generate_bundle
from cortex.ingest import ingest_repository

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

    assert got["symbol:b.cpp:direct"] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)
    assert got["symbol:b.cpp:sibling"][2] == 1
    assert got["symbol:c.cpp:far"] == (100.0 * CALLEE_HOP_SHARE[1], "symbol:a.cpp:top", 2)
    assert "symbol:d.cpp:too_far" not in got
    assert "symbol:a.cpp:top" not in got


def test_walk_keeps_the_best_route_over_several_seeds_and_never_expands_callers() -> None:
    calls = {"symbol:a.cpp:s1": ["symbol:x.cpp:shared"], "symbol:b.cpp:s2": ["symbol:x.cpp:shared"]}
    got = _walk([("symbol:a.cpp:s1", 80.0), ("symbol:b.cpp:s2", 120.0)], calls)

    assert got["symbol:x.cpp:shared"][1:] == ("symbol:b.cpp:s2", 1)
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

    assert got == {"symbol:base.h:Base": (100.0 * BASE_CLASS_SHARE, "symbol:a.cpp:method", 1)}


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

    assert got["symbol:x.cpp:deep"] == (100.0 * CALLEE_HOP_SHARE[0], "symbol:a.cpp:top", 1)


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
