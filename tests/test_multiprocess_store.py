"""Task 1 (0.10.0): multi-process database safety.

WAL + a 30 s busy timeout on every connection, and a cross-process advisory
``ingest.lock`` next to the database.  The MCP auto-refresh path re-checks the
fingerprint after taking the lock so a process that waited behind another's
ingest does not ingest again.

The lock is re-entrant per thread: ``_ensure_fresh`` holds it and then calls
``ingest_repository``, which takes it again.  A naive second ``flock`` on a new
file descriptor in the same process would block forever, so the nested case is
tested explicitly (with a hard timeout so a regression fails instead of hanging).
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import cortex.store as store_module
from cortex.deadline import DeadlineExceeded, deadline_scope
from cortex.ingest import compute_repo_fingerprint, ingest_repository
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore, IngestLockTimeout, default_db_path, ingest_lock, ingest_lock_path
from tests.baseline_helpers import commit_all, fixed_git_env, init_git

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: int = 6) -> Path:
    fixed_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_git(repo)
    for index in range(files):
        (repo / f"m{index}.py").write_text(
            f"def f{index}():\n    return {index}\n", encoding="utf-8"
        )
    commit_all(repo, "init")
    return repo


def _run_with_timeout(target, seconds: float = 20.0):
    """Run ``target`` in a daemon thread; fail (not hang) on deadlock."""
    outcome: dict[str, object] = {}

    def runner() -> None:
        try:
            outcome["value"] = target()
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "deadlock: call did not return"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


# --------------------------------------------------------------------------- #
# Connection setup
# --------------------------------------------------------------------------- #


def test_journal_mode_reads_back_wal_and_busy_timeout(tmp_path: Path) -> None:
    store = CortexStore(tmp_path / "db" / "cortex.db")

    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_in_memory_database_is_not_switched_to_wal() -> None:
    connection = sqlite3.connect(":memory:")
    store_module._configure_connection(connection, Path(":memory:"))

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "memory"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_wal_sidecars_and_lock_file_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)
    db_path = default_db_path(repo)
    store = CortexStore(db_path)  # keep a connection open so the sidecars exist
    store.set_repo_fingerprint(repo, "x")

    for name in ("cortex.db", "cortex.db-wal", "cortex.db-shm", "ingest.lock"):
        path = db_path.parent / name
        assert path.exists(), name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
    store.connection.close()


def test_two_store_instances_write_graph_rows_without_locking_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "cortex.db"
    repo = tmp_path / "repo"
    first = CortexStore(db_path)
    second = CortexStore(db_path)

    for round_ in range(20):
        for label, store in (("a", first), ("b", second)):
            node = GraphNode(node_id=f"file:{label}{round_}.py", kind="file", label=f"{label}{round_}.py", source_ref=f"{label}{round_}.py")
            edge = GraphEdge(
                edge_id=f"e:{label}{round_}", source=node.node_id, target=node.node_id,
                relation="imports", layer="STRUCTURAL",
            )
            store.append_graph(repo, [node], [edge])

    nodes, edges = first.fetch_graph(repo)
    assert len(nodes) == 40 and len(edges) == 40
    assert first.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --------------------------------------------------------------------------- #
# ingest_lock semantics (in-process)
# --------------------------------------------------------------------------- #


def test_lock_file_lives_next_to_the_database_not_in_the_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lock_path = ingest_lock_path(repo)

    assert lock_path == default_db_path(repo).parent / "ingest.lock"
    with ingest_lock(repo):
        assert lock_path.exists()
    assert not (repo / "ingest.lock").exists()


def test_nested_acquisition_in_one_thread_does_not_deadlock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)
    (repo / "m0.py").write_text("def f0():\n    return 'changed'\n", encoding="utf-8")

    def nested() -> dict:
        with ingest_lock(repo.resolve(), timeout=5):
            with ingest_lock(repo.resolve(), timeout=5):
                pass
            # Exactly the _ensure_fresh -> ingest_repository shape.
            return ingest_repository(repo, incremental=True, lock_timeout=5)

    summary = _run_with_timeout(nested)
    assert summary["updated_files"] == 1
    # Fully released afterwards: a fresh acquisition succeeds immediately.
    def reacquire() -> None:
        with ingest_lock(repo.resolve(), timeout=1):
            pass

    _run_with_timeout(reacquire)


def test_other_thread_waits_and_times_out_while_lock_is_held(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with ingest_lock(repo):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(5)
    started = time.monotonic()
    with pytest.raises(IngestLockTimeout):
        with ingest_lock(repo, timeout=0.3):
            pass
    assert time.monotonic() - started >= 0.25
    release.set()
    thread.join(5)
    with ingest_lock(repo, timeout=1):  # released for everyone afterwards
        pass


def _hold_lock_in_subprocess(repo: Path, ready: Path, release: Path) -> subprocess.Popen:
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(SRC)!r})
        from cortex.store import ingest_lock
        with ingest_lock(Path({str(repo)!r})):
            Path({str(ready)!r}).write_text("1")
            deadline = time.monotonic() + 30
            while not Path({str(release)!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        """
    )
    return subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ))


