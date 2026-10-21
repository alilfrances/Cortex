"""The ``--field-shape`` profile of scripts/perf/synthetic_repo.py (0.12.2): deterministic, additive, field-shaped."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from cortex.store import CortexStore, default_db_path
from tests.field_shape import build_field_shape_repo, load_synthetic_repo


def _tree_hash(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=True).stdout.strip()


def test_other_profiles_are_unchanged_by_the_flag_and_the_profile_is_deterministic(tmp_path: Path) -> None:
    synth = load_synthetic_repo()
    synth.generate(tmp_path / "plain", 8, 4)
    synth.generate(tmp_path / "plain2", 8, 4, field_shape=False)
    synth.generate(tmp_path / "field_a", 8, 4, field_shape=True)
    synth.generate(tmp_path / "field_b", 8, 4, field_shape=True)
    assert _tree_hash(tmp_path / "plain") == _tree_hash(tmp_path / "plain2")
    assert _tree_hash(tmp_path / "field_a") == _tree_hash(tmp_path / "field_b") != _tree_hash(tmp_path / "plain")
    assert not (tmp_path / "plain" / "src" / "field").exists()


def test_the_profile_has_the_field_report_shape(tmp_path: Path) -> None:
    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    names = load_synthetic_repo().FIELD_SHAPE
    for key in ("base_header", "base_impl", "mid_impl", "nested_impl", "js_helper", "manager_impl", "second_manager_impl", "first_derived_impl"):
        assert (repo / names[key]).is_file(), key
    assert all((repo / path).is_file() for path in names["mock_headers"])

    files = [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts]
    concatenated = [p for p in files if re.fullmatch(r"[a-z]{10,}\d*", p.stem)]
    assert len(concatenated) / len(files) >= 0.5
    assert "WidgetWaitReadyStep" in (repo / names["first_derived_impl"]).read_text()

    store = CortexStore(default_db_path(repo))
    try:
        nodes, edges = store.fetch_graph(repo)
    finally:
        store.connection.close()
    base = names["base_class"]
    classes = [n for n in nodes if n.label == base and n.kind == "class"]
    assert len(classes) == 6  # real header, 3 mocks, a nested class and the JS helper
    inherits = [e for e in edges if e.relation == "inherits"]
    assert inherits and not any(e.target.startswith("symbol:") for e in inherits)
    assert {e.target.split(":", 1)[0] for e in inherits} == {"name", "module"}
    assert sum(e.target == f"name:{base}" for e in inherits) >= 10
    manager_calls = [e for e in edges if e.relation == "calls" and e.source.startswith(f"symbol:{names['manager_impl']}:")]
    assert len(manager_calls) > 100
    assert sum(e.target.startswith("name:") for e in manager_calls) > len(manager_calls) / 2
