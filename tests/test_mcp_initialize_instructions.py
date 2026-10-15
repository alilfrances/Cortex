from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_live_initialize_returns_server_instructions() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05"},
    }
    result = subprocess.run(
        [sys.executable, str(repo_root / "bin" / "cortex-mcp.py")],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        cwd=repo_root,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout.strip())
    assert response["id"] == 1
    instructions = response["result"]["instructions"]
    assert instructions
    assert "cortex_query" in instructions
    assert "Grep" in instructions
    assert len(instructions) < 2000


def test_protocol_version_negotiation() -> None:
    from cortex.mcp.server import SUPPORTED_PROTOCOL_VERSIONS, _handle

    def negotiated(params: dict) -> dict:
        response = _handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
        assert response is not None
        return response["result"]

    assert SUPPORTED_PROTOCOL_VERSIONS == ("2025-06-18", "2025-03-26", "2024-11-05")
    for version in SUPPORTED_PROTOCOL_VERSIONS:
        assert negotiated({"protocolVersion": version})["protocolVersion"] == version
    for unsupported in ({"protocolVersion": "2023-01-01"}, {"protocolVersion": "2099-01-01"}, {"protocolVersion": 5}, {}):
        assert negotiated(unsupported)["protocolVersion"] == "2025-06-18"
    assert negotiated({"protocolVersion": "2025-06-18"})["capabilities"] == {"tools": {"listChanged": False}}


def test_live_initialize_negotiates_unsupported_version_to_latest() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2023-01-01"}}
    result = subprocess.run(
        [sys.executable, str(repo_root / "bin" / "cortex-mcp.py")],
        input=json.dumps(request) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
        cwd=repo_root,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip())["result"]["protocolVersion"] == "2025-06-18"
