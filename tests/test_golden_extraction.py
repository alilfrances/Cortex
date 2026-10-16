"""Golden snapshots of structural extraction at 7b0e6e0 (Task 0 baseline).

Every file of ``tests/fixtures/multilang_repo`` and ``tests/fixtures/qml_full_repo``
is run through ``cortex.graph.build_file_layer`` once per backend and the full
node/edge set is compared with a checked-in golden:

* ``tests/goldens/extraction/treesitter/<fixture>/<file>.json`` -- skipped when the
  managed runtime is not ready or ``CORTEX_FORCE_REGEX=1``.
* ``tests/goldens/extraction/regex/<fixture>/<file>.json`` -- always runs; the test
  sets ``CORTEX_FORCE_REGEX=1`` itself.

Also pinned: the Python (stdlib ``ast``) output for the ``python_app`` eval fixture,
and the full ``build_graph`` result (degree metadata included) for the Qt eval
fixture, per backend.

The ``test_*_today`` assertions document *current limitations* so that a later
task flipping them does so visibly and intentionally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.ast_extract import extract_python_edges
from cortex.graph import build_file_layer, build_graph
from tests.baseline_helpers import (
    FIXTURES,
    fixed_git_env,
    fixture_files,
    init_git,
    normalised_commits,
    read_sources,
)
from tests.golden_utils import TREE_SITTER_ONLY, assert_golden, canonical_graph
from evals.run_evals import _build_python_app, _build_qt_app

# `calls_*` fixtures are call-specific additions (Tasks 6-8): the two original fixtures
# are frozen in tests/goldens/extraction/baseline-7b0e6e0 and may only gain `calls` edges
# (see tests/test_additive_baseline.py), so call sites live in their own fixtures.
FIXTURE_NAMES = ("multilang_repo", "qml_full_repo", "calls_cpp")

# Always extracted through the regex-based module-metadata path, in both backends.
_METADATA_ONLY_NAMES = {"cmakelists.txt", "qmldir"}
_METADATA_ONLY_SUFFIXES = {".cmake", ".qrc", ".qmltypes", ".qmlproject"}

CASES = [
    pytest.param(name, rel, id=f"{name}/{rel}")
    for name in FIXTURE_NAMES
    for rel in fixture_files(FIXTURES / name)
]


def _is_metadata_only(rel: str) -> bool:
    path = Path(rel)
    return path.name.lower() in _METADATA_ONLY_NAMES or path.suffix.lower() in _METADATA_ONLY_SUFFIXES


def _layer(fixture: str, rel: str):
    root = FIXTURES / fixture
    known = set(fixture_files(root))
    sources = read_sources(root, [rel])
    return build_file_layer(sources, known_paths=known)


def _whole_fixture_layer(fixture: str):
    root = FIXTURES / fixture
    rels = fixture_files(root)
    return build_file_layer(read_sources(root, rels), known_paths=set(rels))


def _backends_of(nodes) -> set[str]:
    return {
        str(node.metadata["parser_backend"])
        for node in nodes
        if node.kind == "file" and "parser_backend" in node.metadata
    }


# --------------------------------------------------------------------------- #
# per-file goldens
# --------------------------------------------------------------------------- #


@TREE_SITTER_ONLY
@pytest.mark.parametrize("fixture,rel", CASES)
def test_treesitter_extraction_golden(fixture: str, rel: str) -> None:
    nodes, edges = _layer(fixture, rel)

    if not _is_metadata_only(rel) and Path(rel).suffix in {".java", ".js", ".ts", ".go", ".rs", ".rb", ".c", ".cpp", ".hpp", ".h", ".qml"}:
        # A silent fallback would otherwise bake regex output into the
        # tree-sitter golden.
        assert _backends_of(nodes) == {"treesitter"}, (fixture, rel, _backends_of(nodes))
    assert_golden(f"extraction/treesitter/{fixture}/{rel}.json", canonical_graph(nodes, edges))


@pytest.mark.parametrize("fixture,rel", CASES)
def test_regex_extraction_golden(fixture: str, rel: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    nodes, edges = _layer(fixture, rel)

    assert _backends_of(nodes) <= {"regex"}, (fixture, rel, _backends_of(nodes))
    assert_golden(f"extraction/regex/{fixture}/{rel}.json", canonical_graph(nodes, edges))


@TREE_SITTER_ONLY
@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_treesitter_whole_fixture_golden(fixture: str) -> None:
    """Cross-file resolution passes (Qt emits/connects/handles, QML) over a full batch."""
    nodes, edges = _whole_fixture_layer(fixture)
    assert_golden(f"extraction/treesitter/{fixture}/__whole_fixture__.json", canonical_graph(nodes, edges))


@pytest.mark.parametrize("fixture", FIXTURE_NAMES)
def test_regex_whole_fixture_golden(fixture: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    nodes, edges = _whole_fixture_layer(fixture)
    assert_golden(f"extraction/regex/{fixture}/__whole_fixture__.json", canonical_graph(nodes, edges))


# --------------------------------------------------------------------------- #
# Python (stdlib ast) goldens
# --------------------------------------------------------------------------- #


def _python_app_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[str]]:
    fixed_git_env(monkeypatch)
    repo = _build_python_app(tmp_path)
    rels = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*.py"))
    return repo, rels


def _python_extraction(repo: Path, rels: list[str]) -> dict[str, tuple[list, list]]:
    known = {p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    return {
        rel: extract_python_edges(rel, (repo / rel).read_text(encoding="utf-8"), known)
        for rel in rels
    }


def test_python_extraction_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, rels = _python_app_files(tmp_path, monkeypatch)
    assert rels, "python_app fixture produced no .py files"
    for rel, (nodes, edges) in _python_extraction(repo, rels).items():
        assert_golden(f"extraction/python/{rel}.json", canonical_graph(nodes, edges))


# --------------------------------------------------------------------------- #
# build_graph (nodes + edges + degree metadata) for the Qt eval fixture
# --------------------------------------------------------------------------- #


def _qt_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fixed_git_env(monkeypatch)
    repo = _build_qt_app(tmp_path)
    rels = sorted(
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    sources = read_sources(repo, rels)
    commits = normalised_commits(repo)
    assert len(commits) == 2, "qt_app fixture is expected to have two commits"
    return build_graph(sources, commits)


@TREE_SITTER_ONLY
def test_treesitter_qt_app_build_graph_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    nodes, edges = _qt_graph(tmp_path, monkeypatch)
    assert all("degree" in node.metadata for node in nodes)
    assert_golden("graph/treesitter/qt_app.json", canonical_graph(nodes, edges))


def test_regex_qt_app_build_graph_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    nodes, edges = _qt_graph(tmp_path, monkeypatch)
    assert all("degree" in node.metadata for node in nodes)
    assert_golden("graph/regex/qt_app.json", canonical_graph(nodes, edges))


# --------------------------------------------------------------------------- #
# Characterization of today's limitations (flipped deliberately by later tasks)
# --------------------------------------------------------------------------- #


def _calls(edges) -> list:
    return [edge for edge in edges if edge.relation == "calls"]


# Families whose tree-sitter and regex extractors emit `calls` edges (flipped per task).
# Task 6: C/C++.  Tasks 7/8 extend this set and finally retire the "none today" tests.
_CALL_FAMILY_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}


def _non_python_non_qml_cases():
    return [
        c for c in CASES
        if Path(c.values[1]).suffix not in {".py", ".qml"} | _CALL_FAMILY_SUFFIXES
    ]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("fixture,rel", _non_python_non_qml_cases())
def test_no_calls_edges_outside_python_and_qml_treesitter_today(fixture: str, rel: str) -> None:
    """Task 6 flipped C/C++; Tasks 7-8 flip JS/TS/Go/Rust/Swift/Java/Ruby."""
    _, edges = _layer(fixture, rel)
    assert _calls(edges) == []


@pytest.mark.parametrize("fixture,rel", _non_python_non_qml_cases())
def test_no_calls_edges_outside_python_and_qml_regex_today(
    fixture: str, rel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 6-8 flip: the regex fallback gains name-only `calls` edges."""
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    _, edges = _layer(fixture, rel)
    assert _calls(edges) == []


