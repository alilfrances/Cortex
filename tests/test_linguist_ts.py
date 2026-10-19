from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository
from cortex.store import CortexStore, default_db_path

LINGUIST = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n<TS version="2.1" language="de_DE">\n<context><name>Device</name><message><source>Reconnect</source><translation>Neu verbinden</translation></message></context>\n</TS>\n'


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "i18n").mkdir()
    (repo / "i18n" / "app_de.ts").write_text(LINGUIST)
    (repo / "i18n" / "bom.ts").write_text("﻿\n  " + LINGUIST)
    (repo / "real.ts").write_text("export function reconnect(): number {\n  return 1;\n}\n")
    (repo / "ambiguous.ts").write_text("// <?xml in a comment is still typescript\nexport const x = 1;\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    return repo


def test_linguist_catalogues_are_text_and_typescript_is_code(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _repo(tmp_path)
    ingest_repository(repo)
    store = CortexStore(default_db_path(repo))
    kinds = {source.path: source.kind for source in store.fetch_sources(repo)}
    assert kinds["i18n/app_de.ts"] == "text"
    assert kinds["i18n/bom.ts"] == "text"
    assert kinds["real.ts"] == "code"
    assert kinds["ambiguous.ts"] == "code"
    nodes, _edges = store.fetch_graph(repo)
    symbols_by_file = {node.source_ref for node in nodes if node.granularity == "symbol"}
    assert "real.ts" in symbols_by_file
    assert "i18n/app_de.ts" not in symbols_by_file


def test_incremental_scan_classifies_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = _repo(tmp_path)
    ingest_repository(repo)
    (repo / "i18n" / "app_fr.ts").write_text(LINGUIST.replace("de_DE", "fr_FR"))
    ingest_repository(repo, incremental=True)
    kinds = {source.path: source.kind for source in CortexStore(default_db_path(repo)).fetch_sources(repo)}
    assert kinds["i18n/app_fr.ts"] == "text"
