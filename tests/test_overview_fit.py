from __future__ import annotations

from pathlib import Path

import pytest

from cortex.mcp import tools


def _analysis(count: int, short: bool) -> dict:
    findings = [
        {
            "symbol": "a" if short else f"symbol_number_{i}",
            "file": "b.c" if short else f"src/module_{i % 40}/file_{i}.cpp",
            "line": i,
            "confidence": ("high", "medium", "low")[i % 3],
            "reason": "r" if short else "no incoming edges; no grep refs",
        }
        for i in range(count)
    ]
    return {
        "repo_name": "r", "repo_path": "/r", "file_count": 3, "node_count": 9, "edge_count": 4,
        "communities": [], "god_nodes": [], "hotspots": [], "surprising_connections": [],
        "dead_code": findings, "dead_code_partial": False,
    }


def _uncapped_best(analysis: dict, budget: int) -> int:
    findings = sorted(analysis["dead_code"], key=tools._overview_dead_code_priority)

    def fits(count: int) -> bool:
        chosen = findings[:count]
        payload = tools._overview_payload_for_report(
            Path("/r"), analysis, chosen, len(findings) - count, budget, budget_feasible=True
        )
        return tools.count_text_tokens(tools.json.dumps(payload, sort_keys=True)) <= budget

    lo, hi, best = 0, len(findings), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if fits(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


@pytest.mark.parametrize("short", [True, False])
@pytest.mark.parametrize("budget", [0, 50, 400, 2000, 20000])
def test_bounded_search_selects_the_same_prefix_as_the_full_search(budget, short):
    analysis = _analysis(3000, short)
    result = tools._fit_overview_report(Path("/r"), analysis, budget)
    assert result["dead_code_returned"] == _uncapped_best(analysis, budget) or not result["budget_feasible"]