CALL_CONTRACT_KEYS = {
    "lineno", "source_file", "callee", "receiver", "receiver_kind", "scope", "receiver_type", "enclosing_class",
}


def test_python_calls_follow_the_call_metadata_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task 5 flip (was: "all Python `calls` targets start with `name:`").

    Per-file extraction still emits `name:<callee>` -- resolution is a repo-wide
    pass -- but every call edge now carries the full call-site metadata
    contract, and the resolved `build_graph` rewrites targets to symbol ids."""
    repo, rels = _python_app_files(tmp_path, monkeypatch)
    calls = [
        edge
        for nodes_edges in _python_extraction(repo, rels).values()
        for edge in _calls(nodes_edges[1])
    ]
    assert calls, "python_app fixture is expected to contain call sites"
    assert all(edge.layer == "STRUCTURAL" and edge.relation == "calls" for edge in calls)
    assert all(CALL_CONTRACT_KEYS <= set(edge.metadata) for edge in calls)
    assert all(edge.target == f"name:{edge.metadata['callee']}" for edge in calls)
    assert len({edge.edge_id for edge in calls}) == len(calls)

    sources = read_sources(repo, rels)
    _, graph_edges = build_graph(sources, [])
    resolved = {(e.source, e.target) for e in _calls(graph_edges) if e.target.startswith("symbol:")}
    assert ("symbol:app/cleanup.py:cleanup_sessions", "symbol:app/session.py:SessionStore.prune_expired") in resolved
    assert ("symbol:app/payments.py:PaymentGateway.capture", "symbol:app/retry.py:retry_operation") in resolved


def test_python_app_build_graph_golden(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full `build_graph` (resolved call targets, resolution tiers, degree) for the Python eval fixture."""
    repo, rels = _python_app_files(tmp_path, monkeypatch)
    sources = read_sources(repo, rels)
    nodes, edges = build_graph(sources, normalised_commits(repo))
    assert_golden("graph/python_app.json", canonical_graph(nodes, edges))


