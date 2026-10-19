"""Task 4 (0.10.0): rename-aware git history for COCHANGE, churn and touches.

``collect_recent_commits`` parses ``git log --name-status -M -z``; renames are
recorded as ``CommitRecord.renames`` and ``gitutils.canonical_commit_files``
maps every commit's paths to the name the file has now, so co-change pairs,
churn and commit ``touches`` edges follow a file across ``git mv``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cortex.cochange import build_cochange_edges
from cortex.gitutils import (
    _parse_record,
    _split_records,
    canonical_commit_files,
    canonical_path_map,
    collect_recent_commits,
)
from cortex.hotspots import compute_churn
from cortex.ingest import _commits_changed, ingest_repository
from cortex.models import CommitRecord
from cortex.risk import analyze_risk
from cortex.store import CortexStore, default_db_path
from tests.baseline_helpers import commit_all, fixed_git_env, git, init_git

BODY = "".join(f"line {i}\n" for i in range(12))


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fixed_git_env(monkeypatch)
    repo = tmp_path / "repo"
    init_git(repo)
    git(repo, "config", "diff.renames", "false")  # -M must not depend on user config
    return repo


def _plan_history(repo: Path) -> None:
    """a.py+b.py co-change twice, ``git mv a.py c.py``, then c.py+b.py once more."""
    _write(repo, "a.py", BODY)
    _write(repo, "b.py", BODY + "b\n")
    commit_all(repo, "add a and b")
    _write(repo, "a.py", BODY + "a2\n")
    _write(repo, "b.py", BODY + "b2\n")
    commit_all(repo, "change a and b")
    git(repo, "mv", "a.py", "c.py")
    commit_all(repo, "rename a to c")
    _write(repo, "c.py", BODY + "c3\n")
    _write(repo, "b.py", BODY + "b3\n")
    commit_all(repo, "change c and b")


def _commit(sha: str, files: list[str], renames: list[tuple[str, str]] | None = None) -> CommitRecord:
    return CommitRecord(sha=sha, summary=sha, author="t", authored_at=1, files=files, renames=renames or [])


# --------------------------------------------------------------------------- #
# End to end: the plan's acceptance scenario
# --------------------------------------------------------------------------- #


def test_cochange_and_churn_follow_a_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _plan_history(repo)
    commits = collect_recent_commits(repo, 10)

    edges = {edge.edge_id: edge for edge in build_cochange_edges(commits)}
    assert edges["cochange:b.py:c.py"].metadata["count"] == 3
    assert not any("a.py" in edge_id for edge_id in edges)

    churn = compute_churn(commits)
    assert churn["c.py"] == 4
    assert "a.py" not in churn


def test_full_ingest_graph_is_rename_aware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _plan_history(repo)

    ingest_repository(repo, commit_limit=10)
    store = CortexStore(default_db_path(repo))
    nodes, edges = store.fetch_graph(repo)

    cochange = {e.edge_id: e for e in edges if e.relation == "cochange"}
    assert cochange["cochange:b.py:c.py"].metadata["count"] == 3
    assert not any("a.py" in e.edge_id for e in edges)
    # Every one of the 4 commits touching a.py/c.py now has a touches edge to c.py.
    touches_c = [e for e in edges if e.relation == "touches" and e.target == "file:c.py"]
    assert len(touches_c) == 4
    file_c = next(n for n in nodes if n.node_id == "file:c.py")
    assert file_c.metadata["hotspot"]["churn"] == 4
    # Renames are persisted with the commit.
    stored = {c.summary: c.renames for c in store.fetch_commits(repo)}
    assert stored["rename a to c"] == [("a.py", "c.py")]


def test_incremental_ingest_picks_up_a_rename_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "a.py", BODY)
    _write(repo, "b.py", BODY + "b\n")
    commit_all(repo, "add a and b")
    _write(repo, "a.py", BODY + "a2\n")
    _write(repo, "b.py", BODY + "b2\n")
    commit_all(repo, "change a and b")
    ingest_repository(repo, commit_limit=10)
    git(repo, "mv", "a.py", "c.py")
    commit_all(repo, "rename a to c")

    ingest_repository(repo, commit_limit=10, incremental=True)
    _nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)

    cochange = {e.edge_id: e for e in edges if e.relation == "cochange"}
    assert sorted(cochange) == ["cochange:b.py:c.py"]
    assert cochange["cochange:b.py:c.py"].metadata["count"] == 2


def test_database_from_before_renames_is_rebuilt_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 0.9.x database stored the same SHAs with no renames: the incremental path
    must rebuild COCHANGE/churn once instead of staying rename-blind."""
    repo = _repo(tmp_path, monkeypatch)
    _plan_history(repo)
    ingest_repository(repo, commit_limit=10)
    db_path = default_db_path(repo)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE commits SET renames_json = '[]'")
        connection.execute("DELETE FROM graph_edges WHERE layer = 'COCHANGE'")
    store = CortexStore(db_path)
    commits = collect_recent_commits(repo, 10)
    assert _commits_changed(store, repo, commits) is True

    ingest_repository(repo, commit_limit=10, incremental=True)

    assert _commits_changed(store, repo, commits) is False
    _nodes, edges = store.fetch_graph(repo)
    assert {e.edge_id for e in edges if e.relation == "cochange"} == {"cochange:b.py:c.py"}


