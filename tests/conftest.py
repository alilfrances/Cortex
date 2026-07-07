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
