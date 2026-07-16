from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .gitutils import discover_repo_root
from .store import CortexStore, default_db_path


def _day_key(epoch_seconds: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(epoch_seconds))


def _rollup(rows: list[dict[str, Any]]) -> dict[str, Any]:
    calls = len(rows)
    response_tokens = sum(int(row["response_tokens"]) for row in rows)
    baseline_tokens = sum(int(row["baseline_tokens"]) for row in rows)
    saved_tokens = baseline_tokens - response_tokens
    save_pct = round(100.0 * saved_tokens / baseline_tokens, 1) if baseline_tokens else 0.0
    return {
        "calls": calls,
        "response_tokens": response_tokens,
        "baseline_tokens": baseline_tokens,
        "saved_tokens": saved_tokens,
        "save_pct": save_pct,
    }


def _price_totals(counts: dict[str, Any], price_in_per_mtok: float) -> dict[str, float]:
    # Ledger tokens (baseline and response) both represent context that flows
    # into an agent's own model as *input* -- raw file reads in the baseline
    # case, Cortex's response in the actual case -- so both are priced at the
    # input $/Mtok rate here. The optional output rate accepted by
    # --price-per-mtok is kept for symmetry with standard <in>,<out> model
    # pricing notation (and possible future use), but this metric doesn't use
    # it: nothing in the ledger is model-generated output tokens.
    return {
        "baseline": round(counts["baseline_tokens"] / 1_000_000 * price_in_per_mtok, 4),
        "actual": round(counts["response_tokens"] / 1_000_000 * price_in_per_mtok, 4),
        "saved": round(counts["saved_tokens"] / 1_000_000 * price_in_per_mtok, 4),
    }


def compute_savings(
    repo_path: Path,
    db_path: Path | None = None,
    price_per_mtok: tuple[float, float] | None = None,
) -> dict[str, Any]:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    rows = store.fetch_tool_usage(repo_root)

    totals = _rollup(rows)

    by_day: dict[str, list[dict[str, Any]]] = {}
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_day.setdefault(_day_key(int(row["called_at"])), []).append(row)
        by_tool.setdefault(str(row["tool"]), []).append(row)

    daily = [{"date": day, **_rollup(day_rows)} for day, day_rows in sorted(by_day.items())]
    per_tool = [{"tool": tool, **_rollup(tool_rows)} for tool, tool_rows in sorted(by_tool.items())]

    result: dict[str, Any] = {
        "repo_path": str(repo_root),
        "totals": totals,
        "daily": daily,
        "per_tool": per_tool,
    }

    if price_per_mtok is not None:
        price_in, price_out = price_per_mtok
        result["price_per_mtok"] = {"input": price_in, "output": price_out}
        result["dollars"] = _price_totals(totals, price_in)
        for entry in daily:
            entry["dollars"] = _price_totals(entry, price_in)
        for entry in per_tool:
            entry["dollars"] = _price_totals(entry, price_in)

    return result


def format_savings(summary: dict[str, Any], output_format: str = "text", daily: bool = False) -> str:
    if output_format == "json":
        return json.dumps(summary, indent=2)

    totals = summary["totals"]
    lines = [
        "# Cortex Token Savings",
        "",
        f"- Repo: {summary['repo_path']}",
        f"- Calls: {totals['calls']}",
        f"- Response tokens: {totals['response_tokens']}",
        f"- Baseline tokens: {totals['baseline_tokens']}",
        f"- Saved tokens: {totals['saved_tokens']}",
        f"- Save %: {totals['save_pct']}%",
    ]
    if "dollars" in summary:
        dollars = summary["dollars"]
        prices = summary["price_per_mtok"]
        lines.append(
            f"- Est. dollars saved: ${dollars['saved']:.4f} "
            f"(baseline ${dollars['baseline']:.4f} vs actual ${dollars['actual']:.4f}, "
            f"at ${prices['input']}/Mtok input)"
        )
    if summary["per_tool"]:
        lines += [
            "",
            "## By tool",
            "",
            "| tool | calls | response tokens | baseline tokens | saved tokens | save % |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for entry in summary["per_tool"]:
            lines.append(
                f"| {entry['tool']} | {entry['calls']} | {entry['response_tokens']} | "
                f"{entry['baseline_tokens']} | {entry['saved_tokens']} | {entry['save_pct']}% |"
            )
    if daily:
        lines += ["", "## Daily"]
        if summary["daily"]:
            lines += [
                "",
                "| date | calls | response tokens | baseline tokens | saved tokens | save % |",
                "|---|---:|---:|---:|---:|---:|",
            ]
            for entry in summary["daily"]:
                lines.append(
                    f"| {entry['date']} | {entry['calls']} | {entry['response_tokens']} | "
                    f"{entry['baseline_tokens']} | {entry['saved_tokens']} | {entry['save_pct']}% |"
                )
        else:
            lines.append("")
            lines.append("No recorded tool usage yet.")
    if totals["calls"] == 0:
        lines.append("")
        lines.append("No recorded tool usage yet. Call Cortex MCP tools (cortex_context, cortex_query, cortex_read_symbol, ...) to populate the ledger.")
    return "\n".join(lines)
