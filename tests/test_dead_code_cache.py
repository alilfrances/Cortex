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
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.py").write_text("def used():\n    return 1\n\n\ndef unused_helper():\n    return 2\n\n\nused()\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _count_analyses(monkeypatch) -> list[int]:
    calls = [0]
    real = tools.analyze_dead_code

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(tools, "analyze_dead_code", counted)
    return calls


def _payload(name: str, repo: Path, **args) -> dict:
    return json.loads(tools.call_tool(name, {"repo_path": str(repo), **args})["content"][0]["text"])


def test_repeat_dead_code_call_analyses_once(repo, monkeypatch):
    calls = _count_analyses(monkeypatch)
    first = _payload("cortex_dead_code", repo)
    second = _payload("cortex_dead_code", repo)
    assert calls[0] == 1
    assert first["findings"] == second["findings"]
    assert any(item["symbol"] == "unused_helper" for item in first["findings"])


def test_dead_code_after_overview_reuses_the_overview_analysis(repo, monkeypatch):
    from cortex import report

    calls = [0]
    real = report.analyze_dead_code

    def counted(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(report, "analyze_dead_code", counted)
    counted_tools = _count_analyses(monkeypatch)
    _payload("cortex_overview", repo)
    result = _payload("cortex_dead_code", repo)
    assert calls[0] == 1
    assert counted_tools[0] == 0
    assert any(item["symbol"] == "unused_helper" for item in result["findings"])


# --- a stopped analysis is kept and continued (0.12) ---


def _stop_after(monkeypatch, candidates: int) -> None:
    """Make every analysis stop after `candidates` polls, as a short deadline would."""

    def should_stop_factory():
        polls = [0]

        def should_stop() -> bool:
            polls[0] += 1
            return polls[0] > candidates

        return should_stop

    monkeypatch.setattr(tools, "_analysis_should_stop", should_stop_factory)


@pytest.fixture
def big_repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data2"))
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    root = tmp_path / "bigrepo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for i in range(6):
        (root / f"m{i}.py").write_text("".join(f"def unused_{i}_{j}():\n    return {j}\n\n\n" for j in range(5)))
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def test_repeated_partial_overviews_continue_and_converge(big_repo: Path, monkeypatch) -> None:
    from cortex import report

    builds = [0]
    real_build = report.build_report_data

    def counted(*args, **kwargs):
        builds[0] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(tools, "build_report_data", counted)
    saves = [0]
    from cortex.store import CortexStore

    real_save = CortexStore.save_communities
    monkeypatch.setattr(CortexStore, "save_communities", lambda self, *a, **k: (saves.__setitem__(0, saves[0] + 1), real_save(self, *a, **k))[1])
    real_should_stop = tools._analysis_should_stop
    _stop_after(monkeypatch, 8)

    processed: list[int] = []
    for _ in range(40):
        payload = _payload("cortex_overview", big_repo, response_format="detailed")
        if not payload.get("dead_code_partial"):
            break
        processed.append(payload["dead_code_progress"]["processed"])
    else:
        raise AssertionError("the overview never completed")

    assert len(processed) >= 2 and processed == sorted(set(processed))  # advances every call
    assert builds[0] == 1 and saves[0] == 1  # communities, hotspots etc. were computed once
    assert payload["dead_code_total"] == 30

    monkeypatch.setattr(tools, "_analysis_should_stop", real_should_stop)
    again = _payload("cortex_overview", big_repo, response_format="detailed")
    assert again["_meta"]["cached"] is True and again["dead_code_total"] == 30


def test_repo_wide_dead_code_continues_its_own_stopped_run(big_repo: Path, monkeypatch) -> None:
    _stop_after(monkeypatch, 8)
    seen: list[int] = []
    for _ in range(40):
        payload = _payload("cortex_dead_code", big_repo, budget=100000)
        if "note" not in payload:
            break
        seen.append(payload["returned_count"])
    else:
        raise AssertionError("dead code never completed")

    assert len(seen) >= 2 and seen == sorted(seen)
    assert payload["returned_count"] == 30
