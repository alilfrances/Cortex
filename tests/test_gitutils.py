"""Characterization of ``cortex.gitutils.collect_recent_commits``.

Task 0 pinned 7b0e6e0 (``git log --name-only``, records split on blank lines).
Task 4 (0.10.0, rename-aware history) replaced the parser with
``--name-status -M -z`` and a record separator, and deliberately flipped the
assertions marked ``TASK 4``.  Defects observed at 7b0e6e0 and their fate:

* A commit with **no file list** (a ``--no-ff`` merge, or ``--allow-empty``) was not
  followed by the blank line the old parser split on, so the *next older* commit's
  header was glued into that commit's ``files`` and the older commit was dropped.
  FIXED in Task 4: every commit is its own record; file-less commits have
  ``files == []``.
* Non-ASCII paths came back git-quoted (``"h\\303\\251llo.py"``) because the parser did
  not pass ``-z``.  FIXED in Task 4: paths are verbatim (``"héllo.py"``).
* ``git mv`` surfaced only the *new* path (``--name-only`` with rename detection).
  Still true for ``files``; Task 4 adds ``renames == [("a.py", "c.py")]`` and makes
  co-change and churn follow the file across the rename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cortex.cochange import build_cochange_edges
from cortex.gitutils import collect_recent_commits, detect_repo_root, discover_repo_root
from cortex.hotspots import compute_churn
from tests.baseline_helpers import commit_all, fixed_git_env, git, init_git


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "repo") -> Path:
    fixed_git_env(monkeypatch)
    repo = tmp_path / name
    init_git(repo)
    # Make the observed behaviour independent of a developer's global git config.
    git(repo, "config", "core.quotepath", "true")
    git(repo, "config", "diff.renames", "true")
    return repo


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ordinary_commit_with_three_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    for name in ("one.py", "two.py", "three.py"):
        _write(repo, name, f"{name} = 1\n")
    commit_all(repo, "add three files")

    commits = collect_recent_commits(repo, 10)

    assert len(commits) == 1
    commit = commits[0]
    assert commit.sha == git(repo, "rev-parse", "HEAD").strip()
    assert commit.summary == "add three files"
    assert commit.author == "Baseline"
    assert isinstance(commit.authored_at, int) and commit.authored_at > 0
    assert commit.files == ["one.py", "three.py", "two.py"]  # git emits paths sorted
    assert commit.to_dict()["files"] == commit.files


def test_newest_first_and_limit_returns_newest_n(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    for index in range(4):
        _write(repo, f"f{index}.py", f"x = {index}\n")
        commit_all(repo, f"commit {index}")

    everything = collect_recent_commits(repo, 100)
    assert [c.summary for c in everything] == ["commit 3", "commit 2", "commit 1", "commit 0"]
    assert [c.files for c in everything] == [["f3.py"], ["f2.py"], ["f1.py"], ["f0.py"]]

    newest_two = collect_recent_commits(repo, 2)
    assert [c.summary for c in newest_two] == ["commit 3", "commit 2"]
    assert collect_recent_commits(repo, 1)[0].sha == git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_non_positive_limit_returns_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "a.py", "a = 1\n")
    commit_all(repo, "only")

    assert collect_recent_commits(repo, limit) == []


def test_non_repository_and_empty_repository_return_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert collect_recent_commits(plain, 5) == []

    empty = _repo(tmp_path, monkeypatch, "empty")
    assert collect_recent_commits(empty, 5) == []


def test_repo_root_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    nested = repo / "pkg" / "deep"
    nested.mkdir(parents=True)

    assert discover_repo_root(nested) == repo.resolve()
    assert detect_repo_root(str(nested)) == repo.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="not inside a git repository"):
        discover_repo_root(plain)


def test_path_with_spaces_is_returned_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "sp ace.py", "x = 1\n")
    _write(repo, "dir with space/inner file.py", "y = 1\n")
    commit_all(repo, "spaces")

    (commit,) = collect_recent_commits(repo, 5)

    assert commit.files == ["dir with space/inner file.py", "sp ace.py"]


def test_non_ascii_path_is_returned_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 4 FLIP: at 7b0e6e0 this came back as ``'"h\\303\\251llo.py"'`` (git C-quoting,
    never matching the file on disk).  ``-z`` output is verbatim even with
    ``core.quotepath=true``."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "héllo.py", "x = 1\n")
    _write(repo, "plain.py", "y = 1\n")
    commit_all(repo, "non-ascii name")

    (commit,) = collect_recent_commits(repo, 5)

    assert commit.files == ["héllo.py", "plain.py"]
    assert all((repo / path).is_file() for path in commit.files)


def _merge_history(repo: Path) -> None:
    _write(repo, "base.py", "base = 1\n")
    commit_all(repo, "c1 base")
    git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "feature.py", "f = 1\n")
    commit_all(repo, "feature work")
    git(repo, "checkout", "-q", "main")
    _write(repo, "mainline.py", "m = 1\n")
    commit_all(repo, "mainline work")
    git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")


def test_merge_commit_alone_has_empty_file_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path, monkeypatch)
    _merge_history(repo)

    (merge,) = collect_recent_commits(repo, 1)

    assert merge.summary == "merge feature"
    assert merge.files == []


def test_merge_commit_is_its_own_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 4 FLIP: at 7b0e6e0 the commit after a file-less merge was lost -- its raw
    header (``sha\\x1fauthor\\x1ftime\\x1fsummary``) landed in ``merge.files`` and it
    never appeared as its own record (3 of 4 commits returned)."""
    repo = _repo(tmp_path, monkeypatch)
    _merge_history(repo)
    log_order = git(repo, "log", "--format=%H").split()
    assert len(log_order) == 4  # merge, mainline, feature, base

    commits = collect_recent_commits(repo, 10)

    assert [c.sha for c in commits] == log_order
    by_summary = {c.summary: c for c in commits}
    assert by_summary["merge feature"].files == []
    assert by_summary["mainline work"].files == ["mainline.py"]
    assert by_summary["feature work"].files == ["feature.py"]
    assert by_summary["c1 base"].files == ["base.py"]
    assert all("\x1f" not in f and "\x1e" not in f for c in commits for f in c.files)
    assert all(c.renames == [] for c in commits)


