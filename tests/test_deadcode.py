from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.models import GraphNode
from cortex.store import CortexStore, default_db_path
from cortex.tokenizer import count_text_tokens
from evals.run_evals import _build_qt_app


def _ingest(repo: Path, data_dir: Path, monkeypatch) -> CortexStore:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data_dir))
    ingest_repository(repo, commit_limit=20)
    return CortexStore(default_db_path(repo))


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_python_dead_code_tiers_and_entry_point_exclusions(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("from .module import exported\n", encoding="utf-8")
    (repo / "pkg" / "module.py").write_text(
        "def unused_high():\n    return 1\n\n"
        "def docs_only():\n    return 2\n\n"
        "def exported():\n    return 3\n\n"
        "def main():\n    return 4\n\n"
        "class Example:\n"
        "    def __init__(self):\n        pass\n\n"
        "    def __str__(self):\n        return 'x'\n\n"
        "@router.get('/route')\n"
        "def decorated_route():\n    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("The docs_only helper is intentionally documented.\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_helpers.py").write_text("def test_fixture_helper():\n    pass\n", encoding="utf-8")
    _init_git(repo)

    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    findings = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]
    by_symbol = {item["symbol"]: item for item in findings}

    assert by_symbol["unused_high"]["confidence"] == "high"
    assert by_symbol["docs_only"]["confidence"] == "medium"
    assert "no grep refs" not in by_symbol["docs_only"]["reason"]
    assert not {"main", "__init__", "__str__", "exported", "decorated_route", "test_fixture_helper"} & by_symbol.keys()


def test_dead_code_confidence_tiering_uses_treesitter_metadata_but_not_regex_fallback(monkeypatch) -> None:
    import cortex.deadcode as deadcode

    class Store:
        def fetch_source_content(self, repo_root: Path, path: str) -> str:
            return ""

    calls: list[str] = []

    def tier(*args, **kwargs):
        calls.append("tiered")
        return "high", "no incoming edges; no grep refs"

    monkeypatch.setattr(deadcode, "_reference_tier", tier)
    treesitter_node = GraphNode(
        node_id="symbol:Foo.swift:orphan",
        kind="func",
        label="orphan",
        source_ref="Foo.swift",
        metadata={"parser_backend": "treesitter"},
    )
    regex_node = GraphNode(
        node_id="symbol:Foo.swift:fallback",
        kind="func",
        label="fallback",
        source_ref="Foo.swift",
        metadata={"parser_backend": "regex"},
    )

    assert deadcode._candidate_reason(Path("repo"), Store(), treesitter_node, [], 2000) == (
        "high",
        "no incoming edges; no grep refs",
    )
    assert calls == ["tiered"]
    assert deadcode._candidate_reason(Path("repo"), Store(), regex_node, [], 2000) == (
        "low",
        "no incoming edges; regex/dynamic-language caveat",
    )
    assert calls == ["tiered"]


def test_qt_dead_code_credits_runtime_edges_and_keeps_orphan_helper(tmp_path: Path, monkeypatch) -> None:
    import cortex.structural.treesitter_backend as treesitter_backend

    monkeypatch.setattr(
        treesitter_backend,
        "extract_treesitter_edges",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("force regex backend")),
    )
    repo = _build_qt_app(tmp_path)
    cpp = repo / "src" / "DeviceManager.cpp"
    cpp.write_text(
        cpp.read_text(encoding="utf-8") + "\nvoid DeviceManager::orphanHelper() {\n    const auto value = 1;\n}\n",
        encoding="utf-8",
    )
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    findings = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]

    assert any(item["symbol"] == "orphanHelper" for item in findings)
    assert not any(item["symbol"] == "deviceConnected" for item in findings)
    assert not any(item["symbol"] == "onDeviceConnected" for item in findings)
    assert not any(item["symbol"] == "DeviceManager" for item in findings)
    assert not any(item["symbol"] in {"onClicked", "onDeviceConnected"} for item in findings)


