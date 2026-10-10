"""Equivalence tests for Phase 1 item 4 of docs/fix-plan-mcp-concurrency-wedge.md.

Each of the four grow-one-item-and-re-serialize budget loops in
`cortex.mcp.tools` was replaced with an O(log n) binary search (or a shared
helper that does the same). This file pins that the optimized versions
select EXACTLY the same items / produce EXACTLY the same numbers as the
original O(n) algorithms, by re-implementing each original algorithm
verbatim here (as `_reference_*`) and comparing outputs across a battery of
inputs, including pathological small budgets, empty inputs, and budgets far
larger than needed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from cortex.mcp import tools as mcp_tools
from cortex.tokenizer import count_text_tokens, truncate_text_to_budget


# ---------------------------------------------------------------------------
# Reference (pre-optimization) implementations, copied verbatim from the
# original source so this test does not depend on the optimized code to
# define what "correct" means.
# ---------------------------------------------------------------------------


def _reference_fit_overview_report(
    repo_root: Path,
    analysis: dict[str, Any],
    budget: int,
    *,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings = [item for item in analysis.get("dead_code", []) if isinstance(item, dict)]
    findings.sort(key=mcp_tools._overview_dead_code_priority)
    minimum = mcp_tools._overview_payload_for_report(
        repo_root, analysis, [], len(findings), budget, budget_feasible=True, extra_fields=extra_fields
    )
    if count_text_tokens(json.dumps(minimum, sort_keys=True)) > budget:
        return mcp_tools._overview_payload_for_report(
            repo_root, analysis, [], len(findings), budget, budget_feasible=False, extra_fields=extra_fields
        )

    selected: list[dict[str, Any]] = []
    for finding in findings:
        candidate = [*selected, finding]
        rendered = mcp_tools._overview_payload_for_report(
            repo_root, analysis, candidate, len(findings) - len(candidate), budget,
            budget_feasible=True, extra_fields=extra_fields,
        )
        if count_text_tokens(json.dumps(rendered, sort_keys=True)) > budget:
            break
        selected = candidate
    return mcp_tools._overview_payload_for_report(
        repo_root, analysis, selected, len(findings) - len(selected), budget,
        budget_feasible=True, extra_fields=extra_fields,
    )


def _reference_context_fit_card(card: dict[str, Any], allowance: int) -> dict[str, Any]:
    card = copy.deepcopy(card)
    if count_text_tokens(json.dumps(card, sort_keys=True)) <= allowance:
        return card
    card["truncated"] = True

    for key in ("impact", "symbols", "cochange_detail"):
        if key in card:
            card[key] = []

    list_keys = (
        "matches", "neighbors", "cochange", "cochange_detail", "signatures", "headings",
        "signals", "slots", "emits", "connects", "handlers", "instantiates",
    )
    while count_text_tokens(json.dumps(card, sort_keys=True)) > allowance:
        changed = False
        for key in list_keys:
            value = card.get(key)
            if isinstance(value, list) and value:
                value.pop()
                changed = True
                if count_text_tokens(json.dumps(card, sort_keys=True)) <= allowance:
                    break
        if changed:
            continue
        for key in ("signature", "message", "hint", "path"):
            value = card.get(key)
            if isinstance(value, str) and value:
                shortened = truncate_text_to_budget(value, max(1, allowance // 4), kind="text")
                if shortened != value:
                    card[key] = shortened
                    changed = True
                    break
        if changed:
            continue
        for key in list_keys:
            if isinstance(card.get(key), list):
                card[key] = []
        for key in ("signature", "message", "hint", "path"):
            if isinstance(card.get(key), str):
                card[key] = ""
        break
    return card


def _reference_prefix_fit(all_items: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], bool]:
    items: list[dict[str, Any]] = []
    truncated = False
    for item in all_items:
        if count_text_tokens(json.dumps([*items, item])) > budget:
            truncated = True
            break
        items.append(item)
    return items, truncated


# ---------------------------------------------------------------------------
# Synthetic inputs
# ---------------------------------------------------------------------------


def _synthetic_analysis(high: int, medium: int, low: int, *, reason_len: int = 0) -> dict[str, Any]:
    filler = "x" * reason_len
    findings = [
        {
            "symbol": f"{tier}_{index:04d}",
            "file": f"src/module_{index % 37}/{tier}_{index:04d}.py",
            "line": (index * 7) % 5000 + 1,
            "confidence": tier,
            "reason": f"synthetic {tier} finding referencing thing #{index} {filler}",
        }
        for tier, total in (("high", high), ("medium", medium), ("low", low))
        for index in range(total)
    ]
    return {
        "repo_name": "synthetic",
        "repo_path": "/repo",
        "file_count": 42,
        "node_count": 1234,
        "edge_count": 5678,
        "communities": [
            {"community_id": i, "node_ids": [f"file:src/c{i}/f{j}.py" for j in range(6)]}
            for i in range(4)
        ],
        "god_nodes": [{"path": f"src/god{i}.py", "connections": 100 - i} for i in range(5)],
        "hotspots": [{"path": f"src/hot{i}.py", "score": 10 - i, "churn": 10 - i, "complexity": 2} for i in range(3)],
        "surprising_connections": [
            {"source": f"src/a{i}.py", "target": f"src/b{i}.py", "note": "cross-community"} for i in range(2)
        ],
        "dead_code": findings,
    }


_BUDGETS = [0, 1, 5, 25, 100, 500, 2000, 10_000, 10**9]


def test_fit_overview_report_matches_reference_across_budgets():
    repo_root = Path("/repo")
    analyses = [
        _synthetic_analysis(0, 0, 0),  # empty findings
        _synthetic_analysis(1, 0, 0),  # single finding
        _synthetic_analysis(5, 5, 5),  # small mixed
        _synthetic_analysis(120, 120, 132),  # larger, uneven reason lengths
        _synthetic_analysis(20, 20, 20, reason_len=400),  # large per-item payload
    ]
    for analysis in analyses:
        for budget in _BUDGETS:
            expected = _reference_fit_overview_report(repo_root, analysis, budget)
            actual = mcp_tools._fit_overview_report(repo_root, analysis, budget)
            assert actual == expected, (len(analysis["dead_code"]), budget)


def test_fit_overview_report_matches_reference_with_extra_fields():
    repo_root = Path("/repo")
    analysis = _synthetic_analysis(40, 40, 40)
    extra_fields = {"semantic": {"installed": False}, "language_runtime": {"ready": True}}
    for budget in _BUDGETS:
        expected = _reference_fit_overview_report(repo_root, analysis, budget, extra_fields=extra_fields)
        actual = mcp_tools._fit_overview_report(repo_root, analysis, budget, extra_fields=extra_fields)
        assert actual == expected, budget


def test_fit_overview_report_selection_is_a_findings_prefix():
    # The selected findings must be the *sorted* prefix, identical order.
    repo_root = Path("/repo")
    analysis = _synthetic_analysis(30, 30, 30)
    sorted_findings = sorted(
        [item for item in analysis["dead_code"] if isinstance(item, dict)],
        key=mcp_tools._overview_dead_code_priority,
    )
    for budget in (50, 300, 1500, 5000):
        actual = mcp_tools._fit_overview_report(repo_root, analysis, budget)
        returned = actual["dead_code_returned"]
        # render_report interpolates the finding dicts; recover the selected
        # symbols via the report text (only the Dead Code Candidates
        # section -- God Nodes/Hotspots lines also start with "- `") to
        # confirm prefix + order.
        report_text = actual["report"]
        dead_code_section = report_text.split("## Dead Code Candidates", 1)[1]
        rendered_symbols = [
            line.split("`")[1]
            for line in dead_code_section.splitlines()
            if line.startswith("- `")
        ]
        expected_symbols = [f["symbol"] for f in sorted_findings[:returned]]
        assert rendered_symbols == expected_symbols, budget


# ---------------------------------------------------------------------------
# _context_fit_card
# ---------------------------------------------------------------------------


def _rich_card() -> dict[str, Any]:
    return {
        "target": "src/widget.qml",
        "node_id": "file:src/widget.qml",
        "kind": "file",
        "path": "src/widget.qml",
        "span": {"start": 1, "end": 400},
        "neighbors": [{"node_id": f"file:src/n{i}.qml", "path": f"src/n{i}.qml", "relation": "imports", "weight": 0.9} for i in range(3)],
        "cochange": [{"path": f"src/co{i}.qml", "weight": 0.5} for i in range(3)],
        "hotspot": {"churn": 5, "complexity": 3, "score": 15},
        "hotspot_bit": True,
        "truncated": False,
        "signals": [f"signalFired{i}" for i in range(6)],
        "slots": [f"onSlotTriggered{i}" for i in range(6)],
        "emits": sorted(f"signalFired{i}" for i in range(6)),
        "connects": [{"signal": f"s{i}", "slot": f"t{i}", "source": f"sym:{i}", "target": f"sym:{i}b"} for i in range(4)],
        "handlers": [f"onHandler{i}" for i in range(5)],
        "instantiates": [{"node_id": f"sym:cls{i}", "path": "src/widget.qml", "label": f"Cls{i}"} for i in range(4)],
        "properties": [f"prop{i}" for i in range(4)],
        "ids": [f"id{i}" for i in range(4)],
        "methods": [f"method{i}" for i in range(4)],
        "enums": [],
        "inline_components": [],
        "bindings": [{"relation": "binds", "source": "a", "target": "b", "name": f"bind{i}"} for i in range(3)],
        "imports": [f"Module{i}" for i in range(3)],
        "modules": [f"Module{i}" for i in range(3)],
        "impact": [{"path": f"src/imp{i}.qml", "score": 1.0} for i in range(10)],
        "cochange_detail": [{"path": f"src/cd{i}.qml", "weight": 0.1} for i in range(10)],
        "symbols": [{"node_id": f"sym:x{i}"} for i in range(10)],
        "signature": "class Widget: this is a fairly long synthetic signature string for trimming purposes " * 2,
        "matches": [{"node_id": f"sym:m{i}", "label": f"match{i}"} for i in range(8)],
    }


def _minimal_matches_card() -> dict[str, Any]:
    return mcp_tools._context_problem_card(
        "someTarget",
        status="ambiguous",
        matches=[
            _FakeNode(f"sym:node{i}", "function", f"label{i}", f"sig{i}(...)", f"src/f{i}.py", i, i + 5)
            for i in range(15)
        ],
    )


class _FakeNode:
    def __init__(self, node_id, kind, label, signature, source_ref, span_start, span_end):
        self.node_id = node_id
        self.kind = kind
        self.label = label
        self.signature = signature
        self.source_ref = source_ref
        self.span_start = span_start
        self.span_end = span_end


def _empty_card() -> dict[str, Any]:
    return mcp_tools._context_problem_card("missingTarget", status="missing")


def _single_item_card() -> dict[str, Any]:
    return {
        "target": "src/one.py",
        "node_id": "file:src/one.py",
        "kind": "file",
        "path": "src/one.py",
        "span": {"start": 1, "end": 10},
        "neighbors": [{"node_id": "file:src/two.py", "path": "src/two.py", "relation": "imports", "weight": 0.4}],
        "cochange": [],
        "hotspot": {"churn": 0, "complexity": 0, "score": 0},
        "hotspot_bit": False,
        "truncated": False,
        "signatures": ["def one(): ..."],
        "message": "a short message",
        "hint": "a short hint",
    }


_CARD_BUILDERS = [_rich_card, _minimal_matches_card, _empty_card, _single_item_card]
_ALLOWANCES = [0, 1, 3, 10, 30, 80, 200, 1000, 10**6]


def test_context_fit_card_matches_reference_across_allowances():
    for build in _CARD_BUILDERS:
        for allowance in _ALLOWANCES:
            original = build()
            expected = _reference_context_fit_card(copy.deepcopy(original), allowance)
            actual = mcp_tools._context_fit_card(copy.deepcopy(original), allowance)
            assert actual == expected, (build.__name__, allowance)


def test_context_fit_card_already_fits_returns_unchanged():
    card = _single_item_card()
    baseline = count_text_tokens(json.dumps(card, sort_keys=True))
    result = mcp_tools._context_fit_card(copy.deepcopy(card), baseline + 500)
    assert result == card
    assert result["truncated"] is False


def test_context_fit_card_impossible_budget_clears_lists_and_strings():
    card = _rich_card()
    result = mcp_tools._context_fit_card(copy.deepcopy(card), 1)
    reference = _reference_context_fit_card(copy.deepcopy(card), 1)
    assert result == reference
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# _prefix_fit_items (shared by _call_relations and _call_search_text)
# ---------------------------------------------------------------------------


def _relation_like_items(n: int, *, big_every: int = 0) -> list[dict[str, Any]]:
    items = []
    for i in range(n):
        extra = "y" * 300 if big_every and i % big_every == 0 else ""
        items.append(
            {
                "relation": "calls",
                "source": f"symbolA{i} @ src/a{i}.py:{i + 1}",
                "target": f"symbolB{i} @ src/b{i}.py:{i + 2}",
                "layer": "STRUCTURAL",
                "confidence": round(0.5 + (i % 5) / 10, 2),
                "origin": "treesitter-parser",
                "extra": extra,
            }
        )
    return items


def _search_hit_like_items(n: int) -> list[dict[str, Any]]:
    return [
        {
            "path": f"src/file{i}.py",
            "score": round(1.0 / (i + 1), 4),
            "snippet": f"...line {i} of context around the match, with some extra words to pad it out {i * 3}...",
        }
        for i in range(n)
    ]


def test_prefix_fit_items_matches_reference_relations_like():
    for n in (0, 1, 2, 10, 50, 200):
        items = _relation_like_items(n, big_every=7)
        for budget in _BUDGETS:
            expected = _reference_prefix_fit(items, budget)
            actual = mcp_tools._prefix_fit_items(items, budget)
            assert actual == expected, (n, budget)


def test_prefix_fit_items_matches_reference_search_text_like():
    for n in (0, 1, 2, 10, 50, 150):
        items = _search_hit_like_items(n)
        for budget in _BUDGETS:
            expected = _reference_prefix_fit(items, budget)
            actual = mcp_tools._prefix_fit_items(items, budget)
            assert actual == expected, (n, budget)


def test_prefix_fit_items_stops_at_first_oversized_item_not_a_later_smaller_one():
    # A pathological ordering: item 0 is huge (many distinct whitespace-
    # separated segments, so the fallback tokenizer counts it as many
    # tokens rather than one long alphanumeric run), everything after is
    # tiny. The original loop breaks permanently at item 0 and never looks
    # past it, even though later items would individually fit. Confirm the
    # optimized version does not "skip ahead".
    big_blob = " ".join(f"word{i}" for i in range(3000))
    items = [{"blob": big_blob}] + [{"n": i} for i in range(50)]
    budget = count_text_tokens(json.dumps([{"n": 0}])) + 2  # fits a tiny item, not the first huge one
    assert count_text_tokens(json.dumps([items[0]])) > budget
    expected = _reference_prefix_fit(items, budget)
    actual = mcp_tools._prefix_fit_items(items, budget)
    assert actual == expected
    assert actual[0] == []
    assert actual[1] is True


def test_prefix_fit_items_empty_and_huge_budget():
    assert mcp_tools._prefix_fit_items([], 0) == ([], False)
    assert mcp_tools._prefix_fit_items([], 10**9) == ([], False)
    items = _relation_like_items(25)
    result_items, truncated = mcp_tools._prefix_fit_items(items, 10**9)
    assert result_items == items
    assert truncated is False