def test_empty_commit_is_its_own_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 4 FLIP: at 7b0e6e0 ``--allow-empty`` swallowed the next commit too
    (only ``["empty marker"]`` came back, carrying the older header in ``files``)."""
    repo = _repo(tmp_path, monkeypatch)
    _write(repo, "a.py", "a = 1\n")
    commit_all(repo, "real change")
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty marker")
    _write(repo, "b.py", "b = 1\n")
    commit_all(repo, "after empty")

    commits = collect_recent_commits(repo, 10)

    assert [(c.summary, c.files) for c in commits] == [
        ("after empty", ["b.py"]),
        ("empty marker", []),
        ("real change", ["a.py"]),
    ]


def _rename_history(repo: Path) -> None:
    body = "".join(f"line {i}\n" for i in range(12))
    _write(repo, "a.py", body)
    _write(repo, "b.py", body + "b\n")
    commit_all(repo, "add a and b")
    _write(repo, "a.py", body + "a2\n")
    _write(repo, "b.py", body + "b2\n")
    commit_all(repo, "change a and b")
    git(repo, "mv", "a.py", "c.py")
    commit_all(repo, "rename a to c")
    _write(repo, "c.py", body + "c3\n")
    _write(repo, "b.py", body + "b3\n")
    commit_all(repo, "change c and b")


def test_rename_commit_lists_new_path_and_records_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 4 FLIP: at 7b0e6e0 ``CommitRecord`` had no ``renames``; ``files`` is unchanged."""
    repo = _repo(tmp_path, monkeypatch)
    _rename_history(repo)

    commits = collect_recent_commits(repo, 10)
    by_summary = {c.summary: c for c in commits}

    assert by_summary["rename a to c"].files == ["c.py"]
    assert by_summary["rename a to c"].renames == [("a.py", "c.py")]
    assert by_summary["add a and b"].files == ["a.py", "b.py"]
    assert by_summary["change c and b"].files == ["b.py", "c.py"]
    assert all(c.renames == [] for c in commits if c.summary != "rename a to c")


def test_rename_follows_cochange_and_churn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TASK 4 FLIP: at 7b0e6e0 ``a.py`` and ``c.py`` were unrelated paths --
    edges ``cochange:a.py:b.py`` (count 2) and ``cochange:b.py:c.py`` (count 1), churn
    ``{"a.py": 2, "b.py": 3, "c.py": 2}``.  History now follows the file to ``c.py``."""
    repo = _repo(tmp_path, monkeypatch)
    _rename_history(repo)
    commits = collect_recent_commits(repo, 10)

    edges = {edge.edge_id: edge for edge in build_cochange_edges(commits)}

    assert sorted(edges) == ["cochange:b.py:c.py"]
    assert edges["cochange:b.py:c.py"].metadata == {"count": 3, "max_count": 3}
    assert edges["cochange:b.py:c.py"].weight == 1.0

    assert compute_churn(commits) == {"b.py": 3, "c.py": 4}


def test_cochange_and_churn_on_hand_built_commits() -> None:
    """Hand-built inputs (git never emits these shapes) pin the counting rules.

    TASK 4 FLIP: at 7b0e6e0 ``build_cochange_edges`` neither de-duplicated paths within
    a commit (a repeated path yielded a ``x.py:x.py`` self-pair) nor normalised ``./``
    (edge ``cochange:./z.py:x.py``), while ``compute_churn`` did both.  Both now share
    ``gitutils.canonical_commit_files``, so they count the same canonical paths.
    """
    from cortex.cochange import MAX_COMMIT_FILES
    from cortex.models import CommitRecord

    def commit(sha: str, files: list[str]) -> CommitRecord:
        return CommitRecord(sha=sha, summary=sha, author="t", authored_at=1, files=files)

    commits = [
        commit("1", ["x.py", "y.py"]),
        commit("2", ["x.py", "y.py", "x.py"]),
        commit("3", [f"bulk{i}.py" for i in range(MAX_COMMIT_FILES + 1)]),  # too large: no pairs
        commit("4", ["./z.py", "", "x.py"]),
    ]

    churn = compute_churn(commits)
    assert {k: v for k, v in churn.items() if not k.startswith("bulk")} == {"x.py": 3, "y.py": 2, "z.py": 1}
    assert sum(1 for k in churn if k.startswith("bulk")) == MAX_COMMIT_FILES + 1

    edges = {edge.edge_id: edge for edge in build_cochange_edges(commits)}
    assert sorted(edges) == ["cochange:x.py:y.py", "cochange:x.py:z.py"]
    assert edges["cochange:x.py:z.py"].metadata == {"count": 1, "max_count": 2}
    # 7b0e6e0: count 3 (commit "2" paired x.py with y.py twice through the repeat).
    assert edges["cochange:x.py:y.py"].metadata == {"count": 2, "max_count": 2}
    assert edges["cochange:x.py:y.py"].weight == 1.0
    assert edges["cochange:x.py:y.py"].layer == "COCHANGE"
    assert edges["cochange:x.py:y.py"].source == "file:x.py"
    assert edges["cochange:x.py:y.py"].target == "file:y.py"
    assert build_cochange_edges([]) == []
    assert build_cochange_edges([commit("solo", ["only.py"])]) == []
