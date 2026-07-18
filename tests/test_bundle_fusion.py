from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.bundle import _looks_like_identifier_query, generate_bundle
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore


def _git_init(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)


def _source(path: str, content: str, kind: str = "code") -> SourceRecord:
    return SourceRecord(path=path, content=content, kind=kind, size_bytes=len(content), modified_at=0.0, content_hash=path)


# --- _looks_like_identifier_query -------------------------------------------------


def test_looks_like_identifier_query_detects_double_colon_qualified_name():
    assert _looks_like_identifier_query("Where is MyClass::mySignal emitted") is True


def test_looks_like_identifier_query_detects_camel_case():
    assert _looks_like_identifier_query("Where is the deviceConnected signal emitted") is True


def test_looks_like_identifier_query_detects_snake_case():
    assert _looks_like_identifier_query("locate device_list_model usage") is True


def test_looks_like_identifier_query_false_for_plain_english():
    assert _looks_like_identifier_query("find markdown setup guidance for plugin installation") is False


def test_looks_like_identifier_query_false_for_empty_string():
    assert _looks_like_identifier_query("") is False


# --- NAME_MATCH_BONUS dominance over FTS body text --------------------------------


def _make_name_vs_body_store(tmp_path: Path) -> tuple[CortexStore, Path]:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    _git_init(repo)

    sources = [
        _source("auth.py", "def login(): pass"),
        # Body text dense with the query term, but the filename/path shares
        # nothing with the task and it defines no matching symbol -- must
        # not out-rank the exact stem match no matter how strong its FTS
        # body-text signal is.
        _source("docs/noise.md", ("auth " * 40) + "unrelated filler text about other topics entirely", kind="markdown"),
    ]
    nodes = [GraphNode(node_id=f"file:{s.path}", kind="file", label=s.path, source_ref=s.path) for s in sources]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_name_match_bonus_dominates_fts_boosted_body_text(tmp_path):
    store, repo = _make_name_vs_body_store(tmp_path)
    result = generate_bundle(repo, task="auth", budget=4000, db_path=store.db_path, output_format="json", rank="bfs")
    scores = {item["path"]: item["score"] for item in result["items"]}
    assert scores["auth.py"] > scores["docs/noise.md"]


# --- FTS fusion breaks a tie plain keyword-overlap scoring cannot ------------------


def _make_tie_break_store(tmp_path: Path) -> tuple[CortexStore, Path]:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    _git_init(repo)

    # Both files contain the exact same SET of query terms (so the existing
    # set-based keyword-overlap scorer, _score_text, scores them
    # identically) but at very different term frequency / document length,
    # which only a real ranking function (BM25) can distinguish.
    dense = "gateway retry connection " * 5
    sparse = "gateway retry connection " + ("filler word padding text here " * 60)
    sources = [
        _source("a_dense.py", dense),
        _source("b_sparse.py", sparse),
    ]
    nodes = [GraphNode(node_id=f"file:{s.path}", kind="file", label=s.path, source_ref=s.path) for s in sources]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_fts_fusion_breaks_tie_plain_keyword_overlap_cannot(tmp_path):
    store, repo = _make_tie_break_store(tmp_path)
    result = generate_bundle(
        repo, task="gateway retry connection", budget=4000, db_path=store.db_path, output_format="json", rank="bfs"
    )
    scores = {item["path"]: item["score"] for item in result["items"]}
    assert scores["a_dense.py"] > scores["b_sparse.py"]


# --- Definition boost: a defining file outranks a merely-mentioning file ----------


def _make_definition_boost_store(tmp_path: Path) -> tuple[CortexStore, Path]:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    _git_init(repo)

    define_content = "class DeviceManager:\n    def scan(self):\n        pass\n"
    # Mentions the term many more times than the defining file, so its raw
    # FTS body-text term-frequency signal is *stronger* -- definition boost
    # must still let the defining file win overall.
    mention_content = "# Notes\n\nCall scan to scan the scan results. scan scan scan scan scan.\n"
    sources = [
        _source("device.py", define_content),
        _source("notes.md", mention_content, kind="markdown"),
    ]
    nodes = [
        GraphNode(node_id="file:device.py", kind="file", label="device.py", source_ref="device.py"),
        GraphNode(node_id="file:notes.md", kind="file", label="notes.md", source_ref="notes.md"),
        GraphNode(
            node_id="symbol:device.py:DeviceManager.scan",
            kind="func",
            label="scan",
            source_ref="device.py",
            granularity="symbol",
        ),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_definition_boost_symbol_defining_file_outranks_mention_only_file(tmp_path):
    store, repo = _make_definition_boost_store(tmp_path)
    result = generate_bundle(repo, task="scan", budget=4000, db_path=store.db_path, output_format="json", rank="bfs")
    scores = {item["path"]: item["score"] for item in result["items"]}
    assert scores["device.py"] > scores["notes.md"]


# --- Adaptive weighting: identifier-shaped query weights name list higher --------


def _make_adaptive_weighting_store(tmp_path: Path) -> tuple[CortexStore, Path]:
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    _git_init(repo)

    content = "widget widget widget"
    sources = [
        _source("aaa_name_hit.py", content),
        _source("zzz_fts_hit.py", content),
    ]
    nodes = [GraphNode(node_id=f"file:{s.path}", kind="file", label=s.path, source_ref=s.path) for s in sources]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_commits(repo, [])
    store.save_graph(repo, nodes, [])
    return store, repo


def test_adaptive_weighting_identifier_query_widens_name_list_edge(tmp_path, monkeypatch):
    """Two files have identical keyword-overlap base scores; only
    zzz_fts_hit.py is surfaced by the (monkeypatched) FTS list. An
    identifier-shaped phrasing of the same query should widen
    aaa_name_hit.py's edge over zzz_fts_hit.py relative to a plain-language
    phrasing, because the identifier-shaped path double-counts the
    lexical/name ranked list in the fusion (P0-2 step 5)."""
    store, repo = _make_adaptive_weighting_store(tmp_path)

    monkeypatch.setattr(
        CortexStore,
        "search_fulltext",
        lambda self, repo_path, query, limit=20: [("zzz_fts_hit.py", -5.0, "snippet")],
    )

    def gap(task: str) -> float:
        result = generate_bundle(repo, task=task, budget=4000, db_path=store.db_path, output_format="json", rank="bfs")
        scores = {item["path"]: item["score"] for item in result["items"]}
        return scores.get("aaa_name_hit.py", 0.0) - scores.get("zzz_fts_hit.py", 0.0)

    identifier_gap = gap("widget_report")  # snake_case -> identifier-shaped
    plain_gap = gap("widget report")  # plain language -> not identifier-shaped

    assert identifier_gap > plain_gap


# --- Graceful no-FTS5 fallback for generate_bundle ---------------------------------


def test_generate_bundle_falls_back_gracefully_without_fts5(tmp_path, monkeypatch):
    monkeypatch.setattr(CortexStore, "_init_fts5", lambda self: False)
    store, repo = _make_name_vs_body_store(tmp_path)
    assert store.fts_enabled is False

    result = generate_bundle(repo, task="auth", budget=4000, db_path=store.db_path, output_format="json", rank="bfs")
    scores = {item["path"]: item["score"] for item in result["items"]}
    assert scores["auth.py"] > scores["docs/noise.md"]
