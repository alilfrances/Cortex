from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.cli import build_parser, main
from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.savings import compute_savings, format_savings
from cortex.store import CortexStore, default_db_path
from cortex.tokenizer import count_text_tokens


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


_AUTH_PY = """\
import hashlib


def hash_password(password, salt="cortex"):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify(user):
    return user is not None and user.get("active", False)


def login(user, password):
    if not verify(user):
        return False
    return hash_password(password) == user.get("password_hash")


def logout(session):
    session["active"] = False
    return session


def reset_password(user, new_password):
    user["password_hash"] = hash_password(new_password)
    return user
""" + "".join(
    f'''

def verify_step_{i:02d}(user, password):
    """Padding helper {i} so auth.py is large enough that reading it raw
    costs meaningfully more than a packed/truncated cortex_query response or
    a single cortex_read_symbol span -- see test_cli_saved_command_end_to_end.
    """
    if user is None:
        return False
    token = f"step-{i}-" + password
    return len(token) > {i}
'''
    for i in range(40)
)

_BILLING_PY = """\
from auth import login


def charge(user, password, amount):
    if not login(user, password):
        raise PermissionError("cannot charge unauthenticated user")
    return {"user": user["id"], "amount": amount, "status": "charged"}


def refund(user, password, amount):
    if not login(user, password):
        raise PermissionError("cannot refund unauthenticated user")
    return {"user": user["id"], "amount": -amount, "status": "refunded"}
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


# --- _estimate_baseline: file-returning tools sum raw file content tokens ---


def test_estimate_baseline_query_sums_distinct_referenced_files(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login charge billing"})
    payload = _payload(result)

    expected_paths = {item["path"] for item in payload["items"]}
    expected = sum(count_text_tokens(store.fetch_source_content(repo, p)) for p in expected_paths)
    baseline = mcp_tools._estimate_baseline("cortex_query", {"repo_path": str(repo)}, payload, store, repo)

    assert expected_paths, "test setup should surface at least one file"
    assert baseline == expected
    assert baseline > 0


def test_estimate_baseline_impact_sums_referenced_files(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    payload = _payload(result)

    expected_paths = {item["path"] for item in payload["items"]}
    expected = sum(count_text_tokens(store.fetch_source_content(repo, p)) for p in expected_paths)
    baseline = mcp_tools._estimate_baseline("cortex_impact", {"repo_path": str(repo)}, payload, store, repo)

    assert expected_paths
    assert baseline == expected


def test_estimate_baseline_read_symbol_uses_resolved_file(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "login"})
    payload = _payload(result)

    assert payload["path"] == "auth.py"
    expected = count_text_tokens(store.fetch_source_content(repo, "auth.py"))
    baseline = mcp_tools._estimate_baseline("cortex_read_symbol", {"repo_path": str(repo)}, payload, store, repo)

    assert baseline == expected
    assert baseline > 0


def test_estimate_baseline_read_symbol_ambiguous_match_has_zero_baseline(tmp_path, monkeypatch):
    # Two symbols share the substring "user" is too broad; instead force an
    # ambiguous match by querying the file path, which yields several symbol
    # matches and no resolved body/path -- there is no delivered content to
    # compare against a raw read, so baseline must be 0, not a guess.
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "auth.py"})
    payload = _payload(result)

    assert "matches" in payload
    assert "path" not in payload
    baseline = mcp_tools._estimate_baseline("cortex_read_symbol", {"repo_path": str(repo)}, payload, store, repo)

    assert baseline == 0


def test_estimate_baseline_references_sums_files_stripping_line_numbers(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_references", {"repo_path": str(repo), "symbol": "login"})
    payload = _payload(result)

    referenced_paths = set()
    for bucket in payload["items"].values():
        for entry in bucket:
            referenced_paths.add(entry.rsplit(":", 1)[0] if ":" in entry else entry)
    expected = sum(
        count_text_tokens(content)
        for content in (store.fetch_source_content(repo, p) for p in referenced_paths)
        if content
    )
    baseline = mcp_tools._estimate_baseline("cortex_references", {"repo_path": str(repo)}, payload, store, repo)

    assert referenced_paths
    assert baseline == expected
    assert baseline > 0


# --- _estimate_baseline: structure-only tools use detailed-render tokens ---


def test_estimate_baseline_search_symbols_equals_detailed_rendering(tmp_path, monkeypatch):
    # P1-5: a `detailed` response always carries a `_meta` envelope now.
    # `_estimate_baseline`'s internal `_detailed_rendering_tokens` prices
    # the DATA cost of the detailed rendering only (see that function's
    # docstring) and excludes `_meta` from the count -- so the expected
    # value computed here must exclude it too via `_without_meta`, or the
    # two numbers would diverge by `_meta`'s own (irrelevant) size.
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    args = {"repo_path": str(repo), "query": "login"}

    concise_payload = _payload(call_tool("cortex_search_symbols", args))
    detailed_payload = _payload(call_tool("cortex_search_symbols", {**args, "response_format": "detailed"}))
    expected = count_text_tokens(json.dumps(mcp_tools._without_meta(detailed_payload)))

    baseline = mcp_tools._estimate_baseline("cortex_search_symbols", args, concise_payload, store, repo)

    assert baseline == expected
    # Detailed adds the "why" field per item, so it must cost more than concise.
    assert expected > count_text_tokens(json.dumps(mcp_tools._without_meta(concise_payload)))


def test_estimate_baseline_overview_equals_detailed_rendering(tmp_path, monkeypatch):
    # See the P1-5 note in test_estimate_baseline_search_symbols_equals_detailed_rendering.
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    args = {"repo_path": str(repo)}

    concise_payload = _payload(call_tool("cortex_overview", args))
    detailed_payload = _payload(call_tool("cortex_overview", {**args, "response_format": "detailed"}))
    expected = count_text_tokens(json.dumps(mcp_tools._without_meta(detailed_payload)))

    baseline = mcp_tools._estimate_baseline("cortex_overview", args, concise_payload, store, repo)

    assert baseline == expected


def test_estimate_baseline_relations_adds_referenced_file_tokens_to_detailed_render(tmp_path, monkeypatch):
    # See the P1-5 note in test_estimate_baseline_search_symbols_equals_detailed_rendering.
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    args = {"repo_path": str(repo), "relation": "imports"}

    payload = _payload(call_tool("cortex_relations", args))
    detailed_payload = _payload(call_tool("cortex_relations", {**args, "response_format": "detailed"}))
    detailed_tokens = count_text_tokens(json.dumps(mcp_tools._without_meta(detailed_payload)))

    referenced_paths = set()
    for item in payload["items"]:
        for key in ("source", "target"):
            endpoint = item.get(key, "")
            if " @ " in endpoint:
                file_part = endpoint.split(" @ ", 1)[1]
                referenced_paths.add(file_part.rsplit(":", 1)[0] if ":" in file_part else file_part)
    file_tokens = sum(
        count_text_tokens(content)
        for content in (store.fetch_source_content(repo, p) for p in referenced_paths)
        if content
    )

    baseline = mcp_tools._estimate_baseline("cortex_relations", args, payload, store, repo)

    assert payload["items"], "test setup should surface at least one imports edge"
    assert baseline == detailed_tokens + file_tokens
    assert baseline >= detailed_tokens


# --- ledger writes are non-fatal and land in the store ---


def test_call_tool_records_ledger_row_after_success(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))
    assert store.fetch_tool_usage(repo) == []

    call_tool("cortex_query", {"repo_path": str(repo), "task": "login"})

    rows = store.fetch_tool_usage(repo)
    assert len(rows) == 1
    assert rows[0]["tool"] == "cortex_query"
    assert rows[0]["response_tokens"] > 0
    assert rows[0]["baseline_tokens"] > 0


def test_call_tool_skips_ledger_row_on_error_response(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    store = CortexStore(default_db_path(repo))

    result = call_tool("cortex_impact", {"repo_path": str(repo), "path": "/does/not/exist.py"})

    assert result["isError"] is True
    assert store.fetch_tool_usage(repo) == []


def test_ledger_write_failure_does_not_break_tool_response(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    def _boom(self, *args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(CortexStore, "record_tool_usage", _boom)

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]

    # And the ledger really is empty -- the failure was swallowed, not silently retried.
    store = CortexStore(default_db_path(repo))
    assert store.fetch_tool_usage(repo) == []


def test_estimate_baseline_exception_does_not_break_tool_response(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mcp_tools, "_estimate_baseline", _boom)

    result = call_tool("cortex_query", {"repo_path": str(repo), "task": "login"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["items"]


# --- CortexStore ledger round trip ---


def test_store_record_and_fetch_tool_usage_round_trips(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()

    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=40)
    store.record_tool_usage(repo, "cortex_impact", response_tokens=5, baseline_tokens=5, meta={"note": "x"})

    rows = store.fetch_tool_usage(repo)

    assert [row["tool"] for row in rows] == ["cortex_query", "cortex_impact"]
    assert rows[0]["response_tokens"] == 10
    assert rows[0]["baseline_tokens"] == 40
    assert rows[1]["meta"] == {"note": "x"}


# --- savings aggregation + CLI ---


def test_compute_savings_aggregates_totals_and_per_tool(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=40)
    store.record_tool_usage(repo, "cortex_query", response_tokens=20, baseline_tokens=60)
    store.record_tool_usage(repo, "cortex_impact", response_tokens=5, baseline_tokens=5)

    summary = compute_savings(repo, db_path=tmp_path / "cortex.db")

    assert summary["totals"]["calls"] == 3
    assert summary["totals"]["response_tokens"] == 35
    assert summary["totals"]["baseline_tokens"] == 105
    assert summary["totals"]["saved_tokens"] == 70
    assert summary["totals"]["save_pct"] == pytest.approx(66.7, abs=0.1)

    per_tool = {entry["tool"]: entry for entry in summary["per_tool"]}
    assert per_tool["cortex_query"]["calls"] == 2
    assert per_tool["cortex_impact"]["saved_tokens"] == 0


def test_compute_savings_daily_rollup_groups_by_date(tmp_path, monkeypatch):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    import time as time_mod

    calls = iter([1_700_000_000, 1_700_003_600, 1_700_100_000])  # two same-day, one next-day
    monkeypatch.setattr(time_mod, "time", lambda: next(calls))
    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=20)
    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=20)
    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=20)

    summary = compute_savings(repo, db_path=tmp_path / "cortex.db")

    assert len(summary["daily"]) == 2
    assert sum(entry["calls"] for entry in summary["daily"]) == 3


def test_compute_savings_price_per_mtok_computes_dollars(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store.record_tool_usage(repo, "cortex_query", response_tokens=1_000_000, baseline_tokens=2_000_000)

    summary = compute_savings(repo, db_path=tmp_path / "cortex.db", price_per_mtok=(3.0, 15.0))

    assert summary["dollars"]["baseline"] == pytest.approx(6.0)
    assert summary["dollars"]["actual"] == pytest.approx(3.0)
    assert summary["dollars"]["saved"] == pytest.approx(3.0)


def test_format_savings_text_and_json(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    store.record_tool_usage(repo, "cortex_query", response_tokens=10, baseline_tokens=40)
    summary = compute_savings(repo, db_path=tmp_path / "cortex.db")

    text = format_savings(summary, output_format="text")
    as_json = json.loads(format_savings(summary, output_format="json"))

    assert "Saved tokens: 30" in text
    assert as_json["totals"]["saved_tokens"] == 30


def test_format_savings_empty_ledger_says_so(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    summary = compute_savings(repo, db_path=tmp_path / "cortex.db")

    text = format_savings(summary)

    assert "No recorded tool usage yet" in text
    assert summary["totals"]["calls"] == 0


def test_cli_saved_command_end_to_end(tmp_path, monkeypatch, capsys):
    # A tight budget forces cortex_query to pack/truncate auth.py's ~3k raw
    # tokens down substantially, and cortex_read_symbol returns one ~6-line
    # span out of that same file -- both should show clear positive savings
    # once the MCP JSON envelope overhead (which cortex saved does not hide)
    # is netted out, exercising the acceptance criteria from IMPROVEMENT_PLAN
    # P0-1 ("cortex saved reports non-zero savings").
    repo = _repo_with_index(tmp_path, monkeypatch)
    call_tool("cortex_query", {"repo_path": str(repo), "task": "login", "budget": 200})
    call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "verify_step_05"})

    parser = build_parser()
    parser.parse_args(["saved", str(repo), "--format", "json"])
    monkeypatch.setattr("sys.argv", ["cortex", "saved", str(repo), "--format", "json"])
    main()

    out = json.loads(capsys.readouterr().out)
    assert out["totals"]["calls"] == 2
    assert out["totals"]["saved_tokens"] > 0
    assert out["totals"]["save_pct"] > 0


def test_cli_saved_command_rejects_malformed_price_flag(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.setattr("sys.argv", ["cortex", "saved", str(repo), "--price-per-mtok", "not-a-number"])

    with pytest.raises(SystemExit):
        main()
