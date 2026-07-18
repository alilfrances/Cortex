from __future__ import annotations

from pathlib import Path

from cortex.models import SourceRecord
from cortex.store import CortexStore


def _source(path: str, content: str, kind: str = "code") -> SourceRecord:
    return SourceRecord(
        path=path,
        content=content,
        kind=kind,
        size_bytes=len(content),
        modified_at=0.0,
        content_hash="",
        mtime_ns=0,
    )


def test_fts5_available_in_test_environment(tmp_path):
    """Sanity check: the rest of this module assumes FTS5 is compiled into
    this environment's sqlite3 build (true for stock CPython on Linux/Mac
    since 3.9-ish). If this fails, the *feature* isn't broken -- the
    environment's sqlite3 lacks FTS5 -- see the fallback tests below for
    the behavior that matters in that case."""
    store = CortexStore(tmp_path / "cortex.db")
    assert store.fts_enabled is True


def test_save_sources_syncs_fts_rows(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "raise ValueError('device offline retry gateway')")])

    hits = store.search_fulltext(repo, "device offline gateway", limit=10)
    paths = [path for path, _score, _snippet in hits]
    assert "app/errors.py" in paths


def test_delete_sources_removes_fts_rows(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "device offline retry gateway message")])
    assert store.search_fulltext(repo, "device offline gateway")

    store.delete_sources(repo, ["app/errors.py"])
    assert store.search_fulltext(repo, "device offline gateway") == []


def test_reset_repo_clears_fts_rows(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "device offline retry gateway message")])
    assert store.search_fulltext(repo, "device offline gateway")

    store.reset_repo(repo)
    assert store.search_fulltext(repo, "device offline gateway") == []


def test_save_sources_resaving_a_path_replaces_stale_fts_row(tmp_path):
    """Re-saving the same path with new content must not leave the old
    content still matchable -- save_sources deletes the old FTS row before
    inserting the new one (same delta granularity as the sources table)."""
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "original gremlin phrase here")])
    assert [p for p, _s, _sn in store.search_fulltext(repo, "gremlin")] == ["app/errors.py"]

    store.save_sources(repo, [_source("app/errors.py", "updated content, no trace of the old word")])
    assert store.search_fulltext(repo, "gremlin") == []
    assert [p for p, _s, _sn in store.search_fulltext(repo, "updated content")] == ["app/errors.py"]


def test_search_fulltext_returns_empty_list_for_missing_fts5(tmp_path, monkeypatch):
    """No-FTS5 fallback (P0-2 acceptance criterion): a store whose sqlite3
    build lacks FTS5 must degrade gracefully everywhere instead of
    raising."""
    monkeypatch.setattr(CortexStore, "_init_fts5", lambda self: False)
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    assert store.fts_enabled is False

    # save/delete/reset must not raise even though FTS is unavailable.
    store.save_sources(repo, [_source("app/errors.py", "device offline retry gateway")])
    store.delete_sources(repo, ["app/errors.py"])
    store.reset_repo(repo)

    assert store.search_fulltext(repo, "device offline") == []


def test_search_fulltext_empty_query_returns_empty_list(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "device offline retry gateway")])
    assert store.search_fulltext(repo, "") == []
    assert store.search_fulltext(repo, "   ") == []


def test_search_fulltext_ranks_multi_term_match_above_single_term(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(
        repo,
        [
            _source("both.py", "the device went offline and needs a manual retry of the gateway link"),
            _source("one.py", "unrelated text that only happens to mention offline once"),
        ],
    )
    hits = store.search_fulltext(repo, "device offline gateway retry", limit=10)
    paths = [path for path, _score, _snippet in hits]
    assert paths[0] == "both.py"


def test_search_fulltext_snippet_is_line_anchored(tmp_path):
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    content = "line one\nline two\nDEVICE_OFFLINE = 'please retry the gateway connection'\nline four\n"
    store.save_sources(repo, [_source("app/messages.py", content)])
    hits = store.search_fulltext(repo, "gateway connection", limit=5)
    assert len(hits) == 1
    _path, _score, snippet = hits[0]
    assert snippet.startswith("L3:")


def test_search_fulltext_identifier_aware_split_word_query_matches_camelcase(tmp_path):
    """P0-2 step 6: a split-word query ('device connected') should match a
    file whose only on-disk spelling is a compound camelCase identifier
    ('deviceConnected'), via the auxiliary `identifiers` column -- the raw
    unicode61-tokenized `content` column alone would index that as one
    opaque token and miss a split-word query."""
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(
        repo,
        [_source("include/DeviceManager.hpp", "signal:\n    void deviceConnected(int deviceId);\n")],
    )
    hits = store.search_fulltext(repo, "device connected", limit=5)
    paths = [path for path, _score, _snippet in hits]
    assert "include/DeviceManager.hpp" in paths


def test_search_fulltext_qualified_cpp_identifier_query_matches(tmp_path):
    """A `Class::method`-shaped query must not blow up FTS5 MATCH syntax
    (`::` and other punctuation are escaped/tokenized, not passed raw)."""
    store = CortexStore(tmp_path / "cortex.db")
    repo = tmp_path / "repo"
    store.save_sources(
        repo,
        [_source("src/DeviceManager.cpp", "void DeviceManager::scan() {\n    emit deviceConnected(42);\n}\n")],
    )
    hits = store.search_fulltext(repo, "DeviceManager::scan", limit=5)
    paths = [path for path, _score, _snippet in hits]
    assert "src/DeviceManager.cpp" in paths


def test_backfill_fts5_populates_rows_for_preexisting_sources(tmp_path):
    """Upgrade path (P0-2): a database created before FTS5 support existed
    (no source_fts table) must have its existing `sources` rows backfilled
    into source_fts the first time a store with FTS5 support opens it."""
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    repo = tmp_path / "repo"
    store.save_sources(repo, [_source("app/errors.py", "device offline retry gateway")])

    # Simulate a pre-P0-2 database: drop the FTS table entirely.
    store.connection.execute("DROP TABLE IF EXISTS source_fts")
    store.connection.commit()
    store.connection.close()

    reopened = CortexStore(db_path)
    assert reopened.fts_enabled is True
    hits = reopened.search_fulltext(repo, "device offline gateway")
    paths = [path for path, _score, _snippet in hits]
    assert "app/errors.py" in paths
