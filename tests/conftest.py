from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_cortex_data_dir(tmp_path_factory, monkeypatch):
    """Keep every test's central store inside pytest's tmp tree, never ~/.cortex."""
    data_dir = tmp_path_factory.mktemp("cortex-data")
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data_dir))


@pytest.fixture(scope="module")
def _field_shape_built(tmp_path_factory):
    """One indexed `--field-shape` repository (12 modules, 30 commits) per test module."""
    from tests.field_shape import build_field_shape_repo

    root = tmp_path_factory.mktemp("field-shape")
    data_dir = root / "data"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("CORTEX_DATA_DIR", str(data_dir))
        patch.setenv("CORTEX_SEMANTIC", "0")
        repo = build_field_shape_repo(root / "repo", modules=12, commits=30)
    return repo, data_dir


@pytest.fixture
def field_shape_repo(_field_shape_built, monkeypatch):
    """The module's shared `--field-shape` repository; its index is read-only for the test."""
    repo, data_dir = _field_shape_built
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    return repo
