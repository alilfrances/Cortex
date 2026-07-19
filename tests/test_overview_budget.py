from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path
from cortex.tokenizer import count_text_tokens


BUDGETED_KEYS = {
    "repo_path",
    "report",
    "top_hotspots",
    "budget",
    "budget_feasible",
    "truncated",
    "dead_code_total",
    "dead_code_returned",
    "budget_note",
    "semantic",
    "language_runtime",
}


def _git_init(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Cortex Test"], cwd=repo, check=True, capture_output=True)


def _indexed_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


def _analysis() -> dict:
    # Keep this deliberately synthetic and deterministic: exercising the
    # presentation policy must not require 852 slow reference scans.
    findings = [
        {
            "symbol": f"{tier}_{index:03d}",
            "file": f"src/{tier}_{index:03d}.py",
            "line": index + 1,
            "confidence": tier,
            "reason": f"synthetic {tier} finding",
        }
        for tier, total in (("high", 300), ("medium", 300), ("low", 252))
        for index in range(total)
    ]
    assert len(findings) == 852
    return {
        "repo_name": "synthetic",
        "repo_path": "/repo",
        "file_count": 1,
        "node_count": 1,
        "edge_count": 0,
        "communities": [],
        "god_nodes": [],
        "hotspots": [{"path": "app.py", "score": 1, "churn": 1, "complexity": 1}],
        "surprising_connections": [],
        "dead_code": findings,
    }


def _dead_code_lines(payload: dict) -> list[str]:
    return [line for line in payload["report"].splitlines() if line.startswith("- `") and "synthetic" in line]


def _json_tokens(payload: dict) -> int:
    budgeted = {key: value for key, value in payload.items() if key in BUDGETED_KEYS}
    return count_text_tokens(json.dumps(budgeted, sort_keys=True))


def test_overview_schema_default_budget_and_format():
    overview = next(item for item in mcp_tools.TOOL_DEFINITIONS if item["name"] == "cortex_overview")
    properties = overview["inputSchema"]["properties"]
    assert properties["budget"]["default"] == 2000
    assert properties["response_format"]["default"] == "concise"


def test_dead_code_priority_is_confidence_then_file_symbol_and_numeric_line():
    findings = [
        {"confidence": "high", "file": "z.py", "symbol": "alpha", "line": 1, "reason": "a"},
        {"confidence": "high", "file": "a.py", "symbol": "zeta", "line": 1, "reason": "a"},
        {"confidence": "high", "file": "a.py", "symbol": "alpha", "line": 20, "reason": "a"},
        {"confidence": "high", "file": "a.py", "symbol": "alpha", "line": 3, "reason": "z"},
        {"confidence": "medium", "file": "0.py", "symbol": "first", "line": 1, "reason": "a"},
    ]
    assert [item["symbol"] for item in sorted(findings, key=mcp_tools._overview_dead_code_priority)] == [
        "alpha", "alpha", "zeta", "alpha", "first"
    ]
    ordered = sorted(findings, key=mcp_tools._overview_dead_code_priority)
    assert [item["line"] for item in ordered[:2]] == [3, 20]


def test_budgeted_overview_is_confidence_first_and_reports_exact_counts():
    analysis = _analysis()
    payload = mcp_tools._fit_overview_report(Path("/repo"), analysis, budget=1500)

    retained = payload["dead_code_returned"]
    expected = [
        f"- `{tier}_{index:03d}`"
        for tier, total in (("high", 300), ("medium", 300), ("low", 252))
        for index in range(total)
    ][:retained]
    lines = _dead_code_lines(payload)
    assert retained and retained < 852
    assert [line.split("`", 2)[1] for line in lines] == [item[3:-1] for item in expected]
    assert _json_tokens(payload) <= payload["budget"]
    assert payload["truncated"] is True
    omitted = payload["dead_code_total"] - payload["dead_code_returned"]
    assert f"{omitted} additional candidate(s) omitted; use `cortex_dead_code` for the complete list." in payload["report"]
    assert payload["dead_code_total"] == 852


def test_overview_large_budget_retains_all_and_tiny_budget_is_honest():
    analysis = _analysis()
    all_retained = mcp_tools._fit_overview_report(Path("/repo"), analysis, budget=200_000)
    assert all_retained["dead_code_returned"] == all_retained["dead_code_total"] == 852
    assert all_retained["truncated"] is False
    assert len(_dead_code_lines(all_retained)) == 852

    tiny = mcp_tools._fit_overview_report(Path("/repo"), analysis, budget=1)
    assert tiny["budget_feasible"] is False
    assert tiny["truncated"] is True
    assert tiny["dead_code_returned"] == 0
    assert tiny["dead_code_total"] == 852
    assert tiny["budget_note"]
    assert _json_tokens(tiny) > tiny["budget"]


def test_concise_and_detailed_calls_obey_2000_token_budget_and_refresh_capabilities(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    analysis = _analysis()
    calls = {"analysis": 0, "capabilities": 0}

    def build(_repo):
        calls["analysis"] += 1
        return analysis

    def capabilities(_store, _repo):
        calls["capabilities"] += 1
        return {"semantic": {"generation": calls["capabilities"]}, "language_runtime": {"ready": True}}

    monkeypatch.setattr(mcp_tools, "build_report_data", build)
    monkeypatch.setattr(mcp_tools, "_overview_detailed_capabilities", capabilities)
    concise = json.loads(mcp_tools._call_overview({"repo_path": str(repo), "budget": 2000})["content"][0]["text"])
    detailed = json.loads(
        mcp_tools._call_overview({"repo_path": str(repo), "response_format": "detailed", "budget": 2000})["content"][0]["text"]
    )
    refreshed = json.loads(
        mcp_tools._call_overview({"repo_path": str(repo), "response_format": "detailed", "budget": 2000})["content"][0]["text"]
    )

    assert calls["analysis"] == 1, "both formats reuse cached analysis"
    assert detailed["semantic"]["generation"] == 1
    assert refreshed["semantic"]["generation"] == 2, "capabilities are volatile and must not be cached"
    for payload in (concise, detailed):
        assert payload["budget_feasible"] is True
        assert _json_tokens(payload) <= 2000
        assert len(json.dumps(payload, sort_keys=True)) < 20_000
        assert payload["dead_code_returned"] < payload["dead_code_total"] == 852


def test_malformed_same_version_analysis_cache_is_replaced_and_ledger_uses_detailed_baseline(tmp_path, monkeypatch):
    repo = _indexed_repo(tmp_path, monkeypatch)
    analysis = _analysis()
    store = CortexStore(default_db_path(repo))
    fingerprint = store.get_repo_fingerprint(repo)
    analysis_key = mcp_tools._overview_analysis_cache_key(fingerprint)
    store.set_query_cache(repo, analysis_key, json.dumps({"version": mcp_tools._OVERVIEW_ANALYSIS_CACHE_VERSION, "analysis": {"dead_code": []}}))
    calls = {"analysis": 0}

    def build(_repo):
        calls["analysis"] += 1
        return analysis

    monkeypatch.setattr(mcp_tools, "build_report_data", build)
    concise_args = {"repo_path": str(repo), "budget": 200_000}
    payload = json.loads(call_tool("cortex_overview", concise_args)["content"][0]["text"])

    assert calls["analysis"] == 1
    cached = json.loads(store.get_query_cache(repo, analysis_key))
    assert cached["version"] == mcp_tools._OVERVIEW_ANALYSIS_CACHE_VERSION
    assert cached["analysis"] == analysis
    assert payload["dead_code_returned"] == payload["dead_code_total"] == 852
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_overview"]
    assert len(rows) == 1
    assert rows[0]["baseline_tokens"] == mcp_tools._detailed_rendering_tokens("cortex_overview", concise_args)
