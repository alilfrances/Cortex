"""In-process graph cache for the read-only MCP tools (0.12 plan, Task 3.1)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools as mcp_tools
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path

FILES = {
    "auth.py": "def verify(user):\n    return user is not None\n\n\ndef login(user):\n    return verify(user)\n",
    "billing.py": "from auth import login\n\n\ndef charge(user):\n    return login(user)\n",
    "tests/test_billing.py": "from billing import charge\n\n\ndef test_charge():\n    assert charge({})\n",
    "src/widget.h": (
        "#pragma once\n#include <QObject>\nclass Widget : public QObject\n{\n    Q_OBJECT\n"
        "    Q_PROPERTY(int value READ value)\npublic:\n    int value() const;\n    void refresh();\n};\n"
    ),
    "src/widget.cpp": (
        '#include "widget.h"\nint Widget::value() const { return 1; }\nvoid Widget::refresh() { value(); }\n'
    ),
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CORTEX_GRAPH_CACHE", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    ingest_repository(root, commit_limit=0)
    mcp_tools._graph_cache = None
    return root


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    original = CortexStore.fetch_graph

    def counting(self, repo_path):
        calls.append(1)
        return original(self, repo_path)

    monkeypatch.setattr(CortexStore, "fetch_graph", counting)
    return calls


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_consecutive_calls_load_the_graph_once(repo: Path, fetches: list[int]) -> None:
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    call_tool("cortex_path", {"repo_path": str(repo), "symbol_a": "charge", "symbol_b": "verify"})

    assert len(fetches) == 1
    assert mcp_tools.graph_cache_info()["nodes"] > 0


def test_refresh_after_an_edit_reloads_the_graph(repo: Path, fetches: list[int]) -> None:
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    (repo / "auth.py").write_text(FILES["auth.py"] + "\n\ndef logout(user):\n    return None\n", encoding="utf-8")
    call_tool("cortex_refresh", {"repo_path": str(repo)})
    after_refresh = len(fetches)  # the incremental ingest loads the graph itself
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})

    assert len(fetches) == after_refresh + 1
    nodes, _ = mcp_tools._graph_cache[1]
    assert any(node.label == "logout" for node in nodes)


def test_another_process_ingest_changes_the_key(repo: Path, fetches: list[int]) -> None:
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    (repo / "auth.py").write_text(FILES["auth.py"] + "\n\ndef logout(user):\n    return None\n", encoding="utf-8")
    ingest_repository(repo, commit_limit=0)  # not through call_tool: the cache is not cleared
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})

    assert len(fetches) == 2


def test_kill_switch_disables_the_cache(repo: Path, fetches: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_GRAPH_CACHE", "0")
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    call_tool("cortex_path", {"repo_path": str(repo), "symbol_a": "charge", "symbol_b": "verify"})

    assert len(fetches) == 2
    assert mcp_tools.graph_cache_info() is None


def _snapshot(graph) -> str:
    nodes, edges = graph
    return json.dumps([[n.to_dict() for n in nodes], [e.to_dict() for e in edges]], sort_keys=True, default=str)


def test_read_tools_never_mutate_the_cached_graph(repo: Path) -> None:
    store = CortexStore(default_db_path(repo))
    calls = [
        ("cortex_query", {"task": "login verify widget"}),
        ("cortex_impact", {"path": "auth.py"}),
        ("cortex_impact", {"path": "src/widget.h"}),
        ("cortex_context", {"targets": ["auth.py", "src/widget.h"]}),
        ("cortex_risk", {"range": "HEAD~0..HEAD"}),
        ("cortex_path", {"symbol_a": "charge", "symbol_b": "verify"}),
        ("cortex_dead_code", {}),
        ("cortex_dead_code", {"path": "src"}),
        ("cortex_overview", {}),
    ]
    for response_format in ("concise", "detailed"):
        for name, extra in calls:
            call_tool(name, {"repo_path": str(repo), "response_format": response_format, **extra})

    assert mcp_tools._graph_cache is not None
    assert _snapshot(mcp_tools._graph_cache[1]) == _snapshot(store.fetch_graph(repo))


def test_status_reports_the_cache(repo: Path) -> None:
    assert _payload(call_tool("cortex_status", {}))["graph_cache"] is None
    call_tool("cortex_impact", {"repo_path": str(repo), "path": "auth.py"})
    info = _payload(call_tool("cortex_status", {}))["graph_cache"]
    assert info["nodes"] > 0 and info["edges"] > 0