def test_migration_adds_renames_column_to_an_old_commits_table(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE commits (repo_path TEXT NOT NULL, sha TEXT NOT NULL, summary TEXT NOT NULL, "
            "author TEXT NOT NULL, authored_at INTEGER NOT NULL, files_json TEXT NOT NULL, "
            "PRIMARY KEY (repo_path, sha))"
        )
        connection.execute(
            "INSERT INTO commits VALUES (?, 'abc', 's', 'a', 1, ?)",
            (str(tmp_path.resolve()), json.dumps(["x.py"])),
        )

    store = CortexStore(db_path)

    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(commits)")}
    assert "renames_json" in columns
    (old,) = store.fetch_commits(tmp_path)
    assert old.files == ["x.py"] and old.renames == []
    store.save_commits(tmp_path, [_commit("def", ["n.py"], [("o.py", "n.py")])])
    assert {c.sha: c.renames for c in store.fetch_commits(tmp_path)}["def"] == [("o.py", "n.py")]
    # Idempotent on reopen.
    CortexStore(db_path)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def _record(sha: str, summary: str, body: str | None) -> str:
    header = f"\x1e{sha}\x1fAuthor\x1f1700000000\x1f{summary}"
    return header + ("\0" if body is None else "\n" + body + "\0")


def test_parser_handles_every_status_kind() -> None:
    stdout = (
        _record(SHA_A, "mixed", "M\0mod.py\0A\0new file.py\0D\0gone.py\0R100\0old.py\0moved.py\0"
                "C075\0src.py\0copy.py\0T\0link\0R087\0dir a/é.py\0dir b/é.py")
        + _record(SHA_B, "merge", None)
        + _record(SHA_C, "after merge", "M\0x.py")
    )

    commits = [_parse_record(chunk) for chunk in _split_records(stdout)]

    assert [c.sha for c in commits] == [SHA_A, SHA_B, SHA_C]
    mixed, merge, after = commits
    assert mixed.files == ["mod.py", "new file.py", "gone.py", "moved.py", "copy.py", "link", "dir b/é.py"]
    # Copies keep their source alive, so only R entries are renames.
    assert mixed.renames == [("old.py", "moved.py"), ("dir a/é.py", "dir b/é.py")]
    assert (merge.files, merge.renames) == ([], [])
    assert after.files == ["x.py"] and after.summary == "after merge"
    assert mixed.authored_at == 1700000000 and mixed.author == "Author"


def test_parser_keeps_separator_characters_inside_a_path() -> None:
    stdout = _record(SHA_A, "odd", "A\0we\x1eird.py") + _record(SHA_B, "next", "M\0n.py")

    commits = [_parse_record(chunk) for chunk in _split_records(stdout)]

    assert [c.files for c in commits] == [["we\x1eird.py"], ["n.py"]]


def test_parser_tolerates_truncated_and_malformed_records() -> None:
    assert _parse_record("not a header") is None
    assert _parse_record(f"{SHA_A}\x1fa\x1fnot-a-time\x1fs\0") is None
    truncated = _parse_record(f"{SHA_A}\x1fa\x1f1\x1fs\nR100\0only-old.py")
    assert truncated is not None and truncated.files == [] and truncated.renames == []


def test_real_git_rename_with_spaces_and_non_ascii(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "old dir/naïve file.py", BODY)
    _write(repo, "keep.py", "k = 1\n")
    commit_all(repo, "add")
    (repo / "new dir").mkdir()
    git(repo, "mv", "old dir/naïve file.py", "new dir/naïve file.py")
    git(repo, "rm", "-q", "keep.py")
    commit_all(repo, "move and delete")

    newest = collect_recent_commits(repo, 5)[0]

    assert sorted(newest.files) == ["keep.py", "new dir/naïve file.py"]
    assert newest.renames == [("old dir/naïve file.py", "new dir/naïve file.py")]


def test_real_git_merge_commit_lists_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "base.py", "b = 1\n")
    commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "mv", "base.py", "moved.py")
    commit_all(repo, "rename on side")
    git(repo, "checkout", "-q", "main")
    _write(repo, "other.py", "o = 1\n")
    commit_all(repo, "main work")
    git(repo, "merge", "--no-ff", "-q", "-m", "merge side", "side")

    commits = collect_recent_commits(repo, 10)

    assert len(commits) == 4
    by_summary = {c.summary: c for c in commits}
    assert by_summary["merge side"].files == []
    assert by_summary["rename on side"].renames == [("base.py", "moved.py")]


