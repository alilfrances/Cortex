from __future__ import annotations

from evals.run_evals import (
    GoldTask,
    _format_markdown,
    _precision_recall,
    _symbol_hit,
    run_hook_adoption_replay,
)


def test_precision_recall_scores_expected_file_overlap():
    precision, recall = _precision_recall({"a.py", "b.py"}, {"b.py", "c.py"})

    assert precision == 0.5
    assert recall == 0.5


def test_symbol_hit_accepts_qualname_leaf_in_matching_file():
    items = [
        {
            "path": "app/service.py",
            "content": "class AuthService:\n    def login(self):\n        pass\n",
        }
    ]

    assert _symbol_hit(items, "app/service.py:AuthService.login")
    assert not _symbol_hit(items, "app/service.py:AuthService.logout")


def test_hook_adoption_replay_has_indexed_positives_and_unindexed_negatives(tmp_path):
    result = run_hook_adoption_replay(tmp_path)

    assert result["positives"] == 4
    assert result["false_positives"] == 0
    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["warm_under_50ms"] is True


def test_format_markdown_contains_aggregate_table():
    task = GoldTask(
        repo="python_app",
        description="Find auth login flow",
        expected_files=("app/auth.py",),
        expected_symbols=("app/auth.py:AuthService.login",),
        budget=800,
    )
    rows = [
        {
            "task": task,
            "mode": "pagerank",
            "precision": 1.0,
            "precision_at_3": 1 / 3,
            "recall": 1.0,
            "file_precision": 1.0,
            "file_recall": 1.0,
            "symbol_recall": 1.0,
            "tokens": 120,
            "latency_ms": 3.4,
            "files": ["app/auth.py"],
        }
    ]

    markdown = _format_markdown(rows)

    assert "| Mode | Tasks | Precision | Precision@3 | Recall | Avg Tokens | Avg Latency ms |" in markdown
    assert "| pagerank | 1 | 1.000 | 0.333 | 1.000 | 120 | 3.4 |" in markdown
