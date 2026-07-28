from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from cortex.risk import analyze_risk
from cortex.models import GraphEdge
from cortex.store import CortexStore, default_db_path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_swift_language_tiering_and_test_pair_detection_support_both_parser_paths(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "swift_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "Sources").mkdir()
    (repo / "LingleTests").mkdir()
    source = repo / "Sources" / "Foo.swift"
    test = repo / "LingleTests" / "FooTests.swift"
    source.write_text(
        "func orphan() { }\n"
        "func documented() { }\n"
        "func used() { }\n"
        "used()\n",
        encoding="utf-8",
    )
    test.write_text("func testFoo() { }\n", encoding="utf-8")
    (repo / "README.md").write_text("The documented helper is described here.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    ingest_repository(repo, commit_limit=20)
    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)
    findings = {
        item["symbol"]: item
        for item in analyze_dead_code(
            repo,
            store=store,
            nodes=nodes,
            edges=[
                *edges,
                GraphEdge(
                    edge_id="fixture:used-call",
                    source="file:Sources/Foo.swift",
                    target="symbol:Sources/Foo.swift:used",
                    relation="calls",
                    layer="STRUCTURAL",
                ),
            ],
        )["findings"]
    }
    swift_nodes = [node for node in nodes if node.source_ref == "Sources/Foo.swift" and node.label in {"orphan", "documented"}]

    assert {"orphan", "documented"} <= findings.keys()
    assert "used" not in findings
    assert "testFoo" not in findings
    if swift_nodes and all(node.metadata.get("parser_backend") == "treesitter" for node in swift_nodes):
        assert findings["orphan"]["confidence"] == "high"
        assert findings["orphan"]["reason"] == "no incoming edges; no grep refs"
        assert findings["documented"]["confidence"] == "medium"
        assert findings["documented"]["reason"] == "no incoming edges; grep refs only in comments/docs"
    else:
        assert findings["orphan"]["confidence"] == "low"
        assert findings["orphan"]["reason"] == "no incoming edges; regex/dynamic-language caveat"
        assert findings["documented"]["confidence"] == "low"
        assert findings["documented"]["reason"] == "no incoming edges; regex/dynamic-language caveat"

    source.write_text(source.read_text(encoding="utf-8") + "// source-only change\n", encoding="utf-8")
    _git(repo, "add", "Sources/Foo.swift")
    _git(repo, "commit", "-qm", "source only")
    missing = analyze_risk(repo)
    assert any(item["source"] == "Sources/Foo.swift" and item["test"] == "LingleTests/FooTests.swift" for item in missing["missing_tests"])

    source.write_text(source.read_text(encoding="utf-8") + "// paired change\n", encoding="utf-8")
    test.write_text(test.read_text(encoding="utf-8") + "// paired change\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "source and test")
    paired = analyze_risk(repo)
    assert not any(item["source"] == "Sources/Foo.swift" for item in paired["missing_tests"])
