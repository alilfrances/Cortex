"""``CortexStore`` connection setup and ``cortex gc`` at 7b0e6e0 (Task 0 baseline).

Task 0 pinned the single-process-oriented defaults of 7b0e6e0: SQLite's Python
default ``timeout=5.0`` (``busy_timeout = 5000``), journal mode ``delete``, and a data
dir holding only ``cortex.db`` + ``meta.json``.

Task 1 (0.10.0, multi-process safety) flipped the assertions marked ``TASK 1``:
``busy_timeout = 30000``, ``journal_mode = wal`` for on-disk databases, and the data
dir now also holds ``ingest.lock`` (plus transient ``-wal``/``-shm`` sidecars while a
connection is open).  Unchanged: ``foreign_keys`` is on; a second writer cannot write
while another connection holds a write transaction (``database is locked`` once its
busy timeout lapses) but readers can read.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cortex.cli import gc_data_dirs
from cortex.store import CortexStore, data_root, default_db_path
from tests.baseline_helpers import commit_all, fixed_git_env, init_git


def _pragma(store: CortexStore, name: str) -> object:
    return store.connection.execute(f"PRAGMA {name}").fetchone()[0]


def _tiny_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fixed_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_git(repo)
    (repo / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (repo / "b.py").write_text("from a import a\n\n\ndef b():\n    return a()\n", encoding="utf-8")
    commit_all(repo, "init")
    return repo


def test_journal_mode_is_wal(tmp_path: Path) -> None:
    """TASK 1 FLIP: was ``delete`` at 7b0e6e0; ``wal`` for on-disk databases now."""
    managed = CortexStore(default_db_path(tmp_path / "some-repo"))
    unmanaged = CortexStore(tmp_path / "elsewhere" / "cortex.db")

    assert _pragma(managed, "journal_mode") == "wal"
    assert _pragma(unmanaged, "journal_mode") == "wal"


def test_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    """TASK 1 FLIP: ``busy_timeout`` was 5000 at 7b0e6e0; now 30000."""
    store = CortexStore(tmp_path / "cortex.db")

    assert _pragma(store, "busy_timeout") == 30000
    assert _pragma(store, "foreign_keys") == 1
    assert store.connection.row_factory is sqlite3.Row


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_managed_database_is_private(tmp_path: Path) -> None:
    db_path = default_db_path(tmp_path / "some-repo")
    CortexStore(db_path)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700


def test_ingest_data_dir_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 1 FLIP: was exactly ``["cortex.db", "meta.json"]`` at 7b0e6e0.  Now the
    ingest lock file is always left behind, and SQLite's ``-wal``/``-shm`` sidecars
    exist while any connection is open (removed on the last clean close)."""
    from cortex.ingest import ingest_repository

    repo = _tiny_repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)

    data_dir = default_db_path(repo).parent
    names = {p.name for p in data_dir.iterdir()}
    assert {"cortex.db", "meta.json", "ingest.lock"} <= names
    assert names <= {"cortex.db", "meta.json", "ingest.lock", "cortex.db-wal", "cortex.db-shm"}
    assert json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))["repo_path"] == str(repo.resolve())


def test_two_stores_read_concurrently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cortex.ingest import ingest_repository

    repo = _tiny_repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)
    db_path = default_db_path(repo)
    expected_nodes, expected_edges = CortexStore(db_path).fetch_graph(repo.resolve())
    assert expected_nodes and expected_edges

    def read_many(_: int) -> tuple[int, int]:
        store = CortexStore(db_path)  # each worker owns its connection
        counts = (0, 0)
        for _ in range(25):
            nodes, edges = store.fetch_graph(repo.resolve())
            counts = (len(nodes), len(edges))
        return counts

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(read_many, range(2)))

    assert results == [(len(expected_nodes), len(expected_edges))] * 2


