"""P0-4 cross-file Qt signal/handler symbol resolution acceptance tests.

Confirmed on the qt_app eval fixture during Wave 0: `emit deviceConnected(42)`
in src/DeviceManager.cpp used to resolve only against symbols in the *same*
file, so it produced an `emits` edge to a placeholder `module:deviceConnected`
node even though the signal is declared in include/DeviceManager.hpp; QML
`onFoo:` handlers never became symbol nodes at all. These tests exercise the
cross-file resolution pass (graph.py::_resolve_qt_edges / QtSymbolIndex) that
fixes both, through both the regex backend (always available) and, when
installed, the tree-sitter backend -- plus the P0-3 incremental-ingest
interaction: a signal declared in a header that is *not* part of an
incremental re-ingest batch must still resolve correctly for a .cpp that is.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.store import CortexStore, default_db_path
from evals.run_evals import _build_qt_app


def _ingest_qt_app(tmp_path: Path) -> Path:
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    return repo


def _relations(repo: Path, **kwargs) -> list[dict]:
    result = call_tool("cortex_relations", {"repo_path": str(repo), "limit": 50, **kwargs})
    payload = json.loads(result["content"][0]["text"])
    assert not result["isError"], payload
    return payload["items"]


def _search_symbols(repo: Path, query: str) -> list[dict]:
    result = call_tool("cortex_search_symbols", {"repo_path": str(repo), "query": query, "limit": 20})
    payload = json.loads(result["content"][0]["text"])
    assert not result["isError"], payload
    return payload["items"]


# ---------------------------------------------------------------------------
# Regex backend (always available -- the default in this environment, since
# the tree-sitter extras aren't installed here; forced explicitly too below).
# ---------------------------------------------------------------------------


def test_emit_resolves_cross_file_to_header_signal_regex_backend(tmp_path, monkeypatch):
    import cortex.structural.treesitter_backend as treesitter_backend

    def fail_tree_sitter(*args, **kwargs):
        raise RuntimeError("grammar unavailable")

    monkeypatch.setattr(treesitter_backend, "extract_treesitter_edges", fail_tree_sitter)

    repo = _ingest_qt_app(tmp_path)
    items = _relations(repo, relation="emits", symbol="deviceConnected")
    assert items, "expected an emits edge for deviceConnected"
    # DeviceManager::scan() is the emit site; its target must resolve to the
    # real header symbol, not the module:deviceConnected placeholder.
    scan_emits = [item for item in items if "DeviceManager.cpp" in item["source"]]
    assert scan_emits, items
    assert any("include/DeviceManager.hpp" in item["target"] for item in scan_emits), scan_emits
    assert not any(item["target"] == "module:deviceConnected" for item in scan_emits), scan_emits


def test_connects_resolves_both_endpoints_to_real_signal_slot_symbols(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    items = _relations(repo, relation="connects")
    assert items
    matches = [
        item
        for item in items
        if "deviceConnected" in item["source"] and "onDeviceConnected" in item["target"]
    ]
    assert matches, items
    # A resolved endpoint renders as "<label> @ <path>:<line>" (see
    # mcp/tools.py::_call_relations.endpoint); an unresolved module:
    # placeholder renders as the bare name with no "@ path" suffix.
    for item in matches:
        assert "@ include/DeviceManager.hpp" in item["source"], item
        assert "@ include/DeviceModel.hpp" in item["target"], item


def test_qml_handlers_are_searchable_symbols(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    for query in ("onClicked", "onDeviceConnected"):
        hits = _search_symbols(repo, query)
        assert any(item["label"] == query for item in hits), (query, hits)


def test_qml_handler_resolves_to_instantiated_component_signal(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    items = _relations(repo, relation="handles")
    assert items
    device_connected_handles = [item for item in items if "Main.qml" in item["source"]]
    assert device_connected_handles, items
    resolved = [item for item in device_connected_handles if "deviceConnected @ qml/DeviceDelegate.qml" in item["target"]]
    assert resolved, device_connected_handles


def test_qml_instantiates_real_cpp_type_symbol(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    store = CortexStore(default_db_path(repo))
    edges = store.query_edges(repo, relation="instantiates", endpoint_substr="DeviceManager", limit=20)
    main_edges = [edge for edge in edges if edge.source == "file:qml/Main.qml"]
    assert main_edges
    assert any(edge.target == "symbol:include/DeviceManager.hpp:DeviceManager" for edge in main_edges), main_edges
    assert not any(edge.target == "module:DeviceManager" for edge in main_edges), main_edges


def test_qml_handler_on_external_type_stays_placeholder(tmp_path):
    # DeviceDelegate.qml's MouseArea { onClicked: clicked() } handles Qt
    # Quick's own MouseArea.clicked signal, not the enclosing DeviceDelegate's
    # own same-named `clicked` signal -- MouseArea isn't a locally known
    # component, so this must stay an unresolved module: placeholder rather
    # than guessing.
    repo = _ingest_qt_app(tmp_path)
    items = _relations(repo, relation="handles")
    delegate_handles = [item for item in items if "DeviceDelegate.qml" in item["source"]]
    assert delegate_handles, items
    # Unresolved endpoints render with the "module:" prefix stripped
    # (see mcp/tools.py::_unresolved_endpoint) -- the edge itself keeps the
    # placeholder id, so the rendered target is the bare signal name with no
    # "@ path:line" resolution suffix.
    assert any(item["target"] == "clicked" for item in delegate_handles), delegate_handles


# ---------------------------------------------------------------------------
# Tree-sitter backend, when the [languages]/[qml] extras are installed.
# Skipped (not failed) when they aren't -- see module docstring.
# ---------------------------------------------------------------------------


def test_emit_and_handles_resolve_cross_file_tree_sitter_backend(tmp_path):
    pytest.importorskip("tree_sitter_cpp")
    pytest.importorskip("tree_sitter_language_pack")

    repo = _ingest_qt_app(tmp_path)
    emits = _relations(repo, relation="emits", symbol="deviceConnected")
    scan_emits = [item for item in emits if "DeviceManager.cpp" in item["source"]]
    assert scan_emits, emits
    assert any("include/DeviceManager.hpp" in item["target"] for item in scan_emits), scan_emits

    handles = _relations(repo, relation="handles")
    main_handles = [item for item in handles if "Main.qml" in item["source"]]
    assert any("deviceConnected @ qml/DeviceDelegate.qml" in item["target"] for item in main_handles), main_handles
    instantiates = _relations(repo, relation="instantiates", symbol="DeviceManager")
    assert any("DeviceManager @ include/DeviceManager.hpp" in item["target"] for item in instantiates), instantiates


# ---------------------------------------------------------------------------
# P0-3 incremental-ingest interaction: the signal declaration lives in a
# header that is *not* part of the incremental batch (only the .cpp changed).
# ---------------------------------------------------------------------------


def test_incremental_reingest_of_qml_alone_still_resolves_cpp_instantiation(tmp_path):
    repo = _ingest_qt_app(tmp_path)
    qml_path = repo / "qml" / "Main.qml"
    content = qml_path.read_text(encoding="utf-8")
    assert "// context tick" not in content
    qml_path.write_text(content.replace("ApplicationWindow {", "ApplicationWindow {\n    // context tick"), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "touch qml scene"], cwd=repo, check=True, capture_output=True)

    result = ingest_repository(repo, commit_limit=20, incremental=True)
    assert result["updated_files"] == 1
    store = CortexStore(default_db_path(repo))
    edges = store.query_edges(repo, relation="instantiates", endpoint_substr="DeviceManager", limit=20)
    assert any(edge.target == "symbol:include/DeviceManager.hpp:DeviceManager" for edge in edges), edges


def test_incremental_reingest_of_cpp_alone_still_resolves_emit_to_header_signal(tmp_path):
    repo = _ingest_qt_app(tmp_path)

    # Sanity check: the emit is already resolved after the full ingest.
    before = _relations(repo, relation="emits", symbol="deviceConnected")
    scan_before = [item for item in before if "DeviceManager.cpp" in item["source"]]
    assert any("include/DeviceManager.hpp" in item["target"] for item in scan_before), scan_before

    # Modify ONLY src/DeviceManager.cpp -- include/DeviceManager.hpp (where
    # deviceConnected is declared) is untouched, so an incremental re-ingest
    # never reparses it; the store's already-known signal index is the only
    # way the emit resolves this time (graph.py::QtSymbolIndex / P0-4).
    cpp_path = repo / "src" / "DeviceManager.cpp"
    content = cpp_path.read_text(encoding="utf-8")
    assert "// scan tick" not in content
    cpp_path.write_text(content.replace("void DeviceManager::scan() {", "void DeviceManager::scan() {\n    // scan tick"), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "touch scan()"], cwd=repo, check=True, capture_output=True)

    result = ingest_repository(repo, commit_limit=20, incremental=True)
    assert result["updated_files"] == 1
    assert result["new_files"] == 0

    store = CortexStore(default_db_path(repo))
    edges = store.query_edges(repo, relation="emits", endpoint_substr="deviceConnected", limit=50)
    scan_edges = [e for e in edges if e.metadata.get("source_file") == "src/DeviceManager.cpp"]
    assert scan_edges, edges
    assert any(e.target == "symbol:include/DeviceManager.hpp:deviceConnected" for e in scan_edges), scan_edges
    assert not any(e.target == "module:deviceConnected" for e in scan_edges), scan_edges
