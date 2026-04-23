# tests/test_cochange.py
from __future__ import annotations
from cortex.models import CommitRecord
from cortex.cochange import build_cochange_edges


def _commit(sha: str, files: list[str]) -> CommitRecord:
    return CommitRecord(sha=sha, summary='msg', author='a', authored_at=0, files=files)


def test_files_that_always_change_together_get_high_weight():
    commits = [
        _commit('a1', ['auth.py', 'session.py']),
        _commit('a2', ['auth.py', 'session.py']),
        _commit('a3', ['auth.py', 'session.py']),
    ]
    edges = build_cochange_edges(commits)
    assert len(edges) == 1
    assert edges[0].weight == 1.0
    assert edges[0].relation == 'cochange'


def test_weight_is_proportional_to_frequency():
    commits = [
        _commit('a1', ['a.py', 'b.py']),
        _commit('a2', ['a.py', 'b.py']),
        _commit('a3', ['a.py', 'c.py']),
        _commit('a4', ['a.py', 'c.py']),
        _commit('a5', ['a.py', 'c.py']),
    ]
    edges = build_cochange_edges(commits)
    ab = next((e for e in edges if 'b.py' in e.source or 'b.py' in e.target), None)
    ac = next((e for e in edges if 'c.py' in e.source or 'c.py' in e.target), None)
    assert ab is not None and ac is not None
    assert ab.weight < ac.weight


def test_single_file_commits_produce_no_edges():
    commits = [_commit('a1', ['solo.py']), _commit('a2', ['solo.py'])]
    edges = build_cochange_edges(commits)
    assert edges == []


def test_all_cochange_edges_have_correct_layer():
    commits = [_commit('a1', ['x.py', 'y.py'])]
    edges = build_cochange_edges(commits)
    for edge in edges:
        assert edge.layer == 'COCHANGE'
        assert edge.confidence == 'EXTRACTED'


def test_empty_commits_returns_empty():
    assert build_cochange_edges([]) == []