def test_writer_blocks_second_writer_but_not_readers(tmp_path: Path) -> None:
    """Rollback-journal behaviour.  Still true under WAL (one writer at a time), but the
    error then only appears after the 30 s busy timeout -- Task 1 must keep the reader
    and the post-commit assertions and adapt the timing."""
    db_path = tmp_path / "cortex.db"
    writer = CortexStore(db_path)
    other = CortexStore(db_path)
    other.connection.execute("PRAGMA busy_timeout = 100")  # keep the test fast

    writer.connection.execute("BEGIN IMMEDIATE")
    writer.connection.execute("INSERT INTO repos(repo_path, updated_at) VALUES ('w', 1)")

    assert other.connection.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0  # reader unblocked
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        other.connection.execute("INSERT INTO repos(repo_path, updated_at) VALUES ('o', 2)")

    writer.connection.commit()
    other.connection.execute("INSERT INTO repos(repo_path, updated_at) VALUES ('o', 2)")
    other.connection.commit()
    rows = writer.connection.execute("SELECT repo_path FROM repos ORDER BY repo_path").fetchall()
    assert [r[0] for r in rows] == ["o", "w"]


def test_second_writer_loses_to_uncommitted_ingest_connection(tmp_path: Path) -> None:
    """SQLite still allows one writer at a time under WAL; a second ``BEGIN IMMEDIATE``
    fails once its busy timeout lapses.  Cross-process ingest is serialized by
    ``ingest.lock`` (Task 1) rather than by SQLite alone."""
    db_path = tmp_path / "cortex.db"
    first = CortexStore(db_path)
    second = CortexStore(db_path)
    second.connection.execute("PRAGMA busy_timeout = 50")

    first.connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        second.connection.execute("BEGIN IMMEDIATE")
    first.connection.rollback()


# --------------------------------------------------------------------------- #
# cortex gc
# --------------------------------------------------------------------------- #


def _make_data_dir(name: str, repo_path: str | None, files: tuple[str, ...] = ("cortex.db",)) -> Path:
    entry = data_root() / name
    entry.mkdir(parents=True)
    if repo_path is not None:
        (entry / "meta.json").write_text(json.dumps({"repo_path": repo_path, "updated_at": 1}), encoding="utf-8")
    for filename in files:
        (entry / filename).write_bytes(b"")
    return entry


def test_gc_classifies_directories_and_ignores_files(tmp_path: Path) -> None:
    live_repo = tmp_path / "live"
    live_repo.mkdir()
    active = _make_data_dir("0active", str(live_repo))
    orphan = _make_data_dir("1orphan", str(tmp_path / "deleted-repo"))
    unknown = _make_data_dir("2unknown", None)
    bad_meta = _make_data_dir("3badmeta", None)
    (bad_meta / "meta.json").write_text("{not json", encoding="utf-8")
    (data_root() / "semantic").mkdir()  # managed model cache: never classified
    (data_root() / "mcp-activity").mkdir()
    # Stray *files* next to the data dirs (sidecars, lock files) must be ignored.
    for stray in ("cortex.db-wal", "cortex.db-shm", "ingest.lock", "notes.txt"):
        (data_root() / stray).write_text("x", encoding="utf-8")

    result = gc_data_dirs()

    assert result["active"] == [{"dir": str(active), "repo_path": str(live_repo)}]
    assert result["orphaned"] == [{"dir": str(orphan), "repo_path": str(tmp_path / "deleted-repo")}]
    assert sorted(item["dir"] for item in result["unknown"]) == [str(unknown), str(bad_meta)]
    assert result["pruned"] == []
    assert orphan.exists()


def test_gc_prune_removes_only_orphans_and_sidecar_files_do_not_change_classification(tmp_path: Path) -> None:
    live_repo = tmp_path / "live"
    live_repo.mkdir()
    sidecars = ("cortex.db", "cortex.db-wal", "cortex.db-shm", "ingest.lock")
    active = _make_data_dir("0active", str(live_repo), files=sidecars)
    orphan = _make_data_dir("1orphan", str(tmp_path / "gone"), files=sidecars)

    before = gc_data_dirs()
    pruned = gc_data_dirs(prune=True)

    assert [item["dir"] for item in before["active"]] == [str(active)]
    assert [item["dir"] for item in before["orphaned"]] == [str(orphan)]
    assert [item["dir"] for item in pruned["pruned"]] == [str(orphan)]
    assert active.exists() and (active / "ingest.lock").exists() and not orphan.exists()


def test_gc_on_missing_data_root_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "does-not-exist"))

    assert gc_data_dirs() == {"active": [], "orphaned": [], "unknown": [], "pruned": []}
    assert os.environ["CORTEX_DATA_DIR"] == str(tmp_path / "does-not-exist")
