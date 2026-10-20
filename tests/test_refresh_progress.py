from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import server


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for index in range(7):
        (root / f"m{index}.py").write_text(f"def f{index}():\n    return {index}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_full_ingest_reports_stages_in_order(repo):
    events: list[tuple[str, int, int]] = []
    ingest_repository(repo, progress=lambda stage, done, total: events.append((stage, done, total)))
    stages = []
    for stage, _done, _total in events:
        if not stages or stages[-1] != stage:
            stages.append(stage)
    assert stages == ["scan", "parse", "resolve", "cochange", "write"]
    parse = [(done, total) for stage, done, total in events if stage == "parse"]
    assert parse[0] == (0, 7)
    assert parse[-1] == (7, 7)


def test_incremental_ingest_reports_progress(repo):
    ingest_repository(repo)
    (repo / "m1.py").write_text("def f1():\n    return 99\n")
    events: list[str] = []
    ingest_repository(repo, incremental=True, progress=lambda stage, done, total: events.append(stage))
    assert events[0] == "scan"
    assert "parse" in events
    assert events[-1] == "write"


def test_refresh_sends_progress_notifications_before_its_result(repo):
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "cortex_refresh",
            "arguments": {"repo_path": str(repo), "mode": "full"},
            "_meta": {"progressToken": "tok-1"},
        },
    }
    out = io.StringIO()
    server.serve(iter([json.dumps(request) + "\n"]), out)
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    notifications = [f for f in frames if f.get("method") == "notifications/progress"]
    responses = [f for f in frames if f.get("id") == 1]
    assert responses and not responses[0]["result"].get("isError")
    assert notifications, "expected at least one progress notification"
    assert all(n["params"]["progressToken"] == "tok-1" for n in notifications)
    progress = [n["params"]["progress"] for n in notifications]
    assert progress == sorted(progress) and len(set(progress)) == len(progress)
    assert frames.index(notifications[-1]) < frames.index(responses[0])


def test_no_progress_token_means_no_notifications(repo):
    request = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "cortex_refresh", "arguments": {"repo_path": str(repo), "mode": "full"}},
    }
    out = io.StringIO()
    server.serve(iter([json.dumps(request) + "\n"]), out)
    frames = [json.loads(line) for line in out.getvalue().splitlines()]
    assert not [f for f in frames if f.get("method") == "notifications/progress"]
