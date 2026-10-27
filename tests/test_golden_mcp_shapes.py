"""MCP surface goldens at 7b0e6e0 (Task 0 baseline).

* ``tests/goldens/mcp/tools_list.json`` -- ``TOOL_DEFINITIONS`` exactly as ``tools/list`` serves it.
* ``tests/goldens/mcp/initialize.json`` -- ``initialize`` responses.  Today the server **echoes
  any** ``protocolVersion`` (even an unsupported one) and falls back to ``2024-11-05`` only when
  the client sends none; capabilities are ``{"tools": {}}``.  Task 3 changes negotiation.
* ``tests/goldens/mcp/shapes/<tool>.<format>.json`` -- sorted top-level key set plus first-item
  key sets (no values) for all 15 tools in ``concise`` and ``detailed``.  Backend-independent:
  the same file is compared under tree-sitter and under the regex fallback.
* ``tests/goldens/mcp/payloads/<backend>/<name>.<format>.json`` -- full scrubbed payloads for
  the deterministic tools, one directory per backend.

Fixtures are built in ``tmp_path`` with fixed git dates, ingested through the real pipeline,
and called through ``call_tool``.  The stdlib tokenizer is forced so ``_meta.saved_tokens``
presence (which decides whether a concise ``_meta`` appears) cannot depend on ``tiktoken``.
Run under ``CORTEX_FORCE_REGEX=1`` the tree-sitter parametrization is skipped and the regex
goldens are used; both run in a normal session.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from cortex.mcp.server import _handle
from cortex.mcp.tools import TOOL_DEFINITIONS
from evals.run_evals import _build_python_app, _build_qt_app
from tests.baseline_helpers import (
    FIXTURES,
    copy_fixture_repo,
    fixed_git_env,
    ingest,
    tool,
)
from tests.golden_utils import (
    assert_golden,
    force_stdlib_tokenizer,
    scrub,
    shape_of,
    treesitter_available,
)

QT_PATH_A = "symbol:qml/Main.qml:Main.DeviceDelegate.onDeviceConnected"
QT_PATH_B = "symbol:include/DeviceModel.hpp:onDeviceConnected"

# name -> (repo, tool, arguments, payload golden?)  Arguments exclude repo_path/response_format.
CASES: dict[str, tuple[str, str, dict[str, Any], bool]] = {
    "cortex_query": ("python_app", "cortex_query", {"task": "login token issuance and session audit"}, False),
    "cortex_overview": ("qt_app", "cortex_overview", {}, False),
    "cortex_context": ("qt_app", "cortex_context", {"targets": ["src/DeviceManager.cpp"]}, False),
    "cortex_impact": ("qt_app", "cortex_impact", {"path": "src/DeviceManager.cpp"}, True),
    "cortex_risk": ("qt_app", "cortex_risk", {"range": "HEAD~1..HEAD"}, True),
    "cortex_dead_code": ("qt_app", "cortex_dead_code", {}, True),
    "cortex_search_symbols": ("qt_app", "cortex_search_symbols", {"query": "DeviceManager", "limit": 5}, True),
    "cortex_read_symbol": (
        "python_app", "cortex_read_symbol", {"symbol": "symbol:app/auth.py:AuthService.login"}, False,
    ),
    "cortex_read_file": ("python_app", "cortex_read_file", {"path": "app/auth.py"}, False),
    "cortex_relations": ("qt_app", "cortex_relations", {"relation": "connects"}, True),
    "cortex_references": ("qt_app", "cortex_references", {"symbol": "deviceConnected"}, True),
    "cortex_search_text": ("python_app", "cortex_search_text", {"query": "retry"}, False),
    "cortex_path": ("qt_app", "cortex_path", {"symbol_a": QT_PATH_A, "symbol_b": QT_PATH_B}, True),
    "cortex_refresh": ("qt_app", "cortex_refresh", {}, False),
    "cortex_status": ("qt_app", "cortex_status", {}, False),
}

# Additional payload goldens: (golden name, repo, tool, arguments).
EXTRA_PAYLOADS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "cortex_relations.emits": ("qt_app", "cortex_relations", {"relation": "emits"}),
    "cortex_relations.inherits": ("qt_app", "cortex_relations", {"relation": "inherits"}),
    "cortex_relations.imports": ("qt_app", "cortex_relations", {"relation": "imports"}),
    "cortex_relations.handles": ("qt_app", "cortex_relations", {"relation": "handles"}),
    "cortex_relations.calls": ("qt_app", "cortex_relations", {"relation": "calls"}),
    "cortex_relations.python_calls": (
        "python_app", "cortex_relations", {"relation": "calls", "symbol": "login"},
    ),
    "cortex_path.handler_to_declaration": (
        "qt_app",
        "cortex_path",
        {
            "symbol_a": "symbol:qml/Main.qml:Main.DeviceDelegate.onClicked",
            "symbol_b": "symbol:qml/DeviceDelegate.qml:DeviceDelegate.clicked",
        },
    ),
    "cortex_references.python_login": ("python_app", "cortex_references", {"symbol": "login"}),
    "cortex_dead_code.python_app": ("python_app", "cortex_dead_code", {}),
    "cortex_dead_code.multilang_repo": ("multilang_repo", "cortex_dead_code", {}),
    "cortex_impact.python_app": ("python_app", "cortex_impact", {"path": "app/auth.py"}),
    "cortex_search_symbols.python_app": ("python_app", "cortex_search_symbols", {"query": "login", "limit": 5}),
}

BACKENDS = [
    pytest.param(
        "treesitter",
        marks=pytest.mark.skipif(not treesitter_available(), reason="managed tree-sitter runtime not usable here"),
    ),
    "regex",
]

# Per-process cache of built fixture repos is unsafe (each test has its own data dir), so
# repos are rebuilt per test; they are tiny.
_BUILDERS = {
    "qt_app": lambda base: _build_qt_app(base),
    "python_app": lambda base: _build_python_app(base),
    "multilang_repo": lambda base: copy_fixture_repo(FIXTURES / "multilang_repo", base / "multilang_repo"),
}


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, repo_name: str) -> Path:
    force_stdlib_tokenizer(monkeypatch)
    fixed_git_env(monkeypatch)
    if backend == "regex":
        monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    else:
        assert os.environ.get("CORTEX_FORCE_REGEX") != "1"
    repo = _BUILDERS[repo_name](tmp_path)
    ingest(repo)
    return repo


def _for_backend(arguments: dict[str, Any], backend: str) -> dict[str, Any]:
    """Symbol ids for QML handlers differ per backend.

    Observed: the regex backend nests a QML child under the *preceding sibling object*
    (``Main.DeviceManager.DeviceDelegate.onClicked``) while tree-sitter nests it under its
    parent (``Main.DeviceDelegate.onClicked``).  The regex ids are pinned as-is by the
    per-backend extraction goldens; here the ids are only translated so ``cortex_path``
    gets a real node on both backends.
    """
    if backend != "regex":
        return arguments
    return {
        key: value.replace("Main.DeviceDelegate.", "Main.DeviceManager.DeviceDelegate.")
        if isinstance(value, str) and key.startswith("symbol_")
        else value
        for key, value in arguments.items()
    }


def _call(repo: Path, tool_name: str, arguments: dict[str, Any], response_format: str) -> tuple[dict[str, Any], bool]:
    if tool_name == "cortex_status":
        return tool(tool_name, repo_path=str(repo), response_format=response_format)
    return tool(tool_name, repo_path=str(repo), response_format=response_format, **arguments)


def _golden_payload(tool_name: str, response_format: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Drop references' optional concise ledger envelope from content goldens.

    A resolved graph hit can make ``saved_tokens`` positive, which adds ``_meta``
    to a concise references response.  That accounting result varies across
    platforms even with the test tokenizer pinned; the envelope contract is
    covered separately by ``test_meta_envelope.py``.  Keep the references data
    itself fully golden while making its shape/payload snapshots portable.
    """
    if tool_name == "cortex_references" and response_format == "concise":
        return {key: value for key, value in payload.items() if key != "_meta"}
    return payload


