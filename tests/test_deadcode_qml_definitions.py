from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository

FIXTURE = Path(__file__).parent / "fixtures" / "qml_dead_code"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    return repo


def test_only_definitions_are_dead_code_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _repo(tmp_path)
    ingest_repository(repo)

    findings = analyze_dead_code(repo)["findings"]
    symbols = [item["symbol"] for item in findings]

    for non_definition in ("ScrollBar.horizontal.policy", "ScrollBar.vertical", "QtQuick", "QtQuick.Controls", "i", "list"):
        assert non_definition not in symbols, non_definition
    assert len(symbols) == len(set(symbols)), "no symbol is reported twice"
    assert "unusedHelper" in symbols
    assert "usedHelper" not in symbols
