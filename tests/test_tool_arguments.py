from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("from b import helper\n\n\ndef main():\n    return helper()\n")
    (root / "b.py").write_text("def helper():\n    return 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _call(repo: Path, tool: str, **arguments):
    result = tools.call_tool(tool, {"repo_path": str(repo), **arguments})
    return result, json.loads(result["content"][0]["text"])


@pytest.mark.parametrize(
    ("tool", "alias", "canonical", "value"),
    [
        ("cortex_path", "source", "symbol_a", "main"),
        ("cortex_path", "from", "symbol_a", "main"),
        ("cortex_path", "target", "symbol_b", "helper"),
        ("cortex_path", "to", "symbol_b", "helper"),
        ("cortex_references", "target", "symbol", "helper"),
        ("cortex_references", "name", "symbol", "helper"),
        ("cortex_read_symbol", "target", "symbol", "helper"),
        ("cortex_relations", "name", "symbol", "helper"),
        ("cortex_query", "query", "task", "helper"),
        ("cortex_query", "prompt", "task", "helper"),
        ("cortex_search_symbols", "pattern", "query", "helper"),
        ("cortex_search_text", "text", "query", "helper"),
    ],
)
def test_aliases_are_renamed_to_the_canonical_argument(tool, alias, canonical, value):
    args, error = tools._normalise_arguments(tool, {alias: value})
    assert error is None
    assert args == {canonical: value}


def test_an_explicit_canonical_value_wins_over_its_alias():
    args, error = tools._normalise_arguments("cortex_path", {"symbol_a": "x", "source": "y", "symbol_b": "z"})
    assert error is not None  # `source` has nowhere to go once symbol_a is set
    assert args["symbol_a"] == "x"


def test_cortex_path_accepts_source_and_target_end_to_end(repo):
    result, payload = _call(repo, "cortex_path", source="symbol:a.py:main", target="symbol:b.py:helper")
    assert result.get("isError") is not True, payload
    assert "error" not in payload


def test_unknown_argument_is_rejected_with_suggestions(repo):
    result, payload = _call(repo, "cortex_dead_code", pth="b.py")
    assert result["isError"] is True
    assert payload["error"] == "unknown_argument"
    assert payload["unknown"] == ["pth"]
    assert "path" in payload["accepted"]
    assert "'path'" in payload["hint"]


def test_format_and_response_format_are_accepted_by_every_tool(repo):
    for tool in (tool["name"] for tool in tools.TOOL_DEFINITIONS):
        args, error = tools._normalise_arguments(tool, {"format": "detailed"})
        assert error is None, tool
        accepted = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == tool)["inputSchema"]["properties"]
        assert ("response_format" in args) == ("response_format" in accepted), tool
        args, error = tools._normalise_arguments(tool, {"response_format": "concise"})
        assert error is None, tool


def test_protocol_metadata_keys_are_allowed():
    args, error = tools._normalise_arguments("cortex_search_symbols", {"query": "x", "_meta": {"progressToken": 1}})
    assert error is None and args["_meta"] == {"progressToken": 1}


def test_unknown_tools_still_reach_the_dispatcher_error():
    result = tools.call_tool("cortex_nope", {"anything": 1})
    assert json.loads(result["content"][0]["text"])["error"] == "unknown_tool"
