from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
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
