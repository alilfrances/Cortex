"""The name bonus is proportional to the IDF weight of the task terms a name carries (plan Task 5.8)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cortex import bundle as bundle_mod
from cortex.bundle import NAME_BONUS_BASE, NAME_MATCH_BONUS, SYMBOL_NAME_FILE_SHARE, _name_bonus, generate_bundle
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore

_RETRIEVAL_BUNDLE = bundle_mod.RetrievalBundle


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    return repo


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(path=path, content=content, kind="code", size_bytes=len(content), modified_at=0.0, content_hash=path)


def _file_node(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path)


def _symbol_node(path: str, name: str, start: int, end: int) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}", kind="func", label=name, source_ref=path, granularity="symbol",
        signature=f"def {name}():", span_start=start, span_end=end,
    )


def _candidate_scores(tmp_path: Path, monkeypatch, sources, nodes, task: str, budget: int = 4000) -> dict[str, float]:
    """The score of every candidate `generate_bundle` ranked, by item id."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = _repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])
    captured: dict[str, list] = {}

    def capture(*args, **kwargs):
        captured["candidates"] = list(sys._getframe(1).f_locals["candidates"])
        return _RETRIEVAL_BUNDLE(*args, **kwargs)

    monkeypatch.setattr(bundle_mod, "RetrievalBundle", capture)
    generate_bundle(repo_path=repo, task=task, budget=budget, db_path=db_path, output_format="json")
    return {item.item_id: item.score for item in captured["candidates"]}


def test_name_bonus_scales_with_the_weight_of_the_terms_the_name_carries():
    weights = {"alpha": 1.0, "beta": 1.0, "gamma": 1.0, "delta": 1.0, "omega": 1.0}
    total = sum(weights.values())

    assert _name_bonus(set(), weights, total) == 0.0
    one = _name_bonus({"alpha"}, weights, total)
    assert one == pytest.approx(NAME_MATCH_BONUS * (NAME_BONUS_BASE + (1 - NAME_BONUS_BASE) * 0.2))
    assert NAME_MATCH_BONUS * NAME_BONUS_BASE < one < NAME_MATCH_BONUS * 0.7
    assert _name_bonus(set(weights), weights, total) == pytest.approx(NAME_MATCH_BONUS)
    # a rare term is worth more than a common one
    assert _name_bonus({"rare"}, {"rare": 4.0, "common": 1.0}, 5.0) > _name_bonus({"common"}, {"rare": 4.0, "common": 1.0}, 5.0)
    # the share never exceeds the full bonus (a concatenation can repeat a part's weight)
    assert _name_bonus({"alpha", "beta"}, weights, 1.0) == pytest.approx(NAME_MATCH_BONUS)


def test_a_stem_with_a_rare_term_outranks_a_stem_with_a_common_term(tmp_path, monkeypatch):
    body = "frobnicate flux widget\n" * 3
    # "frobnicate" is in three files, "widget" in eleven: the task names both
    sources = [_source("frobnicate.py", body), _source("widget.py", body), _source("filler.py", body)]
    sources += [_source(f"other{i}.py", f"def f{i}():\n    return 'widget'\n") for i in range(8)]
    scores = _candidate_scores(tmp_path, monkeypatch, sources, [_file_node(source.path) for source in sources], "frobnicate widget")
    assert scores["source:frobnicate.py"] > scores["source:widget.py"] + 5.0
    assert scores["source:widget.py"] > scores["source:filler.py"]  # a common stem hit still beats no hit at all


def test_a_symbol_name_hit_earns_the_file_at_most_the_symbol_share(tmp_path, monkeypatch):
    body = "def frobnicate_flux():\n    return 1\n"
    with_symbol = _candidate_scores(
        tmp_path / "a", monkeypatch, [_source("engine.py", body)], [_file_node("engine.py"), _symbol_node("engine.py", "frobnicate_flux", 1, 2)],
        "frobnicate flux",
    )
    without_symbol = _candidate_scores(
        tmp_path / "b", monkeypatch, [_source("engine.py", body)], [_file_node("engine.py")], "frobnicate flux",
    )
    gain = with_symbol["source:engine.py"] - without_symbol["source:engine.py"]
    # the definition list of the rank fusion adds a point or so on top of the name bonus; a stem hit would add 100
    assert 0 < gain <= SYMBOL_NAME_FILE_SHARE * NAME_MATCH_BONUS + 2.0
    # the span keeps its own bonus, without the file share's halving
    assert with_symbol["symbol-span:symbol:engine.py:frobnicate_flux"] > SYMBOL_NAME_FILE_SHARE * NAME_MATCH_BONUS


def _incident_repo():
    service = "def create_incident():\n    return notify_slack()\n"
    test = "def test_create_incident_notifies_slack():\n    assert create_incident()\n"
    sources = [_source("app/service.py", service), _source("tests/test_service.py", test), _source("app/other.py", "def x():\n    return 1\n")]
    nodes = [
        _file_node("app/service.py"), _file_node("tests/test_service.py"), _file_node("app/other.py"),
        _symbol_node("app/service.py", "create_incident", 1, 2),
        _symbol_node("tests/test_service.py", "test_create_incident_notifies_slack", 1, 2),
    ]
    return sources, nodes


def test_a_test_span_named_after_many_task_terms_ranks_below_the_production_span(tmp_path, monkeypatch):
    sources, nodes = _incident_repo()
    scores = _candidate_scores(tmp_path, monkeypatch, sources, nodes, "create incident notifies slack")
    production = scores["symbol-span:symbol:app/service.py:create_incident"]
    test = scores["symbol-span:symbol:tests/test_service.py:test_create_incident_notifies_slack"]
    assert production > test


def test_a_task_about_tests_does_not_demote_the_test_span(tmp_path, monkeypatch):
    sources, nodes = _incident_repo()
    scores = _candidate_scores(tmp_path, monkeypatch, sources, nodes, "test create incident notifies slack")
    production = scores["symbol-span:symbol:app/service.py:create_incident"]
    test = scores["symbol-span:symbol:tests/test_service.py:test_create_incident_notifies_slack"]
    assert test > production
