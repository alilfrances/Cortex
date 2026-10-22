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
    # The extractor writes `name:`/`module:` bases (0.12.2 resolves them: the original stays in
    # `metadata.unresolved_target`), so the profile is checked on what was extracted.
    inherits = [e for e in edges if e.relation == "inherits"]
    written = [e.metadata.get("unresolved_target", e.target) for e in inherits]
    assert inherits and {target.split(":", 1)[0] for target in written} == {"name", "module"}
    assert sum(target == f"name:{base}" for target in written) >= 10
    manager_calls = [e for e in edges if e.relation == "calls" and e.source.startswith(f"symbol:{names['manager_impl']}:")]
    assert len(manager_calls) > 100
    # mostly calls through a variable (a `CoreDevice *` or a cast local), the receivers that stayed `name:` in 0.12.1
    assert sum(e.metadata.get("receiver_kind") == "var" for e in manager_calls) > len(manager_calls) / 2


def test_an_anonymous_namespace_in_one_in_three_derived_files_creates_no_qt_class_nodes(tmp_path: Path) -> None:
    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    synth = load_synthetic_repo()
    fixed = {"coredevice", "channeldevice", "unrelated", "devicemanager", "sessionmanager"}
    derived = [p for p in sorted((repo / "src" / "field").glob("*.cpp")) if p.stem not in fixed]
    with_namespace = [p for p in derived if p.read_text().startswith(f'#include "{p.stem}.h"\nnamespace {{\nQString ')]
    assert synth.FIELD_ANON_NAMESPACE_ONE_IN == 3
    assert 0 < len(with_namespace) and abs(len(with_namespace) * 3 - len(derived)) <= 3
    assert "namespace" not in (repo / synth.FIELD_SHAPE["first_derived_impl"]).read_text()

    store = CortexStore(default_db_path(repo))
    try:
        nodes, _edges = store.fetch_graph(repo)
    finally:
        store.connection.close()
    assert [n.node_id for n in nodes if n.kind == "class" and n.label in {"QString", "QStringList", "QMap"}] == []
    # the helpers inside the anonymous namespaces are ordinary file-level functions
    helpers = [n for n in nodes if n.kind == "func" and n.label.startswith("describe_")]
    assert len(helpers) == len(with_namespace)


def test_about_a_quarter_of_the_cpp_classes_derive_only_from_an_external_base(tmp_path: Path) -> None:
    """0.14.0: the field repository's `name:` bases are mostly Qt classes and gtest fixtures (543 of 587)."""
    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    store = CortexStore(default_db_path(repo))
    try:
        nodes, edges = store.fetch_graph(repo)
    finally:
        store.connection.close()
    cpp_suffixes = {".h", ".hpp", ".cpp"}
    classes = [
        n for n in nodes
        if n.kind == "class" and n.granularity == "symbol" and Path(n.source_ref).suffix in cpp_suffixes
        and not n.signature.lstrip().startswith("namespace")
    ]
    bases: dict[str, list] = {}
    for edge in edges:
        if edge.relation == "inherits":
            bases.setdefault(edge.source, []).append(edge)
    external_only = [n for n in classes if bases.get(n.node_id) and all(e.metadata.get("external") for e in bases[n.node_id])]

    assert 0.22 <= len(external_only) / len(classes) <= 0.28, (len(external_only), len(classes))
    marked = [e for e in edges if e.relation == "inherits" and e.metadata.get("external")]
    assert {e.metadata["external"] for e in marked if e.source.rsplit(":", 1)[0].endswith((".h", ".cpp"))} == {"qt", "prefix", "gtest"}
    assert {e.target for e in marked if e.metadata["external"] == "qt"} >= {"name:QObject", "name:QThread"}
    assert {e.target for e in marked if e.metadata["external"] == "prefix"} == {"name:::testing::Test"}
    assert all(e.target.startswith(("name:", "module:")) for e in marked)  # classified, never rewritten
    # the QML views derive from `Item`, which no repository component is named after
    views = [e for e in marked if e.source.split(":")[1].endswith(".qml")]
    assert views and {e.metadata["external"] for e in views} == {"qml_builtin"}
    # in-repo bases are untouched
    assert not any(e.metadata.get("external") for e in edges if e.relation == "inherits" and e.target.startswith("symbol:"))


def test_the_step_mocks_resolve_to_the_mock_base_one_directory_above_them(tmp_path: Path) -> None:
    """0.14.0 (review 0.13.0 section D): the mock base is `tests/header-mocks/stepbase.h`, a production `StepBase`
    exists too, and the mocks in `tests/header-mocks/steps/` include it by angle brackets."""
    repo = build_field_shape_repo(tmp_path / "field", modules=12, commits=30)
    names = load_synthetic_repo().FIELD_SHAPE
    store = CortexStore(default_db_path(repo))
    try:
        nodes, edges = store.fetch_graph(repo)
    finally:
        store.connection.close()
    assert {n.source_ref for n in nodes if n.kind == "class" and n.label == "StepBase"} == {names["step_base"], names["step_base_mock"]}
    mock_base = f"symbol:{names['step_base_mock']}:StepBase"
    edges_by_source = {e.source: e for e in edges if e.relation == "inherits"}
    mocks = [edges_by_source[f"symbol:{path}:Step{path[-3].upper()}"] for path in names["step_mocks"]]

    assert len(mocks) == 3
    assert [e.target for e in mocks] == [mock_base] * 3  # 100%
    assert all(e.metadata["unresolved_target"] == "name:StepBase" and not e.metadata.get("ambiguous") for e in mocks)