@TREE_SITTER_ONLY
def test_parser_backend_label_treesitter() -> None:
    nodes, _ = _layer("multilang_repo", "app.js")
    structural = [node for node in nodes if node.kind != "file"]
    assert structural, "app.js should yield symbol nodes"
    assert {node.metadata["parser_backend"] for node in structural} == {"treesitter"}
    assert _backends_of(nodes) == {"treesitter"}


def test_parser_backend_label_regex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    nodes, edges = _layer("multilang_repo", "app.js")
    structural = [node for node in nodes if node.kind != "file"]
    assert structural
    assert {node.metadata["parser_backend"] for node in structural} == {"regex"}
    assert _backends_of(nodes) == {"regex"}
    # The dispatcher forces LOW confidence on every fallback edge.
    structural_edges = [e for e in edges if e.layer == "STRUCTURAL"]
    assert structural_edges and {e.confidence for e in structural_edges} == {"LOW"}


@TREE_SITTER_ONLY
def test_treesitter_dedupes_same_named_symbols_per_file_today() -> None:
    """Non-goal documented by the plan: symbol ids are ``symbol:<path>:<bare name>``."""
    nodes, _ = _layer("multilang_repo", "engine.cpp")
    symbol_ids = [node.node_id for node in nodes if node.node_id.startswith("symbol:")]
    assert len(symbol_ids) == len(set(symbol_ids))
    assert all(sid.startswith("symbol:engine.cpp:") for sid in symbol_ids)


# Files of the call-specific fixtures that contain call sites (declaration-only files do not).
_CALLING_FILES = {"calls_cpp": {"app.cpp", "util.cpp", "worker.hpp", "diamond.cpp"}}


def _code_call_cases():
    return [
        c for c in CASES
        if Path(c.values[1]).suffix in _CALL_FAMILY_SUFFIXES and c.values[0].startswith("calls_")
    ]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("fixture,rel", _code_call_cases())
def test_call_fixture_files_with_call_sites_have_calls_treesitter(fixture: str, rel: str) -> None:
    """Task 6 flip: every `calls_*` C/C++ file with a call site has >= 1 `calls` edge."""
    if rel not in _CALLING_FILES[fixture]:
        pytest.skip("no call site in this file")
    _, edges = _layer(fixture, rel)
    calls = _calls(edges)
    assert calls and all(CALL_CONTRACT_KEYS <= set(e.metadata) for e in calls)
    assert len({e.edge_id for e in calls}) == len(calls)


@pytest.mark.parametrize("fixture,rel", _code_call_cases())
def test_call_fixture_files_regex_calls_are_name_only_low(
    fixture: str, rel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    _, edges = _layer(fixture, rel)
    for edge in _calls(edges):
        assert edge.confidence == "LOW" and edge.target == f"name:{edge.metadata['callee']}"
        assert edge.metadata["receiver_kind"] == "none" and edge.metadata["name_only"] is True
        assert edge.metadata["receiver_type"] is None and edge.metadata["scope"] is None
