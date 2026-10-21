"""Build the ``--field-shape`` synthetic repository (scripts/perf/synthetic_repo.py) for tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from cortex.ingest import ingest_repository

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "perf" / "synthetic_repo.py"


def load_synthetic_repo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cortex_synthetic_repo", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_field_shape_repo(root: Path, *, modules: int = 12, commits: int = 30, ingest: bool = True) -> Path:
    """Generate (and by default index) a small `--field-shape` repo at ``root`` (must not exist).

    The caller's ``CORTEX_DATA_DIR`` (the autouse conftest fixture) receives the index.
    """
    load_synthetic_repo().generate(root, modules, commits, field_shape=True)
    if ingest:
        ingest_repository(root, commit_limit=commits + 5)
    return root
