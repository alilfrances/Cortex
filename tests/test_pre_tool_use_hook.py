from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore, default_db_path, repo_data_dir
from evals.run_evals import _build_qt_app


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pre-tool-use.py"


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "hook@example.test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Hook Tests"], cwd=repo, check=True)


def _event(repo: Path, tool: str, tool_input: dict[str, object]) -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "cwd": str(repo),
        "tool_name": tool,
        "tool_input": tool_input,
    }


def _run(repo: Path, event: object, monkeypatch, **env: str) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    child_env.update({key: value for key, value in env.items()})
    child_env.setdefault("CORTEX_HOOK_MODE", "advise")
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=repo,
        input=json.dumps(event),
        text=True,
        capture_output=True,
        timeout=5,
        env=child_env,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout
    return json.loads(result.stdout)


def _seed_repo(tmp_path: Path, monkeypatch, *, size: int = 1600) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    body = "\n".join(f"    value_{i} = {i}" for i in range(max(1, size // 20)))
    content = f"def indexed_symbol(value):\n{body}\n    return value\n"
    (repo / "large.py").write_text(content, encoding="utf-8")
    (repo / "small.py").write_text("def tiny():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    store = CortexStore(default_db_path(repo))
    store.reset_repo(repo, fingerprint="fixture")
    store.save_sources(
        repo,
        [
            SourceRecord(
                path="large.py",
                content=content,
                kind="code",
                size_bytes=len(content.encode()),
                modified_at=1.0,
                content_hash="large",
            ),
            SourceRecord(
                path="small.py",
                content="def tiny():\n    return 1\n",
                kind="code",
                size_bytes=24,
                modified_at=1.0,
                content_hash="small",
            ),
        ],
    )
    store.save_graph(
        repo,
        [
            GraphNode(
                node_id="symbol:large.py:indexed_symbol",
                kind="func",
                label="indexed_symbol",
                source_ref="large.py",
                granularity="symbol",
                signature="def indexed_symbol(value):",
                span_start=1,
                span_end=5,
            ),
            GraphNode(
                node_id="symbol:small.py:tiny",
                kind="func",
                label="tiny",
                source_ref="small.py",
                granularity="symbol",
                signature="def tiny():",
                span_start=1,
                span_end=2,
            ),
        ],
        [],
    )
    store.connection.close()
    return repo


def test_manifest_hook_and_advice_output_are_exactly_pretooluse_shaped(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    result = _run(repo, _event(repo, "Grep", {"pattern": "indexed_symbol"}), monkeypatch)
    payload = _payload(result)
    specific = payload["hookSpecificOutput"]

    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "allow"
    assert "additionalContext" in specific
    context = specific["additionalContext"]
    assert 'cortex_search_symbols({"query":"indexed_symbol"' in context
    assert 'cortex_references({"symbol":"indexed_symbol","budget":2000})' in context
    assert "tokens" in context


def test_all_modes_and_stale_enforce_downgrade(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)

    off = _run(repo, _event(repo, "Grep", {"pattern": "indexed_symbol"}), monkeypatch, CORTEX_HOOK_MODE="off")
    assert off.returncode == 0 and off.stdout == "" and off.stderr == ""

    denied = _run(
        repo,
        _event(repo, "Grep", {"pattern": "indexed_symbol"}),
        monkeypatch,
        CORTEX_HOOK_MODE="enforce",
    )
    deny_payload = _payload(denied)
    deny_specific = deny_payload["hookSpecificOutput"]
    assert deny_specific["permissionDecision"] == "deny"
    assert "cortex_search_symbols" in deny_specific["permissionDecisionReason"]
    assert "cortex_references" in deny_specific["permissionDecisionReason"]

    with sqlite3.connect(default_db_path(repo)) as connection:
        connection.execute("UPDATE repos SET updated_at = ?", (int(time.time()) - 100_000,))
    downgraded = _run(
        repo,
        _event(repo, "Grep", {"pattern": "indexed_symbol"}),
        monkeypatch,
        CORTEX_HOOK_MODE="enforce",
        CORTEX_HOOK_STALE_AFTER_SECONDS="10",
    )
    downgrade_payload = _payload(downgraded)
    downgrade_specific = downgrade_payload["hookSpecificOutput"]
    assert downgrade_specific["permissionDecision"] == "allow"
    assert "downgraded" in downgrade_specific["additionalContext"]


def test_read_threshold_exact_skeleton_and_symbol_calls(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    below = _run(
        repo,
        _event(repo, "Read", {"file_path": "small.py"}),
        monkeypatch,
        CORTEX_HOOK_READ_THRESHOLD_BYTES="512",
    )
    assert _payload(below)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" not in _payload(below)["hookSpecificOutput"]

    result = _run(
        repo,
        _event(repo, "Read", {"file_path": "large.py"}),
        monkeypatch,
        CORTEX_HOOK_READ_THRESHOLD_BYTES="0",
    )
    context = _payload(result)["hookSpecificOutput"]["additionalContext"]
    assert 'cortex_read_file({"path":"large.py","mode":"skeleton"})' in context
    assert 'cortex_read_symbol({"symbol":"symbol:large.py:indexed_symbol","mode":"skeleton"})' in context
    assert "raw ~" in context and "skeleton ~" in context


def test_unknowns_plain_globs_and_regexes_are_silent(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    events = [
        _event(repo, "Grep", {"pattern": "not_indexed"}),
        _event(repo, "Grep", {"pattern": "indexed_symbol|secret"}),
        _event(repo, "Glob", {"pattern": "**/*.py"}),
        _event(repo, "Read", {"file_path": "missing.py"}),
        _event(repo, "Bash", {"command": "grep indexed_symbol"}),
    ]
    for event in events:
        result = _run(repo, event, monkeypatch)
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_grep_and_glob_scope_filters_indexed_matches_and_never_denies(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    scoped_cases = [
        ("Grep", {"pattern": "indexed_symbol", "path": "small.py"}),
        ("Glob", {"pattern": "**/*indexed_symbol*", "path": "small.py"}),
        ("Grep", {"pattern": "indexed_symbol", "path": "not-indexed"}),
        ("Glob", {"pattern": "**/*indexed_symbol*", "path": "not-indexed"}),
        ("Grep", {"pattern": "indexed_symbol", "path": str(tmp_path / "outside")}),
        ("Glob", {"pattern": "**/*indexed_symbol*", "path": str(tmp_path / "outside")}),
    ]
    for tool, tool_input in scoped_cases:
        result = _run(repo, _event(repo, tool, tool_input), monkeypatch, CORTEX_HOOK_MODE="enforce")
        assert result.returncode == 0
        assert result.stdout == ""


def test_indexed_directory_scope_is_positive_but_not_enforceable(tmp_path, monkeypatch):
    repo = _build_qt_app(tmp_path)
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    ingest_repository(repo, commit_limit=20)

    cases = [
        ("Grep", {"pattern": "DeviceManager", "path": "src"}),
        ("Glob", {"pattern": "**/*DeviceManager*", "path": "src"}),
    ]
    for tool, tool_input in cases:
        advised = _payload(_run(repo, _event(repo, tool, tool_input), monkeypatch))
        advised_specific = advised["hookSpecificOutput"]
        assert advised_specific["permissionDecision"] == "allow"
        assert "within indexed scope `src`" in advised_specific["additionalContext"]

        enforced = _payload(
            _run(repo, _event(repo, tool, tool_input), monkeypatch, CORTEX_HOOK_MODE="enforce")
        )
        assert enforced["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "additionalContext" in enforced["hookSpecificOutput"]


def test_unrepresentable_search_options_never_deny_in_enforce(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    grep = _payload(
        _run(
            repo,
            _event(repo, "Grep", {"pattern": "indexed_symbol", "type": "py"}),
            monkeypatch,
            CORTEX_HOOK_MODE="enforce",
        )
    )
    assert grep["hookSpecificOutput"]["permissionDecision"] != "deny"

    read = _payload(
        _run(
            repo,
            _event(repo, "Read", {"file_path": "large.py", "offset": 10}),
            monkeypatch,
            CORTEX_HOOK_MODE="enforce",
            CORTEX_HOOK_READ_THRESHOLD_BYTES="0",
        )
    )
    assert read["hookSpecificOutput"]["permissionDecision"] != "deny"


def test_qt_signal_handler_and_cpp_read_redirects(tmp_path, monkeypatch):
    repo = _build_qt_app(tmp_path)
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    ingest_repository(repo, commit_limit=20)

    for pattern in ("deviceConnected", "onClicked"):
        result = _run(repo, _event(repo, "Grep", {"pattern": pattern}), monkeypatch)
        context = _payload(result)["hookSpecificOutput"]["additionalContext"]
        assert "cortex_search_symbols" in context
        assert "cortex_references" in context

    result = _run(repo, _event(repo, "Read", {"file_path": "src/DeviceManager.cpp"}), monkeypatch)
    context = _payload(result)["hookSpecificOutput"]["additionalContext"]
    assert 'cortex_read_file({"path":"src/DeviceManager.cpp","mode":"skeleton"})' in context
    assert "cortex_read_symbol" in context


def test_missing_corrupt_locked_and_non_git_inputs_fail_open(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    missing = _run(repo, _event(repo, "Grep", {"pattern": "anything"}), monkeypatch)
    assert missing.stdout == ""

    db_path = default_db_path(repo)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not sqlite")
    corrupt = _run(repo, _event(repo, "Grep", {"pattern": "anything"}), monkeypatch)
    assert corrupt.stdout == ""

    # Build a valid DB, then hold an exclusive transaction in another process
    # so the hook's short SQLite timeout is exercised.
    repo = _seed_repo(tmp_path / "locked", monkeypatch)
    db_path = default_db_path(repo)
    ready = tmp_path / "lock-ready"
    holder_code = (
        "import pathlib, sqlite3, sys, time; "
        "c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN EXCLUSIVE'); "
        "pathlib.Path(sys.argv[2]).write_text('ready'); time.sleep(3)"
    )
    holder = subprocess.Popen([sys.executable, "-c", holder_code, str(db_path), str(ready)])
    try:
        deadline = time.time() + 2
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.01)
        locked = _run(repo, _event(repo, "Grep", {"pattern": "indexed_symbol"}), monkeypatch)
        assert locked.returncode == 0
        assert locked.stdout == ""
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    plain = tmp_path / "plain"
    plain.mkdir()
    non_git = _run(plain, _event(plain, "Grep", {"pattern": "indexed_symbol"}), monkeypatch)
    assert non_git.stdout == ""

    malformed = subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=repo,
        input="not-json",
        text=True,
        capture_output=True,
        timeout=5,
        env=os.environ.copy(),
    )
    assert malformed.returncode == 0 and malformed.stdout == "" and malformed.stderr == ""


def test_jsonl_schema_logs_passes_and_logging_failure_is_fail_soft(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    # Indexed pass decisions are logged even when they emit no advice.
    result = _run(repo, _event(repo, "Glob", {"pattern": "**/*.py"}), monkeypatch)
    assert result.stdout == ""
    log_path = repo_data_dir(repo) / "usage.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    row = rows[-1]
    assert {
        "timestamp",
        "repo",
        "tool",
        "normalized_target",
        "action",
        "reason",
        "mode",
        "freshness",
        "index_age_seconds",
        "match_count",
        "estimated_tokens",
    } <= row.keys()
    assert row["action"] == "pass"
    assert row["tool"] == "Glob"

    unknown = _run(repo, _event(repo, "Grep", {"pattern": "notIndexed"}), monkeypatch)
    assert unknown.stdout == ""
    unknown_row = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert unknown_row["action"] == "pass"
    assert unknown_row["normalized_target"] == "notindexed"

    # Turn usage.jsonl into a directory: advice must still be returned.
    log_path.unlink()
    log_path.mkdir()
    advised = _run(repo, _event(repo, "Grep", {"pattern": "indexed_symbol"}), monkeypatch)
    assert "additionalContext" in _payload(advised)["hookSpecificOutput"]


def test_db_and_target_are_not_mutated_and_warm_decision_is_fast(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path, monkeypatch)
    db_path = default_db_path(repo)
    before = hashlib.sha256(db_path.read_bytes()).digest()
    target_before = (repo / "large.py").read_bytes()

    result = _run(repo, _event(repo, "Grep", {"pattern": "indexed_symbol"}), monkeypatch)
    assert result.returncode == 0
    assert hashlib.sha256(db_path.read_bytes()).digest() == before
    assert (repo / "large.py").read_bytes() == target_before

    # Import/startup is intentionally excluded from the warm target.  The
    # subprocess test above remains the honest end-to-end path.
    spec = importlib.util.spec_from_file_location("cortex_pre_tool_use_test", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    event = _event(repo, "Grep", {"pattern": "indexed_symbol"})
    module.process_event(event)  # warm the import/SQLite path once
    started = time.perf_counter()
    for _ in range(5):
        assert module.process_event(event) is not None
    warm_ms = (time.perf_counter() - started) * 1000 / 5
    assert warm_ms < 50, f"warm hook decision exceeded 50ms: {warm_ms:.2f}ms"
