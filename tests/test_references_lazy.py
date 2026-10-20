from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from cortex import references
from cortex.ingest import ingest_repository
from cortex.references import ReferenceCorpus, GraphReferenceIndex, find_references
from cortex.store import CortexStore, default_db_path
from cortex.tokenizer import count_text_tokens


def _old_find_references(store, repo_root, symbol, budget, mode="all", corpus=None, graph_index=None):
    """The pre-lazy implementation, kept as the oracle."""
    if graph_index is not None:
        graph_hits, covered = graph_index.hits(symbol)
    else:
        graph_hits, covered = references._graph_hits(store, repo_root, symbol)
    grep_hits = references._grep_hits(repo_root, symbol, covered, corpus)
    eligible = [
        hit for hit in (*graph_hits, *grep_hits)
        if not (mode == "writes" and hit["access"] not in {"definition", "write"})
    ]
    lo, hi, best = 0, len(eligible), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0 or count_text_tokens(str(references._items_with_prefix(eligible, mid))) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return {
        "items": references._items_with_prefix(eligible, best),
        "truncated": best < len(eligible),
        "returned_count": best,
    }


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    # `shared` appears many times across files; `rare` once; `ghost` never.
    for index in range(40):
        lines = ["def f%d(x):" % index]
        lines += [f"    shared = shared + {n}  # use {index}.{n}" for n in range(6)]
        lines += ["    return shared"]
        (root / "src" / f"m{index}.py").write_text("\n".join(lines) + "\n")
    (root / "src" / "rare_user.py").write_text("def g():\n    return rare()\n\n\ndef rare():\n    return 1\n")
    (root / "docs" / "notes.md").write_text("The `shared` value and rare helper are described here.\n" * 5)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


@pytest.mark.parametrize("symbol", ["shared", "rare", "ghost", "f7", "x"])
@pytest.mark.parametrize("budget", [60, 300, 2000, 20000])
@pytest.mark.parametrize("mode", ["all", "writes"])
@pytest.mark.parametrize("with_corpus", [False, True])
def test_lazy_find_references_equals_the_full_scan(repo, symbol, budget, mode, with_corpus):
    store = CortexStore(default_db_path(repo))
    corpus = graph_index = None
    if with_corpus:
        nodes, edges = store.fetch_graph(repo)
        corpus = ReferenceCorpus(repo, {n.label for n in nodes if n.granularity == "symbol"} | {symbol})
        graph_index = GraphReferenceIndex(nodes, edges)
    expected = _old_find_references(store, repo, symbol, budget, mode, corpus, graph_index)
    actual = find_references(store, repo, symbol, budget, mode, corpus, graph_index)
    assert actual == expected


def test_lazy_scan_stops_reading_after_the_budget_is_spent(repo, monkeypatch):
    store = CortexStore(default_db_path(repo))
    scanned: list[str] = []
    real = references._scan_text

    def counting(pattern, symbol, covered, item):
        scanned.append(item.rel_path)
        yield from real(pattern, symbol, covered, item)

    monkeypatch.setattr(references, "_scan_text", counting)
    nodes, edges = store.fetch_graph(repo)
    corpus = ReferenceCorpus(repo, {"shared"})
    result = find_references(store, repo, "shared", 60, corpus=corpus, graph_index=GraphReferenceIndex(nodes, edges))
    assert result["truncated"] is True
    assert len(scanned) < 40  # a small budget does not scan every file that mentions the symbol


def _old_line_access(symbol: str, line: str) -> str:
    symbol_pattern = r"\b" + re.escape(symbol) + r"\b"
    if re.search(symbol_pattern + r"\s*(?:<<=|>>=|\+=|-=|\*=|/=|\|=|&=|\^=)", line):
        return "write"
    if re.search(r"(?:\+\+|--)\s*" + symbol_pattern, line) or re.search(symbol_pattern + r"\s*(?:\+\+|--)", line):
        return "write"
    if re.search(symbol_pattern + r"\s*(?:\.\s*(?:" + "|".join(references._MUTATING_METHODS) + r")\s*\(|\[[^\]]*\]\s*=(?!=|>))", line):
        return "write"
    if re.search(r"\bdel\s+" + symbol_pattern, line):
        return "write"
    if re.search(symbol_pattern + r"\s*=(?!=|>)", line):
        return "write"
    return "read"


@pytest.mark.parametrize(
    "line",
    [
        "x = 1", "x == 1", "x += 2", "x <<= 1", "++x", "x--", "del x", "x.append(1)", "x[0] = 2", "x[0] == 2",
        "y = x", "foo(x)", "x => 1", "x.get(1)", "# x", "print(x, y)", "self.x = 3", "xx = 1", "x . pop (0)",
    ],
)
def test_precompiled_access_patterns_match_the_original(line):
    assert references._line_access("x", line) == _old_line_access("x", line)