def test_cortex_dead_code_mcp_returns_findings_and_respects_budget(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def first_unused():\n    return 1\n\n"
        "def second_unused():\n    return 2\n\n"
        "def third_unused():\n    return 3\n",
        encoding="utf-8",
    )
    _init_git(repo)
    store = _ingest(repo, tmp_path / "data", monkeypatch)

    full = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo), "budget": 999999}))
    assert full["findings"]
    assert {"symbol", "file", "line", "confidence", "reason"} <= set(full["findings"][0])

    one_finding = {
        "budget": 1,
        "budget_feasible": True,
        "findings": full["findings"][:1],
        "repo_path": str(repo),
        "returned_count": 1,
        "truncated": False,
    }
    budget = count_text_tokens(json.dumps(one_finding, sort_keys=True))
    capped = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo), "budget": budget}))
    assert len(capped["findings"]) <= 1
    assert capped["returned_count"] == len(capped["findings"])
    assert capped["truncated"] is True


def _many_unused_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def alpha_unused():\n    return 1\n\n"
        "def beta_unused():\n    return 2\n\n"
        "def gamma_unused():\n    return 3\n",
        encoding="utf-8",
    )
    (repo / "b.py").write_text("def delta_unused():\n    return 4\n", encoding="utf-8")
    _init_git(repo)
    return repo


def test_dead_code_lists_repo_files_once_per_analysis(tmp_path: Path, monkeypatch) -> None:
    import cortex.references as references

    repo = _many_unused_repo(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    calls: list[Path] = []
    original = references._iter_candidate_files

    def counting(repo_root: Path, *args, **kwargs):
        calls.append(repo_root)
        return original(repo_root, *args, **kwargs)

    monkeypatch.setattr(references, "_iter_candidate_files", counting)
    findings = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]

    assert len(findings) >= 4
    assert len(calls) == 1


def test_dead_code_should_stop_returns_partial_findings(tmp_path: Path, monkeypatch) -> None:
    repo = _many_unused_repo(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)

    full = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)
    checks: list[int] = []

    def stop_after_two() -> bool:
        checks.append(1)
        return len(checks) > 2

    partial = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges, should_stop=stop_after_two)

    assert full["partial"] is False
    assert partial["partial"] is True
    assert len(partial["findings"]) < len(full["findings"])