def _wait_for(path: Path, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < deadline, f"timed out waiting for {path}"
        time.sleep(0.02)


def test_lock_excludes_other_processes_and_honours_timeout_and_deadline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = _hold_lock_in_subprocess(repo, ready, release)
    try:
        _wait_for(ready)
        with pytest.raises(IngestLockTimeout):
            with ingest_lock(repo, timeout=0.3):
                pass
        # Inside an MCP call the wait is bounded by the call's deadline too.
        with deadline_scope(0.2, tool_name="cortex_query"):
            with pytest.raises(DeadlineExceeded):
                with ingest_lock(repo, timeout=30):
                    pass
    finally:
        release.write_text("1")
        assert proc.wait(20) == 0
    with ingest_lock(repo, timeout=2):
        pass


def test_ensure_fresh_answers_stale_when_another_process_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cortex.mcp.tools as tools

    repo = _repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)
    (repo / "m1.py").write_text("def f1():\n    return 'edited'\n", encoding="utf-8")
    monkeypatch.setattr(store_module, "INGEST_LOCK_TIMEOUT_SECONDS", 0.3)
    ready, release = tmp_path / "ready", tmp_path / "release"
    proc = _hold_lock_in_subprocess(repo.resolve(), ready, release)
    try:
        _wait_for(ready)
        store = CortexStore(default_db_path(repo))
        status = tools._ensure_fresh(store, repo.resolve())
    finally:
        release.write_text("1")
        assert proc.wait(20) == 0

    assert status["stale"] is True
    assert "auto_refreshed" not in status


# --------------------------------------------------------------------------- #
# Two real processes
# --------------------------------------------------------------------------- #

_ENSURE_FRESH_WORKER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {src!r})
import cortex.mcp.tools as tools
from cortex.store import CortexStore, default_db_path

repo = Path({repo!r}).resolve()
work = Path({work!r})
me = sys.argv[1]
ingest_log = work / "ingests.log"

real_ingest = tools.ingest_repository
def counting_ingest(*args, **kwargs):
    with open(ingest_log, "a") as handle:
        handle.write(me + "\\n")
    time.sleep(1.0)  # widen the window: the other process must queue on the lock
    return real_ingest(*args, **kwargs)
tools.ingest_repository = counting_ingest

real_staleness = tools._staleness
calls = {{"n": 0}}
def barrier_staleness(store, root):
    status = real_staleness(store, root)
    calls["n"] += 1
    if calls["n"] == 1:
        # Both processes must have observed the stale index before either
        # takes the lock -- otherwise the test proves nothing.
        assert status["stale"], "fixture must start stale"
        (work / ("checked-" + me)).write_text("1")
        deadline = time.monotonic() + 30
        while not all((work / ("checked-" + p)).exists() for p in ("A", "B")):
            assert time.monotonic() < deadline, "barrier timeout"
            time.sleep(0.01)
    return status
