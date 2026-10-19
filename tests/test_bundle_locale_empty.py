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


def test_translation_catalogue_does_not_outrank_the_implementation(repo):
    paths = _paths(repo, "device card reconnect state wrong")
    assert "src/core.hpp" in paths
    if "i18n/app_de.ts" in paths:
        assert paths.index("src/core.hpp") < paths.index("i18n/app_de.ts")


def test_a_task_about_translation_still_finds_the_catalogue(repo):
    assert "i18n/app_de.ts" in _paths(repo, "translation of the device card reconnect state wrong strings")


def test_empty_files_are_never_packed(repo):
    bundle = generate_bundle(repo_path=repo, task="device state reconnect", budget=4000, output_format="json")
    assert all(item["token_count"] > 0 for item in bundle["items"])
    assert "src/device_state_reconnect.py" not in [item["path"] for item in bundle["items"]]
