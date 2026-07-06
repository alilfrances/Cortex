from __future__ import annotations

import sys
from pathlib import Path

from cortex.cli import _watch_polling


def test_polling_watch_detects_fingerprint_change_and_refreshes_once(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "watchdog", None)
    repo = tmp_path / "repo"
    repo.mkdir()
    watched = repo / "app.py"
    watched.write_text("print('one')\n", encoding="utf-8")
    refreshes: list[Path] = []
    sleeps = 0

    def refresh(path: Path) -> None:
        refreshes.append(path)

    def sleep(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            watched.write_text("print('two')\n", encoding="utf-8")

    _watch_polling(repo, interval=0.01, refresh=refresh, sleep=sleep, max_refreshes=1)

    assert refreshes == [repo]