# --------------------------------------------------------------------------- #
# tools/list and initialize
# --------------------------------------------------------------------------- #


def test_tools_list_golden() -> None:
    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response is not None and response["result"]["tools"] == TOOL_DEFINITIONS
    assert len({t["name"] for t in TOOL_DEFINITIONS}) == len(TOOL_DEFINITIONS)
    assert len(TOOL_DEFINITIONS) == 15
    assert sorted(CASES) == sorted(t["name"] for t in TOOL_DEFINITIONS)
    # Task 3 (0.10.0) added `title` + `annotations` to every tool (absent at 7b0e6e0).
    assert all(t["title"] == t["annotations"]["title"] for t in TOOL_DEFINITIONS)
    assert_golden("mcp/tools_list.json", json.loads(json.dumps(TOOL_DEFINITIONS)))


def test_initialize_golden() -> None:
    results: dict[str, Any] = {}
    for label, params in {
        "client_2024-11-05": {"protocolVersion": "2024-11-05"},
        "client_2025-06-18": {"protocolVersion": "2025-06-18"},
        "client_unsupported_2023-01-01": {"protocolVersion": "2023-01-01"},
        "client_sends_no_version": {},
    }.items():
        response = _handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": params})
        assert response is not None and response["id"] == 7
        results[label] = response["result"]

    # 7b0e6e0 echoed every client version verbatim (and "2024-11-05" when none was
    # sent), with capabilities {"tools": {}}.  Task 3 negotiates: a supported version
    # is echoed, anything else (or nothing) gets the server's newest.
    assert results["client_2024-11-05"]["protocolVersion"] == "2024-11-05"
    assert results["client_2025-06-18"]["protocolVersion"] == "2025-06-18"
    assert results["client_unsupported_2023-01-01"]["protocolVersion"] == "2025-06-18"
    assert results["client_sends_no_version"]["protocolVersion"] == "2025-06-18"
    assert all(r["capabilities"] == {"tools": {"listChanged": False}} for r in results.values())
    assert all(r["serverInfo"] == {"name": "cortex", "version": "0.15.2"} for r in results.values())
    assert_golden("mcp/initialize.json", results)


