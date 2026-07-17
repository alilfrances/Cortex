from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cortex.bundle import generate_bundle
from cortex.config import CortexConfig, load_config
from cortex.ingest import ingest_repository
from cortex.store import CortexStore, default_db_path


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


def _write_config(repo: Path, body: str) -> None:
    (repo / ".cortex").mkdir(parents=True, exist_ok=True)
    (repo / ".cortex" / "config.toml").write_text(body, encoding="utf-8")


def test_load_config_defaults_when_missing(tmp_path):
    config = load_config(tmp_path)
    assert config == CortexConfig()
    assert config.connect_functions == ["connect"]
    assert config.skip_dirs == []
    assert config.noise_identifiers == []
    assert config.synonyms == {}


def test_load_config_malformed_raises_value_error_with_path(tmp_path):
    _write_config(tmp_path, "[parsing\nbroken =")
    with pytest.raises(ValueError, match="config.toml"):
        load_config(tmp_path)


def test_load_config_wrong_type_raises_value_error(tmp_path):
    _write_config(tmp_path, '[ingest]\nskip_dirs = "third_party"\n')
    with pytest.raises(ValueError, match="skip_dirs"):
        load_config(tmp_path)


def test_config_wires_wrapper_connects_skip_dirs_and_synonyms(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_config(
        repo,
        "\n".join(
            [
                "[parsing]",
                'connect_functions = ["connect", "safeConnect"]',
                "[ingest]",
                'skip_dirs = ["generated"]',
                "[query]",
                'synonyms = { frobnicate = ["zorble"] }',
            ]
        )
        + "\n",
    )
    (repo / "wiring.cpp").write_text(
        "void setup() {\n  safeConnect(a, SIGNAL(x()), b, SLOT(y()));\n}\n"
    )
    (repo / "zorble.py").write_text("def zorble():\n    return 1\n")
    generated = repo / "generated"
    generated.mkdir()
    (generated / "machine.py").write_text("def machine_made(): pass\n")
    subprocess.run(["git", "add", "-f", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    summary = ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    sources = {s.path for s in store.fetch_sources(repo)}

    # skip_dirs extends the built-in skip list.
    assert "generated/machine.py" not in sources
    assert "wiring.cpp" in sources
    assert summary["source_count"] == 2

    # The wrapper macro is parsed into a connects edge.
    _nodes, edges = store.fetch_graph(repo)
    connects = [e for e in edges if e.relation == "connects"]
    assert len(connects) == 1
    assert connects[0].metadata["source_file"] == "wiring.cpp"

    # A task phrased with the synonym key still name-matches the symbol.
    bundle = generate_bundle(repo_path=repo, task="fix frobnicate", budget=4000, output_format="json")
    assert isinstance(bundle, dict)
    assert any(item["path"] == "zorble.py" for item in bundle["items"])


def test_synonyms_expand_value_to_key_direction(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_config(repo, '[query]\nsynonyms = { frobnicate = ["zorble"] }\n')
    (repo / "frobnicate.py").write_text("def frobnicate():\n    return 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)
    bundle = generate_bundle(repo_path=repo, task="fix zorble", budget=4000, output_format="json")
    assert isinstance(bundle, dict)
    assert any(item["path"] == "frobnicate.py" for item in bundle["items"])


def test_noise_identifiers_excluded_from_keyword_matching(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _write_config(repo, '[parsing]\nnoise_identifiers = ["ZORBLE_TRACE"]\n')
    (repo / "noisy.py").write_text('def unrelated():\n    log("ZORBLE_TRACE enabled")\n')
    # Backdate the file so the recency weight cannot give it a positive score.
    os.utime(repo / "noisy.py", (0, 0))
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    ingest_repository(repo)
    bundle = generate_bundle(repo_path=repo, task="zorble trace bug", budget=4000, output_format="json")
    assert isinstance(bundle, dict)
    assert not any(item["path"] == "noisy.py" for item in bundle["items"])