tools._staleness = barrier_staleness

store = CortexStore(default_db_path(repo))
status = tools._ensure_fresh(store, repo)
print(json.dumps({{"who": me, "stale": status["stale"], "auto_refreshed": "auto_refreshed" in status}}))
"""

_INGEST_WORKER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {src!r})
import cortex.ingest as ingest

work = Path({work!r})
me = sys.argv[1]
real = ingest._ingest_locked
def timed(*args, **kwargs):
    start = time.monotonic()
    time.sleep(0.3)
    try:
        return real(*args, **kwargs)
    finally:
        with open(work / "intervals.log", "a") as handle:
            handle.write(json.dumps([me, start, time.monotonic()]) + "\\n")
ingest._ingest_locked = timed

(work / ("ready-" + me)).write_text("1")
deadline = time.monotonic() + 30
while not (work / "go").exists():
    assert time.monotonic() < deadline
    time.sleep(0.005)
summary = ingest.ingest_repository(Path({repo!r}), incremental=True)
print(json.dumps({{"who": me, "updated": summary["updated_files"]}}))
"""


def _spawn(script: str, who: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, who],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def _finish(proc: subprocess.Popen) -> dict:
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, err
    return json.loads(out.strip().splitlines()[-1])


def _assert_consistent(repo: Path) -> None:
    store = CortexStore(default_db_path(repo))
    assert store.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.get_repo_fingerprint(repo) == compute_repo_fingerprint(repo.resolve())


def test_two_processes_auto_refresh_concurrently_and_only_one_ingests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    ingest_repository(repo, commit_limit=10)
    store = CortexStore(default_db_path(repo))
    updated_at_before = store.get_repo_indexed_at(repo)
    (repo / "m2.py").write_text("def f2():\n    return 'edited'\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    script = _ENSURE_FRESH_WORKER.format(src=str(SRC), repo=str(repo), work=str(work))

    procs = [_spawn(script, who) for who in ("A", "B")]
    results = sorted((_finish(proc) for proc in procs), key=lambda item: item["who"])

    ingests = (work / "ingests.log").read_text(encoding="utf-8").split()
    assert len(ingests) == 1, ingests  # the waiter re-checked and skipped
    assert [r["stale"] for r in results] == [False, False]
    assert sum(r["auto_refreshed"] for r in results) == 1
    ingested_by = next(r["who"] for r in results if r["auto_refreshed"])
    assert ingests == [ingested_by]
    _assert_consistent(repo)
    assert store.get_repo_indexed_at(repo) >= updated_at_before


def test_two_processes_ingest_repository_concurrently_serialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch, files=30)
    ingest_repository(repo, commit_limit=10)
    for index in range(0, 30, 3):
        (repo / f"m{index}.py").write_text(f"def f{index}():\n    return 'v2'\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    script = _INGEST_WORKER.format(src=str(SRC), repo=str(repo), work=str(work))

    procs = [_spawn(script, who) for who in ("A", "B")]
    _wait_for(work / "ready-A")
    _wait_for(work / "ready-B")
    (work / "go").write_text("1")
    results = sorted((_finish(proc) for proc in procs), key=lambda item: item["who"])

    # The direct ingest API has no fingerprint re-check (a commit can change
    # history without touching any file), so both run -- but never overlap,
    # and the second sees the first's writes (nothing left to update).
    intervals = sorted(
        json.loads(line) for line in (work / "intervals.log").read_text(encoding="utf-8").splitlines()
    )
    (_, a_start, a_end), (_, b_start, b_end) = intervals
    assert a_end <= b_start or b_end <= a_start, intervals
    assert sorted(r["updated"] for r in results) == [0, 10]
    _assert_consistent(repo)
