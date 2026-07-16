"""P0-2 Qt/C++/QML search parity (IMPROVEMENT_PLAN.md ground rules + P0-2
acceptance criteria): on the shared qt_app eval fixture, searching a signal
name, a SIGNAL()/SLOT() macro string, an onFoo QML handler name, and a
Class::method qualified name must each surface the right .cpp/.hpp/.qml
file; body-text search must find a string literal inside a .qml file.
"""

from __future__ import annotations

from pathlib import Path

from cortex.bundle import generate_bundle
from cortex.ingest import ingest_repository
from cortex.store import CortexStore, default_db_path
from evals.run_evals import _build_qt_app


def _ingest_qt_app(tmp_path: Path) -> Path:
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    return repo


def _bundle_paths(repo: Path, task: str, budget: int = 2000) -> set[str]:
    result = generate_bundle(repo, task=task, budget=budget, db_path=default_db_path(repo), output_format="json")
    return {item["path"] for item in result["items"]}


def test_qt_parity_signal_name_query_surfaces_header_and_implementation(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    paths = _bundle_paths(repo, "deviceConnected signal declaration and emission")
    assert "include/DeviceManager.hpp" in paths
    assert "src/DeviceManager.cpp" in paths


def test_qt_parity_qualified_class_method_query_surfaces_implementation(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    paths = _bundle_paths(repo, "DeviceManager::scan")
    assert "src/DeviceManager.cpp" in paths


def test_qt_parity_onfoo_qml_handler_query_surfaces_qml_file(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    paths = _bundle_paths(repo, "onDeviceConnected QML handler")
    assert "qml/Main.qml" in paths


def test_qt_parity_signal_slot_macro_string_findable_via_fulltext(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    # The fixture only wires signals via the new-style pointer-to-member
    # connect(); Qt parity also requires the legacy SIGNAL()/SLOT() macro
    # string form -- a plain string literal Cortex's structural backends
    # don't parse specially -- to be findable via full-text body search.
    legacy = repo / "src" / "LegacyConnect.cpp"
    legacy.write_text(
        '#include "DeviceManager.hpp"\n#include "DeviceModel.hpp"\n\n'
        "void wireLegacy(DeviceManager *mgr, DeviceModel *model) {\n"
        "    connect(mgr, SIGNAL(deviceConnected(int)), model, SLOT(onDeviceConnected(int)));\n"
        "}\n",
        encoding="utf-8",
    )
    ingest_repository(repo, commit_limit=20)

    store = CortexStore(default_db_path(repo))
    assert store.fts_enabled, "FTS5 must be available in the test environment for this assertion"
    hits = store.search_fulltext(repo, "SIGNAL deviceConnected SLOT onDeviceConnected", limit=10)
    hit_paths = {path for path, _score, _snippet in hits}
    assert "src/LegacyConnect.cpp" in hit_paths


def test_qt_parity_body_text_search_finds_string_literal_inside_qml(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    store = CortexStore(default_db_path(repo))
    assert store.fts_enabled, "FTS5 must be available in the test environment for this assertion"

    # qml/Main.qml contains `onClicked: console.log("delegate clicked")` --
    # a string literal only findable via body text, not any symbol name.
    hits = store.search_fulltext(repo, "delegate clicked", limit=10)
    hit_paths = {path for path, _score, _snippet in hits}
    assert "qml/Main.qml" in hit_paths


def test_qt_parity_cortex_search_text_mcp_tool_finds_qml_string_literal(tmp_path):
    from cortex.mcp.tools import call_tool
    import json

    repo = _ingest_qt_app(tmp_path)
    result = call_tool("cortex_search_text", {"repo_path": str(repo), "query": "delegate clicked"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["fts_available"] is True
    paths = [item["path"] for item in payload["items"]]
    assert "qml/Main.qml" in paths