# --------------------------------------------------------------------------- #
# Canonical paths
# --------------------------------------------------------------------------- #


def test_rename_chain_maps_every_old_name_to_the_newest() -> None:
    commits = [  # newest first
        _commit("4", ["d.py"]),
        _commit("3", ["d.py"], [("c.py", "d.py")]),
        _commit("2", ["c.py"], [("b.py", "c.py")]),
        _commit("1", ["b.py", "x.py"], [("a.py", "b.py")]),
        _commit("0", ["a.py", "x.py"]),
    ]

    assert canonical_commit_files(commits) == [["d.py"], ["d.py"], ["d.py"], ["d.py", "x.py"], ["d.py", "x.py"]]
    assert canonical_path_map(commits) == {"a.py": "d.py", "b.py": "d.py", "c.py": "d.py"}
    assert compute_churn(commits) == {"d.py": 5, "x.py": 2}


def test_reused_name_does_not_merge_unrelated_histories() -> None:
    """Old ``a.py`` was renamed to ``c.py``; later a brand-new ``a.py`` appeared."""
    commits = [
        _commit("3", ["a.py", "z.py"]),          # the new a.py
        _commit("2", ["c.py"], [("a.py", "c.py")]),
        _commit("1", ["a.py", "y.py"]),          # the old a.py -> today's c.py
    ]

    assert canonical_commit_files(commits) == [["a.py", "z.py"], ["c.py"], ["c.py", "y.py"]]
    churn = compute_churn(commits)
    assert churn == {"a.py": 1, "c.py": 2, "y.py": 1, "z.py": 1}


def test_name_taken_over_by_a_rename_is_not_an_alias_for_older_commits() -> None:
    """``b.py -> a.py`` happened after an older, unrelated ``a.py`` was deleted."""
    commits = [
        _commit("3", ["a.py"], [("b.py", "a.py")]),
        _commit("2", ["b.py"]),
        _commit("1", ["a.py"]),  # the old a.py: stays a.py (path identity)
    ]

    assert canonical_commit_files(commits) == [["a.py"], ["a.py"], ["a.py"]]
    assert canonical_path_map(commits) == {"b.py": "a.py"}


def test_rename_cycle_and_swap_are_deterministic() -> None:
    cycle = [
        _commit("2", ["a.py"], [("b.py", "a.py")]),
        _commit("1", ["b.py"], [("a.py", "b.py")]),
        _commit("0", ["a.py"]),
    ]
    assert canonical_commit_files(cycle) == [["a.py"], ["a.py"], ["a.py"]]
    assert canonical_path_map(cycle) == {"b.py": "a.py"}

    swap = [  # one commit swaps two files' names
        _commit("1", ["a.py", "b.py"], [("a.py", "b.py"), ("b.py", "a.py")]),
        _commit("0", ["a.py", "b.py", "x.py"]),
    ]
    first = canonical_commit_files(swap)
    assert first == canonical_commit_files(swap)
    assert first[1] == ["b.py", "a.py", "x.py"]


def test_rename_inside_one_commit_with_both_names_dedupes() -> None:
    commits = [
        _commit("1", ["new.py"], [("old.py", "new.py")]),
        _commit("0", ["old.py", "new.py", "./old.py", "other.py"]),
    ]

    assert canonical_commit_files(commits)[1] == ["new.py", "other.py"]
    edges = build_cochange_edges(commits)
    assert [e.edge_id for e in edges] == ["cochange:new.py:other.py"]


def test_mapping_commits_and_records_without_renames_are_accepted() -> None:
    commits = [
        {"files": ["n.py"], "renames": [["o.py", "n.py"]]},
        {"files": ["o.py"]},
        _commit("legacy", ["o.py"]),
    ]

    assert canonical_commit_files(commits) == [["n.py"], ["n.py"], ["n.py"]]


# --------------------------------------------------------------------------- #
# Risk: co-change lookup through previous_path
# --------------------------------------------------------------------------- #


def test_risk_uses_previous_path_cochange_for_a_file_renamed_in_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "a.py", BODY)
    _write(repo, "b.py", BODY + "b\n")
    commit_all(repo, "add a and b")
    _write(repo, "a.py", BODY + "a2\n")
    _write(repo, "b.py", BODY + "b2\n")
    commit_all(repo, "change a and b")
    ingest_repository(repo, commit_limit=10)
    nodes, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    git(repo, "mv", "a.py", "c.py")

    result = analyze_risk(repo, staged=True, nodes=nodes, edges=edges)

    renamed = next(item for item in result["files"] if item["path"] == "c.py")
    assert renamed["previous_path"] == "a.py"
    assert {"path": "b.py", "weight": 1.0, "for": "c.py"} in result["missing_cochange"]
