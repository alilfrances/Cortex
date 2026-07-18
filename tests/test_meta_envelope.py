"""Tests for the P1-5 standard `_meta` envelope on MCP responses.

Covers the P1-5 metadata-envelope contract:
  - `_meta` present (with the base schema fields) in `detailed` mode for
    every read/query/analysis tool.
  - `_meta` completely absent in `concise` mode when nothing is noteworthy.
  - `saved_tokens` surfaces in `_meta` only when positive, and is exactly
    the same number the P0-1 ledger records (not a parallel estimate).
  - `_meta` assembly is non-fatal: a failure computing the index age or
    the ledger's baseline must degrade to omitting the affected field(s),
    never to breaking the tool response.

`_meta.cached`/the P1-3 cache-hit-freshness fix are covered in
tests/test_query_cache.py, alongside the rest of the P1-3 suite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


_AUTH_PY = """\
def login(user, password):
    return user == "admin" and password == "secret"


def logout(user):
    return None
"""

_BILLING_PY = """\
from auth import login


def charge(user, password, amount):
    if not login(user, password):
        raise PermissionError("nope")
    return amount
"""


def _repo_with_index(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "auth.py").write_text(_AUTH_PY, encoding="utf-8")
    (repo / "billing.py").write_text(_BILLING_PY, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


# --- `_meta` present in detailed mode for every read/query/analysis tool ---


def test_meta_present_in_detailed_for_all_read_tools(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    calls = [
        ("cortex_query", {"task": "login billing"}),
        ("cortex_overview", {}),
        ("cortex_context", {"targets": ["auth.py"]}),
        ("cortex_impact", {"path": "auth.py"}),
        ("cortex_risk", {"staged": True}),
        ("cortex_dead_code", {}),
        ("cortex_search_symbols", {"query": "login"}),
        ("cortex_read_symbol", {"symbol": "login"}),
        ("cortex_read_file", {"path": "auth.py"}),
        ("cortex_relations", {"relation": "imports"}),
        ("cortex_path", {"symbol_a": "charge", "symbol_b": "login"}),
        ("cortex_references", {"symbol": "login"}),
        ("cortex_search_text", {"query": "login"}),
    ]
    assert {tool for tool, _args in calls} == mcp_tools._LEDGER_TOOLS
    for tool, extra_args in calls:
        args = {"repo_path": str(repo), "response_format": "detailed", **extra_args}
        result = call_tool(tool, args)
        payload = _payload(result)
        assert result["isError"] is False, f"{tool} unexpectedly errored: {payload}"
        assert "_meta" in payload, f"{tool} detailed response is missing _meta"
        meta = payload["_meta"]
        assert isinstance(meta["index_age_seconds"], int)
        assert isinstance(meta["indexed_at"], int)
        assert isinstance(meta["fingerprint_fresh"], bool)
        # indexed_at/index_age_seconds live only inside _meta, not duplicated
        # at the top level of the detailed payload (see _META_ONLY_STATUS_KEYS).
        assert "index_age_seconds" not in payload
        assert "indexed_at" not in payload


# --- `_meta` absent in concise mode when nothing is noteworthy ---


def test_meta_absent_in_concise_when_nothing_noteworthy(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    # Querying the bare file path resolves to multiple symbol matches
    # (login, logout both live in auth.py), never a single body --
    # _estimate_baseline gives this response a baseline of 0 (no delivered
    # content to compare against a raw read), so saved_tokens can only be
    # negative/zero and must never surface. The index is fresh and
    # cortex_read_symbol never caches, so nothing else is noteworthy
    # either: `_meta` must be completely absent from the concise response.
    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "auth.py"})
    payload = _payload(result)

    assert "matches" in payload
    assert "_meta" not in payload


# --- saved_tokens surfaces when positive, and matches the ledger exactly ---


def test_saved_tokens_surfaces_in_concise_and_matches_ledger_exactly(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    # A file large enough that reading ONE small function out of it via
    # cortex_read_symbol costs far less than the deterministic baseline
    # (the whole file, raw) -- guarantees a clearly positive saved_tokens.
    # Zero-padded names (matching test_token_savings.py's verify_step_NN
    # fixture) so "helper_05" isn't a substring of any other function name.
    big = "\n\n".join(f"def helper_{i:02d}(x):\n    return x + {i}\n" for i in range(80))
    (repo / "helpers.py").write_text(big, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "helper_05"})
    payload = _payload(result)

    assert payload["path"] == "helpers.py"
    assert "_meta" in payload, "a positive saved_tokens must make an otherwise plain concise response noteworthy"
    saved = payload["_meta"]["saved_tokens"]
    assert saved > 0

    store = CortexStore(default_db_path(repo))
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_read_symbol"]
    assert len(rows) == 1
    # P1-5: _meta.saved_tokens must be *exactly* the ledger's
    # baseline_tokens - response_tokens -- the same computation, not a
    # second, separately-derived estimate.
    assert rows[0]["baseline_tokens"] - rows[0]["response_tokens"] == saved


def test_saved_tokens_omitted_not_negative_on_tiny_fixture(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    # The ambiguous "auth.py" match has baseline 0 and a non-zero response,
    # so the true saved_tokens is negative -- it must never appear in
    # _meta (and here _meta is dropped from the response altogether, see
    # test_meta_absent_in_concise_when_nothing_noteworthy), while the
    # ledger keeps recording the true (negative) number for audit purposes.
    call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "auth.py"})

    store = CortexStore(default_db_path(repo))
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_read_symbol"]
    assert len(rows) == 1
    assert rows[0]["baseline_tokens"] == 0
    assert rows[0]["response_tokens"] > 0


# --- `_meta` assembly is non-fatal ---


def test_meta_assembly_survives_indexed_at_lookup_failure(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    def _boom(self, *_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(CortexStore, "get_repo_indexed_at", _boom)

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login", "response_format": "detailed"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]
    # Degrades to the "unknown" sentinel rather than raising and taking
    # down fingerprint-based staleness detection with it.
    assert payload["_meta"]["indexed_at"] == 0
    assert payload["_meta"]["index_age_seconds"] == 0


def test_meta_build_failure_degrades_to_omitting_meta_entirely(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_tools, "_build_meta", _boom)

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login", "response_format": "detailed"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]
    assert "_meta" not in payload


def test_ledger_and_saved_tokens_failure_does_not_break_response(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_tools, "_estimate_baseline", _boom)

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login", "response_format": "detailed"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]
    # Base _meta still assembled; just no saved_tokens since the baseline
    # computation that would produce it failed.
    assert "_meta" in payload
    assert "saved_tokens" not in payload["_meta"]
