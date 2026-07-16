from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cortex.bundle import generate_bundle
from cortex.hotspots import compute_churn, estimate_complexity, top_hotspots
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.models import CommitRecord, SourceRecord
from cortex.report import generate_report
from cortex.store import CortexStore, default_db_path
from cortex.structural.regex_backend import extract_regex_edges
from evals.run_evals import _build_qt_app


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Hotspot Tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "hotspots@example.test"], cwd=repo, check=True)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_compute_churn_counts_each_file_once_per_commit() -> None:
    commits = [
        CommitRecord("a", "first", "test", 1, ["src/a.py", "src/a.py", "src/b.py"]),
        {"sha": "b", "files": ["src/a.py"]},
    ]

    assert compute_churn(commits) == {"src/a.py": 2, "src/b.py": 1}


def test_complexity_is_language_specific_and_skips_comments_strings_and_raw_strings() -> None:
    python = SourceRecord(
        "logic.py",
        "# if for while\ndef run(value):\n    text = \"if for\"\n    if value and value:\n        return True\n    return False\n",
        "code",
        0,
        0.0,
    )
    cpp = SourceRecord(
        "switch.cpp",
        'int run(int value) { /* if while */ const char *s = "case if"; '
        'auto raw = R"TAG(switch { case 9: })TAG"; switch (value) { '
        'case 1: return 1; case 2: return 2; default: return 0; } }\n',
        "code",
        0,
        0.0,
    )
    qml = SourceRecord(
        "View.qml",
        "Item {\n"
        "    property bool enabled: model.count > 0 && ready\n"
        "    width: parent.width ? 100 : 20\n"
        "    onClicked: if (enabled) doThing()\n"
        "    onAccepted: doOtherThing()\n"
        "}\n",
        "code",
        0,
        0.0,
    )
    trivial_cpp = SourceRecord("trivial.cpp", "int run() { return 0; }\n", "code", 0, 0.0)

    assert estimate_complexity(python) > 0
    assert estimate_complexity(cpp) > estimate_complexity(trivial_cpp)
    assert estimate_complexity(qml) > 0
    # The two case labels, switch, and operators are real C++ complexity; the
    # words inside comments, literals, and the raw string are not counted.
    clean_cpp = SourceRecord(
        "clean.cpp",
        "int run(int value) { switch (value) { case 1: return 1; case 2: return 2; default: return 0; } }\n",
        "code",
        0,
        0.0,
    )
    assert estimate_complexity(cpp) == estimate_complexity(clean_cpp)


def test_shared_masking_keeps_regex_cpp_body_span_past_raw_string_braces() -> None:
    content = (
        'void render() {\n'
        '    const char *raw = R"TAG(} if (ready) { case 1: })TAG";\n'
        '    if (ready) { return; }\n'
        '}\n'
    )

    nodes, _edges = extract_regex_edges("render.cpp", content, {"render.cpp"})
    render = next(node for node in nodes if node.label == "render")
    assert (render.span_start, render.span_end) == (1, 4)


def test_ingest_persists_and_incrementally_updates_hotspots(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "hot.py").write_text("def run(value):\n    if value:\n        return 1\n    return 0\n")
    (repo / "quiet.py").write_text("def quiet():\n    return 0\n")
    _commit(repo, "initial files")

    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    nodes, _ = store.fetch_graph(repo)
    before = {node.source_ref: node.metadata["hotspot"] for node in nodes if node.kind == "file"}
    quiet_rowid_before = store.connection.execute(
        "SELECT rowid FROM graph_nodes WHERE repo_path = ? AND node_id = ?",
        (str(repo.resolve()), "file:quiet.py"),
    ).fetchone()[0]
    assert before["hot.py"]["churn"] == 1
    assert before["hot.py"]["complexity"] > 0

    refresh_calls = []
    original_update = CortexStore.update_file_hotspots

    def track_global_refresh(self, *args, **kwargs):
        refresh_calls.append(True)
        return original_update(self, *args, **kwargs)

    monkeypatch.setattr(CortexStore, "update_file_hotspots", track_global_refresh)

    (repo / "hot.py").write_text(
        "def run(value):\n"
        "    if value:\n"
        "        for item in value:\n"
        "            if item:\n"
        "                return item\n"
        "    return 0\n"
    )
    result = ingest_repository(repo, incremental=True)
    assert result["updated_files"] == 1
    assert refresh_calls == [], "source-only refreshes must not scan all file-node metadata"

    nodes, _ = store.fetch_graph(repo)
    after = {node.source_ref: node.metadata["hotspot"] for node in nodes if node.kind == "file"}
    quiet_rowid_after = store.connection.execute(
        "SELECT rowid FROM graph_nodes WHERE repo_path = ? AND node_id = ?",
        (str(repo.resolve()), "file:quiet.py"),
    ).fetchone()[0]
    assert after["hot.py"]["complexity"] > before["hot.py"]["complexity"]
    assert after["quiet.py"] == before["quiet.py"]
    assert quiet_rowid_after == quiet_rowid_before

    _commit(repo, "make hot path more complex")
    ingest_repository(repo, incremental=True)
    assert len(refresh_calls) == 1, "a new commit must refresh retained-file churn metadata"
    nodes, _ = store.fetch_graph(repo)
    final = {node.source_ref: node.metadata["hotspot"] for node in nodes if node.kind == "file"}
    assert final["hot.py"]["churn"] == 2
    assert final["quiet.py"]["churn"] == 1


