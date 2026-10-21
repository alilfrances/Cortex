from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex.bundle import generate_bundle
from cortex.ingest import ingest_repository

CATALOGUE = (
    '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="de_DE">\n'
    + "".join(
        f"<message><source>Device card reconnect state wrong {i}</source><translation>Geraet Verbindung Zustand {i}</translation></message>\n"
        for i in range(120)
    )
    + "</TS>\n"
)

DEVICE = (
    "// device card reconnect state wrong\n"
    "class Core {\n"
    "public:\n"
    "    void apply(int value) { current_ = value; }\n"
    "private:\n"
    "    int current_ = 0;\n"
    "};\n"
)


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    (root / "i18n").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "i18n" / "app_de.ts").write_text(CATALOGUE)
    (root / "src" / "core.hpp").write_text(DEVICE)
    (root / "src" / "device_state_reconnect.py").write_text("")  # empty file whose path matches every task word
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _paths(repo: Path, task: str, budget: int = 4000) -> list[str]:
    bundle = generate_bundle(repo_path=repo, task=task, budget=budget, output_format="json")
    return [item["path"] for item in bundle["items"]]


def _keyword_score(repo: Path, task: str, path: str) -> float:
    bundle = generate_bundle(repo_path=repo, task=task, budget=4000, output_format="json")
    item = next(item for item in bundle["items"] if item["path"] == path)
    return item["score"] - item["metadata"]["graph_bonus"]


def test_translation_catalogue_is_demoted_for_a_non_translation_task(repo, monkeypatch):
    # This used to assert that core.hpp is listed before app_de.ts. In this 3-file repo that
    # ordering came from the graph bonus (the symbol-level walk bled rank out of the catalogue
    # file into its many message symbols); with a dozen unrelated files added the catalogue
    # outranks the implementation both with the old and the file-level walk. What this test
    # can stably guard is the demotion itself, on the keyword score, free of graph effects.
    from cortex import bundle as bundle_mod

    task = "device card reconnect state wrong"
    demoted = _keyword_score(repo, task, "i18n/app_de.ts")
    monkeypatch.setattr(bundle_mod, "LOCALE_PATH_DEMOTION", 1.0)
    undemoted = _keyword_score(repo, task, "i18n/app_de.ts")

    assert undemoted > 0
    assert demoted == pytest.approx(undemoted * 0.25)
    assert "src/core.hpp" in _paths(repo, task)


def test_a_task_about_translation_still_finds_the_catalogue(repo):
    assert "i18n/app_de.ts" in _paths(repo, "translation of the device card reconnect state wrong strings")


def test_empty_files_are_never_packed(repo):
    bundle = generate_bundle(repo_path=repo, task="device state reconnect", budget=4000, output_format="json")
    assert all(item["token_count"] > 0 for item in bundle["items"])
    assert "src/device_state_reconnect.py" not in [item["path"] for item in bundle["items"]]
