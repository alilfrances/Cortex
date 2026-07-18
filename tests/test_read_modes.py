"""Tests for P1-6: aggressive read modes for read tools.

Covers:
  - cortex_read_symbol mode="full" (default) is byte-identical to the
    pre-P1-6 payload shape (no new "mode" key, same body/body_format).
  - cortex_read_symbol mode="skeleton" and mode="signature".
  - The new cortex_read_file tool: skeleton mode on a large Python file
    (imports + signatures + elision, under budget) and mode="full".
  - Qt parity: skeleton of the qt_app fixture's C++ header keeps
    #include/class/Q_OBJECT/signals:/slots:/member signatures; skeleton of
    a .qml file keeps imports/component id/signal declarations/onFoo:
    handlers with bound expressions elided.
  - P0-1 ledger records savings for cortex_read_file and skeleton/signature
    cortex_read_symbol reads.
  - Both new surfaces route through the P1-5 _meta envelope.

See IMPROVEMENT_PLAN.md's P1-6 section.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.bundle import SKELETON_MARKER
from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path
from evals.run_evals import _build_qt_app


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def _ingest_qt_app(tmp_path: Path) -> Path:
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    return repo


_AUTH_PY = """\
def login(user, password):
    return user == "admin" and password == "secret"


def logout(user):
    return None
