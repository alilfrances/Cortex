from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cortex import bundle as bundle_mod
from cortex.fusion import rrf_fuse
from cortex.ingest import ingest_repository
from cortex.mcp.tools import _cache_key, call_tool
from cortex.models import GraphNode, SourceRecord
from cortex.semantic import MODEL_ID
from cortex.store import CortexStore, default_db_path


@pytest.fixture
def fake_local_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install an isolated deterministic fake; never touch a real model cache."""
    numpy = pytest.importorskip("numpy")
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "semantic-data"))
    from cortex import semantic

    class FakeModel:
        calls: list[tuple[str, dict]] = []

        @classmethod
        def from_pretrained(cls, path, **kwargs):
            cls.calls.append((str(path), dict(kwargs)))
            return cls()

        def save_pretrained(self, path):
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            (path / "config.json").write_text("{}", encoding="utf-8")
            (path / "tokenizer.json").write_text("{}", encoding="utf-8")
            (path / "model.safetensors").write_bytes(b"test-only fake model artifact")

        def encode(self, texts):
            rows = []
            for text in texts:
                lowered = str(text).lower()
                if any(term in lowered for term in ("click", "qml", "delegate")):
                    rows.append([0.0, 1.0, 0.0])
                elif any(term in lowered for term in ("gamma", "auth", "login", "credential")):
                    rows.append([1.0, 0.0, 0.0])
                else:
                    rows.append([0.5, 0.5, 0.0])
            return numpy.asarray(rows, dtype=numpy.float32)

    monkeypatch.setattr(semantic, "_StaticModel", FakeModel)
    monkeypatch.setattr(semantic, "_numpy", numpy)
    monkeypatch.setenv("CORTEX_SEMANTIC", "1")
    semantic.clear_model_cache()
    model_dir = semantic.model_path()
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.safetensors").write_bytes(b"test-only fake model artifact")
    semantic._write_manifest(model_dir, semantic._artifact_version(model_dir))
    semantic.clear_model_cache()
    return FakeModel, semantic


def _git_init(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Cortex Test"], cwd=repo, check=True, capture_output=True)


def _commit(repo: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)


def test_chunk_text_contains_signature_comment_docstring_and_bounded_excerpt(fake_local_model, tmp_path):
    from cortex import semantic

    source = SourceRecord(
        path="app.py",
        content=(
            "# leading explanation\n"
            "def login(user):\n"
            "    \"\"\"Issue a session credential.\"\"\"\n"
            "    first()\n"
            "    second()\n"
            "    third()\n"
            "    fourth()\n"
            "    fifth()\n"
            "    sixth()\n"
            "    seventh()\n"
            "    eighth()\n"
            "    ninth()\n"
            "    tenth()\n"
            "    return user\n"
        ),
        kind="code",
        size_bytes=0,
        modified_at=0,
        content_hash="source-hash",
    )
    node = GraphNode(
        node_id="symbol:app.py:login",
        kind="func",
        label="login",
        source_ref="app.py",
        granularity="symbol",
        signature="def login(user):",
        span_start=2,
        span_end=15,
    )
    chunk = semantic.symbol_chunk_text(source, node, excerpt_lines=3)
    assert "def login(user):" in chunk
    assert "leading explanation" in chunk
    assert "Issue a session credential" in chunk
    assert "first()" in chunk
    assert "third()" not in chunk and "tenth()" not in chunk


def test_cli_semantic_status_is_local_and_no_network(monkeypatch, tmp_path, capsys):
    from cortex import cli, semantic

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.delenv("CORTEX_SEMANTIC", raising=False)
    semantic.clear_model_cache()

    class BlockedSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError("semantic status attempted a network socket")

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    monkeypatch.setattr(sys, "argv", ["cortex", "semantic", "status"])
    cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
    assert payload["active"] is False
    assert payload["model_ready"] is False
    assert any(term in payload["reason"] for term in ("setup", "installed"))


def test_cli_semantic_setup_dispatches_force_without_download(monkeypatch, capsys):
    from cortex import cli, semantic

    calls: list[bool] = []

    def fake_setup(*, force: bool = False):
        calls.append(force)
        return {"model_ready": True, "reason": "test fake; no download"}

    monkeypatch.setattr(semantic, "setup_model", fake_setup)
    monkeypatch.setattr(sys, "argv", ["cortex", "semantic", "setup", "--force"])
    cli.main()
    assert calls == [True]
    assert json.loads(capsys.readouterr().out)["reason"] == "test fake; no download"


def test_schema_migration_creates_owned_embedding_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE chunk_embeddings (repo_path TEXT, node_id TEXT, vector BLOB)"
    )
    connection.execute("INSERT INTO chunk_embeddings VALUES (?, ?, ?)", ("repo", "node", b"legacy"))
    connection.commit()
    connection.close()

    store = CortexStore(db_path)
    columns = {row["name"] for row in store.connection.execute("PRAGMA table_info(chunk_embeddings)")}
    assert {"source_path", "source_hash", "model_id", "model_version", "dimension", "created_at"} <= columns
    assert store.connection.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 1


def test_full_delta_delete_embedding_ownership(fake_local_model, tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _commit(repo, "initial")
    db_path = tmp_path / "index.db"

    ingest_repository(repo, db_path=db_path)
    store = CortexStore(db_path)
    version = store.connection.execute(
        "SELECT model_version FROM chunk_embeddings LIMIT 1"
    ).fetchone()[0]
    assert store.count_chunk_embeddings(repo, MODEL_ID, version) == 2

    (repo / "a.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    ingest_repository(repo, db_path=db_path, incremental=True)
    rows = store.fetch_chunk_embeddings(repo, MODEL_ID, version)
    assert {row["source_path"] for row in rows} == {"a.py", "b.py"}
    assert {row["node_id"] for row in rows} == {"symbol:a.py:gamma", "symbol:b.py:beta"}

    (repo / "b.py").unlink()
    ingest_repository(repo, db_path=db_path, incremental=True)
    rows = store.fetch_chunk_embeddings(repo, MODEL_ID, version)
    assert [row["source_path"] for row in rows] == ["a.py"]

    ingest_repository(repo, db_path=db_path)
    assert store.count_chunk_embeddings(repo, MODEL_ID, version) == 1


def test_setup_is_only_remote_identifier_boundary(fake_local_model, monkeypatch):
    FakeModel, semantic = fake_local_model
    # Remove the fixture's local cache so explicit setup is forced to use the
    # provider-qualified id. The test model is a fake artifact, not a claimed
    # production download.
    shutil.rmtree(semantic.model_path())
    FakeModel.calls.clear()
    result = semantic.setup_model()
    assert result["model_ready"] is True
    assert FakeModel.calls[0][0] == MODEL_ID

    FakeModel.calls.clear()
    assert semantic.semantic_runtime_ready() is True
    assert FakeModel.calls
    runtime_path, kwargs = FakeModel.calls[-1]
    assert runtime_path == str(semantic.model_path())
    assert runtime_path != MODEL_ID
    assert kwargs.get("force_download") is False


def test_absent_or_inactive_semantic_path_is_byte_identical(fake_local_model, monkeypatch, tmp_path):
    from cortex import semantic

    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 'alpha'\n", encoding="utf-8")
    _commit(repo, "initial")
    db_path = tmp_path / "index.db"
    ingest_repository(repo, db_path=db_path)

    monkeypatch.setattr(bundle_mod.time, "time", lambda: 123)
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    semantic.clear_model_cache()
    inactive = bundle_mod.generate_bundle(repo, "alpha", 1000, db_path=db_path, output_format="json")

    # Compare against a genuinely absent optional dependency under the normal
    # enabled/default environment, not another inactive call with a cached
    # fake model.
    monkeypatch.delenv("CORTEX_SEMANTIC", raising=False)
    monkeypatch.setattr(semantic, "_StaticModel", None)
    monkeypatch.setattr(semantic, "_numpy", None)
    semantic.clear_model_cache()
    assert semantic.semantic_enabled() is True
    absent = bundle_mod.generate_bundle(repo, "alpha", 1000, db_path=db_path, output_format="json")
    assert absent == inactive


def test_socket_blocked_ingest_query_overview_status_use_only_local_model(fake_local_model, monkeypatch, tmp_path):
    FakeModel, semantic = fake_local_model
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _commit(repo, "initial")
    db_path = default_db_path(repo)
    ingest_repository(repo, db_path=db_path)
    FakeModel.calls.clear()
    semantic.clear_model_cache()

    class BlockedSocket:
        def __init__(self, *args, **kwargs):
            raise AssertionError("runtime semantic ingest/query attempted a socket")

    monkeypatch.setattr(socket, "socket", BlockedSocket)
    # Force a changed-symbol re-embed after clearing the loaded model cache.
    (repo / "a.py").write_text("def gamma():\n    return 3\n", encoding="utf-8")
    ingest_repository(repo, db_path=db_path, incremental=True)
    semantic.clear_model_cache()
    result = bundle_mod.generate_bundle(repo, "gamma", 1000, db_path=db_path, output_format="json")
    assert result["items"]

    semantic.clear_model_cache()
    overview_result = call_tool(
        "cortex_overview",
        {"repo_path": str(repo), "response_format": "detailed"},
    )
    overview = json.loads(overview_result["content"][0]["text"])
    assert overview["semantic"]["model_ready"] is True
    status = semantic.semantic_status(CortexStore(db_path), repo)
    assert status["active"] is True
    assert status["indexed_chunks"] >= 1

    assert FakeModel.calls
    for loaded_path, kwargs in FakeModel.calls:
        assert loaded_path == str(semantic.model_path())
        assert loaded_path != MODEL_ID
        assert kwargs.get("force_download") is False


def test_cosine_and_rrf_ranking_are_deterministic(fake_local_model, tmp_path):
    FakeModel, semantic = fake_local_model
    repo = tmp_path / "repo"
    _git_init(repo)
    db_path = tmp_path / "index.db"
    store = CortexStore(db_path)
    store.reset_repo(repo)
    # These are explicit test vectors for the fake model, not production model
    # output. The two nodes share a path to exercise deterministic de-dup.
    version = semantic._model_details().version
    store.save_chunk_embeddings(
        repo,
        [
            {"node_id": "symbol:z.py:z", "source_path": "z.py", "source_hash": "z", "model_id": MODEL_ID, "model_version": version, "vector": semantic._vector_blob([1, 0, 0]), "dimension": 3},
            {"node_id": "symbol:a.py:a", "source_path": "a.py", "source_hash": "a", "model_id": MODEL_ID, "model_version": version, "vector": semantic._vector_blob([0, 1, 0]), "dimension": 3},
            {"node_id": "symbol:a.py:aa", "source_path": "a.py", "source_hash": "aa", "model_id": MODEL_ID, "model_version": version, "vector": semantic._vector_blob([0, 1, 0]), "dimension": 3},
        ],
    )
    first = semantic.ranked_paths(store, repo, "click")
    second = semantic.ranked_paths(store, repo, "click")
    assert first == second == ["a.py", "z.py"]
    assert rrf_fuse([["a.py", "z.py"], ["z.py", "a.py"]]) == rrf_fuse(
        [["z.py", "a.py"], ["a.py", "z.py"]]
    )


def test_status_distinguishes_enabled_ready_and_indexed(fake_local_model, monkeypatch, tmp_path):
    _FakeModel, semantic = fake_local_model
    repo = tmp_path / "repo"
    _git_init(repo)
    store = CortexStore(tmp_path / "status.db")
    store.reset_repo(repo)

    no_chunks = semantic.semantic_status(store, repo)
    assert no_chunks["enabled"] is True
    assert no_chunks["installed"] is True
    assert no_chunks["model_ready"] is True
    assert no_chunks["active"] is False
    assert no_chunks["indexed_chunks"] == 0

    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    disabled = semantic.semantic_status(store, repo)
    assert disabled["enabled"] is False
    assert disabled["active"] is False

    monkeypatch.delenv("CORTEX_SEMANTIC", raising=False)
    monkeypatch.setattr(semantic, "_numpy", None)
    semantic.clear_model_cache()
    missing_numpy = semantic.semantic_status(store, repo)
    assert missing_numpy["installed"] is False
    assert missing_numpy["model_ready"] is False
    assert missing_numpy["active"] is False


def test_detailed_overview_reports_semantic_and_upgrades_old_cache(fake_local_model, tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _commit(repo, "initial")
    db_path = default_db_path(repo)
    ingest_repository(repo, db_path=db_path)
    store = CortexStore(db_path)
    fingerprint = store.get_repo_fingerprint(repo)
    arguments = {"repo_path": str(repo), "response_format": "detailed"}
    key = _cache_key(fingerprint, "cortex_overview", arguments)
    store.set_query_cache(
        repo,
        key,
        json.dumps({"repo_path": str(repo), "report": "old", "top_hotspots": []}),
    )

    result = call_tool("cortex_overview", arguments)
    payload = json.loads(result["content"][0]["text"])
    assert {"installed", "model_ready", "indexed_chunks", "reason"} <= set(payload["semantic"])

    cached = json.loads(store.get_query_cache(repo, key))
    assert "semantic" in cached


def test_regex_qt_symbols_are_chunked(fake_local_model, tmp_path):
    from cortex import semantic
    from evals.run_evals import _build_qt_app

    repo = _build_qt_app(tmp_path)
    db_path = tmp_path / "qt.db"
    ingest_repository(repo, db_path=db_path)
    store = CortexStore(db_path)
    nodes, _ = store.fetch_graph(repo)
    labels = {node.label for node in nodes if node.granularity == "symbol"}
    assert {"onClicked", "deviceConnected"} <= labels
    rows = store.fetch_chunk_embeddings(repo, MODEL_ID, semantic._model_details().version)
    embedded_ids = {row["node_id"] for row in rows}
    assert {
        "symbol:qml/DeviceDelegate.qml:MouseArea.onClicked",
        "symbol:qml/DeviceDelegate.qml:clicked",
        "symbol:include/DeviceManager.hpp:deviceConnected",
        "symbol:include/DeviceModel.hpp:onDeviceConnected",
    } <= embedded_ids


def test_ingest_overhead_ratio_logic():
    from evals.run_evals import ingest_overhead_ratio

    assert ingest_overhead_ratio(0, 1) is None
    assert ingest_overhead_ratio(2, 3) == 1.5
    assert ingest_overhead_ratio(1, 2) == 2.0


def test_repeated_query_latency_measurement_reports_warmed_medians(fake_local_model, tmp_path):
    from evals.run_evals import measure_query_latency

    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _commit(repo, "initial")
    db_path = tmp_path / "index.db"
    ingest_repository(repo, db_path=db_path)
    result = measure_query_latency(repo, "credential verification", db_path, budget=100, repeats=3)
    assert result["available"] is True
    assert result["model_loaded_before_timing"] is True
    assert len(result["off_samples_ms"]) == 3
    assert len(result["semantic_on_samples_ms"]) == 3
    assert result["off_median_ms"] == sorted(result["off_samples_ms"])[1]
    assert result["semantic_on_median_ms"] == sorted(result["semantic_on_samples_ms"])[1]


def test_repeated_ingest_overhead_measurement_reports_medians(fake_local_model, tmp_path):
    from evals.run_evals import measure_ingest_overhead

    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    _commit(repo, "initial")
    result = measure_ingest_overhead(repo, repeats=3)
    assert result["available"] is True
    assert result["repeats"] == 3
    assert len(result["baseline_samples"]) == 3
    assert len(result["semantic_samples"]) == 3
    assert len(result["ratio_samples"]) == 3
    assert result["ratio"] == sorted(result["ratio_samples"])[1]