def test_notifications_and_unknown_methods() -> None:
    assert _handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _handle({"jsonrpc": "2.0", "method": "no/such"}) is None  # notification: no reply
    error = _handle({"jsonrpc": "2.0", "id": 3, "method": "no/such"})
    assert error == {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "Method not found: no/such"}}


# --------------------------------------------------------------------------- #
# per-tool response shapes (backend independent)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", sorted(CASES))
def test_tool_shape_goldens(name: str, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_name, tool_name, arguments, _ = CASES[name]
    repo = _prepare(tmp_path, monkeypatch, backend, repo_name)

    for response_format in ("concise", "detailed"):
        payload, is_error = _call(repo, tool_name, _for_backend(arguments, backend), response_format)
        assert is_error is False, payload
        stable_payload = _golden_payload(tool_name, response_format, payload)
        assert_golden(f"mcp/shapes/{name}.{response_format}.json", shape_of(stable_payload))


# --------------------------------------------------------------------------- #
# full payload goldens (values included) per backend
# --------------------------------------------------------------------------- #

PAYLOAD_CASES = {
    **{name: (repo, tool_name, args) for name, (repo, tool_name, args, wanted) in CASES.items() if wanted},
    **EXTRA_PAYLOADS,
}


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("name", sorted(PAYLOAD_CASES))
def test_tool_payload_goldens(name: str, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_name, tool_name, arguments = PAYLOAD_CASES[name]
    repo = _prepare(tmp_path, monkeypatch, backend, repo_name)

    for response_format in ("concise", "detailed"):
        payload, is_error = _call(repo, tool_name, _for_backend(arguments, backend), response_format)
        assert is_error is False, payload
        stable_payload = _golden_payload(tool_name, response_format, payload)
        assert_golden(
            f"mcp/payloads/{backend}/{name}.{response_format}.json",
            scrub(stable_payload, roots=(repo, tmp_path)),
        )


# --------------------------------------------------------------------------- #
# today's behaviours worth naming explicitly
# --------------------------------------------------------------------------- #


def test_error_payload_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_stdlib_tokenizer(monkeypatch)
    unknown, is_error = tool("cortex_nope")
    assert is_error is True and unknown["error"] == "unknown_tool"

    plain = tmp_path / "plain"
    plain.mkdir()
    # A git repo that was never ingested: the missing-db error, not an exception.
    from tests.baseline_helpers import commit_all, init_git

    fixed_git_env(monkeypatch)
    repo = tmp_path / "unindexed"
    init_git(repo)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    commit_all(repo, "one")
    missing, is_error = tool("cortex_query", repo_path=str(repo), task="anything")
    assert sorted(missing) == ["error", "hint", "message", "repo_path"]
    assert missing["error"] == "missing_db"
