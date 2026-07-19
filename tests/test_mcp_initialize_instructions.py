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
