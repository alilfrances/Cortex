"""P1-1 batched Cortex context acceptance tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.server import _handle
from cortex.mcp.tools import TOOL_DEFINITIONS, call_tool
from cortex.store import CortexStore, default_db_path
from evals.run_evals import _build_qt_app


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _repo_with_index(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app.py").write_text(
        """from helpers import helper


def run(value):
    return helper(value)


def other(value):
    return value + 1
""",
        encoding="utf-8",
    )
    (repo / "helpers.py").write_text(
        """def helper(value):
    return value * 2


def duplicate(value):
    return value
""",
        encoding="utf-8",
    )
    (repo / "other.py").write_text(
        """def duplicate(value):
    return value - 1
""",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        """# Context fixture

## Usage

## Triage
""",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial fixture"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=20)
    return repo


def test_context_is_registered_and_visible_via_tools_list() -> None:
    definition = next(tool for tool in TOOL_DEFINITIONS if tool["name"] == "cortex_context")
    schema = definition["inputSchema"]
    assert schema["required"] == ["targets"]
    assert schema["properties"]["targets"]["type"] == "array"
    assert schema["properties"]["budget"]["default"] == 2000
    assert set(schema["properties"]["include"]["items"]["enum"]) == {"impact", "cochange", "symbols"}

    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    assert "cortex_context" in {tool["name"] for tool in response["result"]["tools"]}


def test_five_mixed_targets_return_ordered_cards_under_shared_budget(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    targets = [
        "README.md",                    # repository-relative path
        "file:app.py",                  # exact file node id
        "run",                          # symbol name
        "symbol:app.py:run",            # exact symbol node id
        "helpers.py",                   # another repository-relative path
    ]

    result = call_tool("cortex_context", {"repo_path": str(repo), "targets": targets, "budget": 2000})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["targets"] == targets
    assert [card["target"] for card in payload["cards"]] == targets
    assert len(payload["cards"]) == len(targets)
    assert payload["total_tokens"] <= 2000
    assert all(card.get("node_id") and "status" not in card for card in payload["cards"])
    assert payload["cards"][0]["node_id"] == "file:README.md"
    assert payload["cards"][0]["kind"] == "file"
    assert payload["cards"][0]["path"] == "README.md"
    assert payload["cards"][0]["headings"] == ["Context fixture", "Usage", "Triage"]
    assert payload["cards"][1]["node_id"] == "file:app.py"
    assert payload["cards"][2]["node_id"] == "symbol:app.py:run"
    assert payload["cards"][3]["node_id"] == "symbol:app.py:run"
    assert payload["cards"][4]["node_id"] == "file:helpers.py"
    assert payload["cards"][2]["signature"].startswith("def run")
    assert payload["cards"][2]["span"]["start"] < payload["cards"][2]["span"]["end"]
    assert all("truncated" in card for card in payload["cards"])
    for card in payload["cards"]:
        assert len(card["neighbors"]) <= 3
        assert len(card["cochange"]) <= 3
        assert all("weight" in partner for partner in card["cochange"])
        assert set(card["hotspot"]) == {"churn", "complexity", "score"}
        assert isinstance(card["hotspot_bit"], bool)
        assert card["hotspot_bit"] == (card["hotspot"]["score"] > 0)
    qt_keys = {"signals", "slots", "emits", "connects", "handlers", "instantiates"}
    assert not any(qt_keys & set(card) for card in payload["cards"])


def test_exact_file_targets_bypass_symbol_resolver(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise AssertionError("exact file targets must not use _resolve_symbol")

    monkeypatch.setattr(mcp_tools, "_resolve_symbol", fail)
    payload = _payload(
        call_tool(
            "cortex_context",
            {"repo_path": str(repo), "targets": ["app.py", "file:README.md"]},
        )
    )
    assert [card["node_id"] for card in payload["cards"]] == ["file:app.py", "file:README.md"]


def test_ambiguous_and_missing_targets_are_non_error_cards(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool(
        "cortex_context",
        {"repo_path": str(repo), "targets": ["duplicate", "missing.py", "app.py"], "budget": 2000},
    )
    payload = _payload(result)

    assert result["isError"] is False
    ambiguous, missing, resolved = payload["cards"]
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["node_id"] is None
    assert len(ambiguous["matches"]) == 2
    assert {"node_id", "kind", "label", "signature", "source_ref", "span_start", "span_end"} == set(ambiguous["matches"][0])
    assert missing["status"] == "missing"
    assert missing["path"] is None
    assert resolved["node_id"] == "file:app.py"


def test_include_expansions_add_detail_and_small_budget_marks_cards(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    compact = _payload(
        call_tool("cortex_context", {"repo_path": str(repo), "targets": ["app.py"], "budget": 2000})
    )["cards"][0]
    expanded = _payload(
        call_tool(
            "cortex_context",
            {
                "repo_path": str(repo),
                "targets": ["app.py"],
                "budget": 4000,
                "include": ["impact", "cochange", "symbols"],
            },
        )
    )["cards"][0]

    assert "impact" not in compact
    assert "symbols" not in compact
    assert expanded["impact"]
    assert expanded["symbols"]
    assert expanded["cochange_detail"]
    assert expanded["cochange"] == compact["cochange"]

    capped = _payload(
        call_tool(
            "cortex_context",
            {
                "repo_path": str(repo),
                "targets": ["app.py", "helpers.py", "README.md"],
                "budget": 1200,
                "include": ["impact", "cochange", "symbols"],
            },
        )
    )
    assert capped["budget_feasible"] is True
    assert capped["total_tokens"] <= capped["budget"]
    assert len(capped["cards"]) == 3
    assert [card["target"] for card in capped["cards"]] == ["app.py", "helpers.py", "README.md"]
    assert any(card["truncated"] for card in capped["cards"])

    tiny = _payload(
        call_tool(
            "cortex_context",
            {"repo_path": str(repo), "targets": ["app.py", "helpers.py"], "budget": 1},
        )
    )
    assert [card["target"] for card in tiny["cards"]] == ["app.py", "helpers.py"]
    assert tiny["budget_feasible"] is False
    assert tiny["budget_note"]


def test_context_uses_only_deduplicated_target_source_lookups(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    source_calls: list[str] = []
    original_record = CortexStore.fetch_source_record

    def counted_record(self, repo_path, path):
        source_calls.append(path)
        return original_record(self, repo_path, path)

    def fail_full_corpus_fetch(self, repo_path):
        raise AssertionError("cortex_context must not fetch every indexed source")

    monkeypatch.setattr(CortexStore, "fetch_source_record", counted_record)
    monkeypatch.setattr(CortexStore, "fetch_sources", fail_full_corpus_fetch)
    result = call_tool(
        "cortex_context",
        {
            "repo_path": str(repo),
            "targets": ["app.py", "symbol:app.py:run", "app.py", "helpers.py", "missing.py"],
        },
    )

    assert result["isError"] is False
    assert source_calls == ["app.py", "helpers.py"]

    source_calls.clear()
    symbol_only = call_tool(
        "cortex_context",
        {"repo_path": str(repo), "targets": ["symbol:app.py:run"]},
    )
    assert symbol_only["isError"] is False
    assert source_calls == []


def test_context_cards_are_deterministic_for_repeated_batch(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    args = {
        "repo_path": str(repo),
        "targets": ["helpers.py", "symbol:app.py:run", "README.md"],
        "budget": 2000,
        "include": ["impact", "cochange", "symbols"],
    }
    first = _payload(call_tool("cortex_context", args))
    second = _payload(call_tool("cortex_context", args))
    assert first["cards"] == second["cards"]
    assert first["total_tokens"] == second["total_tokens"]


def test_context_calls_ensure_fresh_once_for_the_whole_batch(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    original = mcp_tools._ensure_fresh
    calls: list[tuple] = []

    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(mcp_tools, "_ensure_fresh", counted)
    result = call_tool(
        "cortex_context",
        {"repo_path": str(repo), "targets": ["app.py", "helpers.py", "run", "README.md", "missing.py"]},
    )

    assert result["isError"] is False
    assert len(calls) == 1


def test_context_routes_meta_and_raw_file_ledger_baseline(tmp_path, monkeypatch) -> None:
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool(
        "cortex_context",
        {
            "repo_path": str(repo),
            "targets": ["app.py", "symbol:app.py:run"],
            "response_format": "detailed",
        },
    )
    payload = _payload(result)
    assert result["isError"] is False
    assert payload["_meta"]["fingerprint_fresh"] is True

    store = CortexStore(default_db_path(repo))
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_context"]
    assert len(rows) == 1
    expected = sum(
        len((store.fetch_source_content(repo, path) or "").split())
        for path in {"app.py"}
    )
    # The exact tokenizer is intentionally implementation-dependent; the
    # baseline must nevertheless be the raw indexed app.py content and not
    # the compact card or a second per-target estimate.
    assert rows[0]["baseline_tokens"] > 0
    assert rows[0]["baseline_tokens"] == mcp_tools._referenced_file_tokens(store, repo, {"app.py"})
    assert expected > 0


def test_qt_context_card_exposes_regex_qt_wiring_and_cpp_instantiation(tmp_path, monkeypatch) -> None:
    import cortex.structural.treesitter_backend as treesitter_backend

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("grammar unavailable")))
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    payload = _payload(
        call_tool(
            "cortex_context",
            {
                "repo_path": str(repo),
                "targets": ["symbol:include/DeviceManager.hpp:DeviceManager", "qml/Main.qml"],
                "budget": 2000,
            },
        )
    )

    cpp, qml = payload["cards"]
    assert cpp["node_id"] == "symbol:include/DeviceManager.hpp:DeviceManager"
    assert "deviceConnected" in cpp["signals"]
    assert "onDeviceConnected" in cpp["slots"]
    assert "deviceConnected" in cpp["emits"]
    assert any(
        wiring["signal"] == "deviceConnected" and wiring["slot"] == "onDeviceConnected"
        for wiring in cpp["connects"]
    )
    assert {"onClicked", "onDeviceConnected"} <= set(qml["handlers"])
    instantiations = {item["label"] for item in qml["instantiates"]}
    assert "DeviceManager" in instantiations
    assert any(item["node_id"] == "symbol:include/DeviceManager.hpp:DeviceManager" for item in qml["instantiates"])


def test_qt_context_tree_sitter_parity_when_installed(tmp_path) -> None:
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    payload = _payload(
        call_tool(
            "cortex_context",
            {"repo_path": str(repo), "targets": ["qml/Main.qml"], "budget": 1000},
        )
    )
    card = payload["cards"][0]
    assert "DeviceManager" in {item["label"] for item in card["instantiates"]}
    assert {"onClicked", "onDeviceConnected"} <= set(card["handlers"])