def test_hotspot_boost_is_opt_in_and_churned_file_ranks_first(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "hot.py").write_text(
        "def process_feature(value):\n"
        "    if value:\n"
        "        for item in value:\n"
        "            if item:\n"
        "                return item\n"
        "    return None\n"
    )
    (repo / "quiet.py").write_text("def process_feature(value):\n    return value\n")
    _commit(repo, "initial implementation")
    for index in range(2):
        (repo / "hot.py").write_text((repo / "hot.py").read_text() + f"# revision {index}\n")
        _commit(repo, f"churn hot file {index}")
    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    nodes, _ = store.fetch_graph(repo)
    ranked_hotspots = top_hotspots(nodes)
    assert ranked_hotspots
    assert ranked_hotspots[0]["path"] == "hot.py"
    assert all(ranked_hotspots[0]["score"] >= item["score"] for item in ranked_hotspots)

    default = generate_bundle(repo, "process feature", 10000, output_format="json")
    explicit_off = generate_bundle(repo, "process feature", 10000, output_format="json", hotspot_boost=False)
    boosted = generate_bundle(repo, "process feature", 10000, output_format="json", hotspot_boost=True)

    assert [(item["path"], item["score"]) for item in default["items"]] == [
        (item["path"], item["score"]) for item in explicit_off["items"]
    ]
    assert [item["path"] for item in boosted["items"]][:2] == ["hot.py", "quiet.py"]


def test_hotspots_surface_in_report_overview_and_impact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "app.py").write_text("def app(value):\n    if value:\n        return 1\n    return 0\n")
    (repo / "neighbor.py").write_text("def neighbor():\n    return 0\n")
    _commit(repo, "app and neighbor")
    ingest_repository(repo)

    report = generate_report(repo)
    assert "## Hotspots" in report
    assert "app.py" in report

    overview = _payload(call_tool("cortex_overview", {"repo_path": str(repo), "response_format": "detailed"}))
    assert overview["top_hotspots"][0]["path"] == "app.py"

    impact = _payload(call_tool("cortex_impact", {"repo_path": str(repo), "path": "app.py"}))
    neighbor = next(item for item in impact["items"] if item["path"] == "neighbor.py")
    assert set(neighbor["hotspot"]) == {"churn", "complexity", "score"}


def test_qt_fixture_hotspots_count_switch_handlers_and_bindings(tmp_path: Path) -> None:
    repo = _build_qt_app(tmp_path)
    ingest_repository(repo, commit_limit=20)
    store = CortexStore(default_db_path(repo))
    nodes, _ = store.fetch_graph(repo)
    by_path = {node.source_ref: node.metadata["hotspot"] for node in nodes if node.kind == "file"}

    manager = by_path["src/DeviceManager.cpp"]
    main = by_path["qml/Main.qml"]
    assert manager["churn"] == 2
    assert main["churn"] == 2
    assert manager["complexity"] > by_path["src/DeviceModel.cpp"]["complexity"]
    assert main["complexity"] > by_path["qml/DeviceDelegate.qml"]["complexity"]
    assert manager["score"] > by_path["src/DeviceModel.cpp"]["score"]
    assert main["score"] > by_path["qml/DeviceDelegate.qml"]["score"]