def test_python_function_called_only_from_another_module_is_not_a_candidate(tmp_path: Path, monkeypatch) -> None:
    """Task 5: call edges used to end at `name:` endpoints, so only the grep tier
    rescued a cross-module callee. The resolved `symbol:` edge is a real caller."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "helpers.py").write_text("def shared_helper():\n    return 1\n\n\ndef orphan_helper():\n    return 2\n")
    (repo / "runner.py").write_text("from helpers import shared_helper\n\n\ndef run():\n    return shared_helper()\n")
    _init_git(repo)

    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    assert any(e.relation == "calls" and e.target == "symbol:helpers.py:shared_helper" for e in edges)

    by_symbol = {item["symbol"]: item for item in analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]}
    assert "shared_helper" not in by_symbol  # credited by the graph, not merely softened by a grep hit
    assert by_symbol["orphan_helper"]["confidence"] == "high"


def _repo_with_two_dirs(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    (repo / "a" / "one.py").write_text("def only_in_a():\n    return 1\n\n\ndef shared_name():\n    return 2\n")
    (repo / "b" / "two.py").write_text("def only_in_b():\n    return 3\n\n\ndef shared_name():\n    return 4\n")
    (repo / "b" / "use.py").write_text("from a.one import only_in_a\n\n\ndef go():\n    return only_in_a()\n")
    for i in range(30):  # enough files that the corpus polls its stop predicate more than once
        (repo / "b" / f"filler{i}.py").write_text(f"def filler_{i}():\n    return {i}\n")
    _init_git(repo)
    return repo


def test_stop_while_building_the_reference_corpus_gives_a_partial_result_without_it(tmp_path: Path, monkeypatch) -> None:
    from cortex import references

    repo = _repo_with_two_dirs(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    monkeypatch.setattr(references, "_STOP_CHECK_EVERY", 5)
    full = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)
    assert full["partial"] is False and full["findings"]

    polls: list[int] = []

    def stop_inside_the_corpus() -> bool:
        polls.append(1)
        return len(polls) > 2  # the first candidate's corpus is still being read

    stopped = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges, should_stop=stop_inside_the_corpus)

    assert stopped["partial"] is True
    assert stopped["findings"] == []  # the unfinished candidate is not reported
    assert {item["symbol"] for item in stopped["findings"]} <= {item["symbol"] for item in full["findings"]}


def test_scoped_run_equals_the_full_run_filtered_to_the_scope(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_two_dirs(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)

    full = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]
    scoped = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges, path_prefix="b")["findings"]

    assert scoped == [item for item in full if item["file"].startswith("b/")]
    assert any(item["symbol"] == "only_in_b" for item in scoped)


def _resume_in_slices(repo: Path, store: CortexStore, nodes, edges, slice_size: int) -> tuple[list[dict], list[int]]:
    resume = None
    processed: list[int] = []
    for _ in range(10_000):
        polls = [0]

        def stop() -> bool:
            polls[0] += 1
            return polls[0] > slice_size

        result = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges, should_stop=stop, resume=resume)
        if not result["partial"]:
            return result["findings"], processed
        processed.append(result["progress"]["processed"])
        resume = {"cursor": result["cursor"], "findings": result["findings"]}
    raise AssertionError("resuming never finished")


def test_resuming_in_small_slices_equals_one_uninterrupted_run(tmp_path: Path, monkeypatch) -> None:
    repo = _many_unused_repo(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    full = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)

    findings, processed = _resume_in_slices(repo, store, nodes, edges, slice_size=3)

    assert findings == full["findings"]
    assert processed == sorted(processed) and len(set(processed)) == len(processed)  # always advances
    assert len(processed) >= 2


def test_resuming_equals_one_run_on_the_qt_fixture(tmp_path: Path, monkeypatch) -> None:
    repo = _build_qt_app(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    full = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)

    findings, _ = _resume_in_slices(repo, store, nodes, edges, slice_size=4)

    assert findings == full["findings"]


def test_a_partial_result_reports_where_it_stopped(tmp_path: Path, monkeypatch) -> None:
    repo = _many_unused_repo(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    polls = [0]

    def stop() -> bool:
        polls[0] += 1
        return polls[0] > 2

    result = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges, should_stop=stop)

    assert result["partial"] is True
    # (The stop may land inside the first candidate's corpus build; the cursor then names it.)
    assert len(result["cursor"]) == 3 and 0 <= result["progress"]["processed"] < result["progress"]["total"]
    complete = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)
    assert "cursor" not in complete and "progress" not in complete


def test_cmake_targets_are_never_dead_code_candidates(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "CMakeLists.txt").write_text(
        "add_library(core src/a.cpp)\nadd_executable(app src/main.cpp)\ntarget_link_libraries(app core)\n"
    )
    (repo / "src" / "a.cpp").write_text("int orphan_function(int v) {\n    return v + 1;\n}\n")
    (repo / "src" / "main.cpp").write_text("int main() {\n    return 0;\n}\n")
    _init_git(repo)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    assert {node.node_id for node in nodes if node.kind == "target"} == {"target:core", "target:app"}

    findings = analyze_dead_code(repo, store=store, nodes=nodes, edges=edges)["findings"]

    assert not [item for item in findings if item["file"] == "CMakeLists.txt"]
    assert any(item["symbol"] == "orphan_function" for item in findings)


def test_override_index_declares_method_matches_the_full_scan(tmp_path: Path, monkeypatch) -> None:
    from cortex.deadcode import _OverrideIndex

    repo = _build_qt_app(tmp_path)
    store = _ingest(repo, tmp_path / "data", monkeypatch)
    nodes, edges = store.fetch_graph(repo)
    sources = {source.path: source.content for source in store.fetch_sources(repo)}
    index = _OverrideIndex(nodes, edges, sources)

    def old_declares(cls, name, seen, depth=0):  # the scan of every method in the repository
        if cls.node_id in seen or depth > 6:
            return False
        seen.add(cls.node_id)
        for method in index._methods_by_file.get(cls.source_ref, ()):
            if method.label == name and cls.span_start is not None and cls.span_end is not None \
                    and cls.span_start <= (method.span_start or 0) <= cls.span_end:
                return True
        for methods in index._methods_by_file.values():
            for candidate in methods:
                if candidate.label == name and f"{cls.label}::{name}" in (candidate.signature or ""):
                    return True
        return any(old_declares(base, name, seen, depth + 1) for base in index._base_nodes(cls))

    classes = [node for node in nodes if node.granularity == "symbol" and node.kind == "class"]
    names = {node.label for node in nodes if node.granularity == "symbol"}
    assert classes and names
    for cls in classes:
        for name in names:
            assert index._declares_method(cls, name, set()) == old_declares(cls, name, set()), (cls.node_id, name)