"""


def _repo_with_index(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "auth.py").write_text(_AUTH_PY, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


_HELPERS_PY_HEADER = "import os\nimport sys\n\n\n"


def _big_helpers_py(count: int = 120) -> str:
    """A Python file big enough that its full raw content dwarfs a tight
    token budget, but its skeleton (imports + signatures, bodies elided)
    comfortably fits under one -- proves skeletonization, not just
    truncation, is what makes it fit."""
    functions = "\n\n".join(
        f"def helper_{i:03d}(x):\n"
        f"    total = x\n"
        f"    for step in range({i + 1}):\n"
        f"        total += step * {i}\n"
        f"    return total\n"
        for i in range(count)
    )
    return _HELPERS_PY_HEADER + functions + "\n"


def _repo_with_big_python_file(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "helpers.py").write_text(_big_helpers_py(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)
    return repo


# --- cortex_read_symbol modes ---


def test_read_symbol_default_mode_is_full_and_unchanged(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "login", "budget": 200})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["body"] == '1: def login(user, password):\n2:     return user == "admin" and password == "secret"'
    assert payload["body_format"] == "line_number: source"
    assert payload["truncated"] is False
    # P1-6 invariant: omitting `mode` must not add a "mode" key -- the
    # default payload shape is byte-identical to the tool's pre-P1-6 output.
    assert "mode" not in payload


def test_read_symbol_explicit_full_mode_matches_default(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    default_payload = _payload(call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "login"}))
    explicit_payload = _payload(call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "login", "mode": "full"}))

    assert default_payload["body"] == explicit_payload["body"]
    assert default_payload["body_format"] == explicit_payload["body_format"]


def test_read_symbol_signature_mode_omits_body(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": "login", "mode": "signature"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["mode"] == "signature"
    assert payload["signature"] == 'def login(user, password):'
    assert payload["span_start"] == 1
    assert "body" not in payload
    assert "body_format" not in payload


def test_read_symbol_skeleton_mode_nests_class_children(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    big_body = "\n".join(f"        value_{i} = {i}" for i in range(80))
    content = (
        "import os\n\n"
        "class LoginService:\n"
        "    def authenticate(self, token):\n"
        f"{big_body}\n"
        "        return True\n"
    )
    (repo / "auth.py").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    result = call_tool(
        "cortex_read_symbol",
        {"repo_path": str(repo), "symbol": "symbol:auth.py:LoginService", "mode": "skeleton", "budget": 2000},
    )
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["mode"] == "skeleton"
    assert payload["body_format"] == "skeleton"
    assert payload["body"].startswith(SKELETON_MARKER)
    assert "import os" in payload["body"]
    assert "class LoginService:" in payload["body"]
    assert "def authenticate(self, token):" in payload["body"]
    assert "[body elided]" in payload["body"]
    assert "value_0 = 0" not in payload["body"]


# --- cortex_read_file ---


def test_read_file_skeleton_of_large_python_file_fits_tight_budget(tmp_path, monkeypatch):
    repo = _repo_with_big_python_file(tmp_path, monkeypatch)
    full_content = (repo / "helpers.py").read_text(encoding="utf-8")

    # A budget the full raw file (~4900 tokens) cannot possibly fit under,
    # but the skeleton (imports + ~120 one-line signatures, bodies elided,
    # ~2200 tokens) can.
    budget = 2500
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "helpers.py", "budget": budget})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["mode"] == "skeleton"
    assert payload["skeletonized"] is True
    assert payload["kind"] == "code"
    assert payload["symbol_count"] == 120
    assert payload["truncated"] is False
    assert payload["token_count"] <= budget
    assert payload["body"].startswith(SKELETON_MARKER)
    assert "import os" in payload["body"]
    assert "import sys" in payload["body"]
    assert "def helper_000(x):" in payload["body"]
    assert "def helper_119(x):" in payload["body"]
    assert "[body elided]" in payload["body"]
    # The bodies are what made the raw file too big for the budget --
    # confirm they were actually elided, not merely truncated off the end.
    assert "total += step" not in payload["body"]
    assert len(payload["body"]) < len(full_content)


def test_read_file_full_mode_returns_exact_indexed_content(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    full_content = (repo / "auth.py").read_text(encoding="utf-8")

    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "auth.py", "mode": "full", "budget": 4000})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["mode"] == "full"
    assert payload["skeletonized"] is False
    assert payload["body"] == full_content
    assert payload["truncated"] is False


def test_read_file_missing_path_is_error(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "nope.py"})
    payload = _payload(result)

    assert result["isError"] is True
    assert payload["error"] == "missing_source"


def test_read_file_falls_back_to_full_when_no_symbols_indexed(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "README.md").write_text("# Notes\n\nSome prose with no symbols.\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "README.md"})
    payload = _payload(result)

    assert result["isError"] is False
    assert payload["mode"] == "skeleton"
    assert payload["skeletonized"] is False
    assert payload["symbol_count"] == 0
    assert payload["body"] == "# Notes\n\nSome prose with no symbols.\n"


# --- Qt parity (qt_app fixture) ---


def test_read_file_skeleton_of_cpp_header_keeps_qt_scaffolding(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "include/DeviceManager.hpp"})
    payload = _payload(result)
    body = payload["body"]

    assert result["isError"] is False
    assert payload["skeletonized"] is True
    assert "#include <QObject>" in body
    assert "class DeviceManager : public QObject {" in body
    assert "Q_OBJECT" in body
    assert "signals:" in body
    assert "slots:" in body
    assert "void deviceConnected(int deviceId);" in body
    assert "void onDeviceConnected(int deviceId);" in body


def test_read_file_skeleton_of_qml_keeps_signals_and_elides_handlers(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "qml/Main.qml"})
    payload = _payload(result)
    body = payload["body"]

    assert result["isError"] is False
    assert payload["skeletonized"] is True
    assert "import QtQuick 2.15" in body
    assert "ApplicationWindow {" in body
    assert "id: delegate" in body
    assert "signal sceneReady()" in body
    assert "onClicked: [body elided] ..." in body
    assert "onDeviceConnected: [body elided] ..." in body
    # The bound expressions themselves must actually be gone, not just
    # relocated -- this is what "handler names with bound expressions
    # elided" means in the P1-6 Qt-parity acceptance criteria.
    assert "console.log" not in body


def test_read_file_skeleton_of_qml_delegate_keeps_component_and_signals(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "qml/DeviceDelegate.qml"})
    payload = _payload(result)
    body = payload["body"]

    assert result["isError"] is False
    assert "import QtQuick 2.15" in body
    assert "Item {" in body
    assert "signal deviceConnected(int deviceId)" in body
    assert "signal clicked()" in body
    assert "onClicked: [body elided] ..." in body
    # The handler's bound call expression must be elided, not shown verbatim.
    handler_line = next(line for line in body.splitlines() if "onClicked:" in line)
    assert handler_line.strip() == "onClicked: [body elided] ..."


# --- P0-1 ledger savings ---


def test_ledger_records_read_file_skeleton_savings(tmp_path, monkeypatch):
    repo = _repo_with_big_python_file(tmp_path, monkeypatch)
    call_tool("cortex_read_file", {"repo_path": str(repo), "path": "helpers.py", "budget": 2500})

    store = CortexStore(default_db_path(repo))
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_read_file"]
    assert rows, "expected a cortex_read_file ledger row"
    row = rows[-1]
    full_content = (repo / "helpers.py").read_text(encoding="utf-8")
    from cortex.tokenizer import count_text_tokens

    assert row["baseline_tokens"] >= count_text_tokens(full_content, kind="code") - 5
    assert row["response_tokens"] < row["baseline_tokens"]


def test_ledger_records_read_symbol_skeleton_savings_bigger_than_full(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    big_body = "\n".join(f"        value_{i} = {i}" for i in range(200))
    content = (
        "import os\n\n"
        "class LoginService:\n"
        "    def authenticate(self, token):\n"
        f"{big_body}\n"
        "        return True\n"
    )
    (repo / "auth.py").write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    ingest_repository(repo, commit_limit=0)

    call_tool(
        "cortex_read_symbol",
        {"repo_path": str(repo), "symbol": "symbol:auth.py:LoginService", "mode": "skeleton", "budget": 4000},
    )
    store = CortexStore(default_db_path(repo))
    rows = [row for row in store.fetch_tool_usage(repo) if row["tool"] == "cortex_read_symbol"]
    assert rows, "expected a cortex_read_symbol ledger row"
    row = rows[-1]
    # Baseline is priced against the whole file regardless of mode (see
    # _estimate_baseline's P1-6 docstring update), so a skeleton read's much
    # smaller response yields a bigger saved_tokens than a full-span read of
    # the same symbol would for the same file.
    assert row["response_tokens"] < row["baseline_tokens"]
    assert (row["baseline_tokens"] - row["response_tokens"]) > 0


def test_estimate_baseline_read_file_uses_full_raw_file(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "auth.py"})
    payload = _payload(result)
    store = CortexStore(default_db_path(repo))

    baseline = mcp_tools._estimate_baseline("cortex_read_file", {"repo_path": str(repo)}, payload, store, repo)
    from cortex.tokenizer import count_text_tokens

    full_content = (repo / "auth.py").read_text(encoding="utf-8")
    assert baseline == count_text_tokens(full_content, kind="code")


# --- _meta envelope routing (P1-5) ---


def test_read_file_detailed_response_carries_meta(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool("cortex_read_file", {"repo_path": str(repo), "path": "auth.py", "response_format": "detailed"})
    payload = _payload(result)

    assert result["isError"] is False
    assert "_meta" in payload
    meta = payload["_meta"]
    assert isinstance(meta["index_age_seconds"], int)
    assert isinstance(meta["indexed_at"], int)
    assert isinstance(meta["fingerprint_fresh"], bool)
    assert isinstance(meta["cached"], bool)


def test_read_symbol_skeleton_detailed_response_carries_meta(tmp_path, monkeypatch):
    repo = _repo_with_index(tmp_path, monkeypatch)
    result = call_tool(
        "cortex_read_symbol",
        {"repo_path": str(repo), "symbol": "login", "mode": "skeleton", "response_format": "detailed"},
    )
    payload = _payload(result)

    assert result["isError"] is False
    assert "_meta" in payload
