from __future__ import annotations

from pathlib import Path

from cortex.models import GraphEdge, GraphNode
from cortex.references import GraphReferenceIndex, ReferenceCorpus, _graph_hits, find_references
from cortex.store import CortexStore


def _references_repo(tmp_path: Path) -> tuple[Path, CortexStore]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "sample.py").write_text(
        "def x(value):\n"
        "    return value\n"
        "read = x\n"
        "x = 1\n"
        "x += 2\n"
        "x.append(3)\n"
        "if x == value:\n"
        "    return x\n",
        encoding="utf-8",
    )
    store = CortexStore(repo / ".cortex" / "cortex.db")
    store.reset_repo(repo)
    store.save_graph(
        repo,
        [
            GraphNode("file:sample.py", "file", "sample.py", "sample.py"),
            GraphNode(
                "symbol:sample.py:x",
                "function",
                "x",
                "sample.py",
                granularity="symbol",
                span_start=1,
            ),
        ],
        [GraphEdge("contains", "file:sample.py", "symbol:sample.py:x", "contains")],
    )
    return repo, store


def test_find_references_tags_reads_writes_and_definitions(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    result = find_references(store, repo, "x")
    hits = {entry["text"]: entry for entries in result["items"].values() for entry in entries}

    assert hits["sample.py:1"]["access"] == "definition"
    assert hits["sample.py:3"]["access"] == "read"
    assert hits["sample.py:4"]["access"] == "write"
    assert hits["sample.py:5"]["access"] == "write"
    assert hits["sample.py:6"]["access"] == "write"
    assert hits["sample.py:7"]["access"] == "read"
    assert hits["sample.py:8"]["access"] == "read"


def test_find_references_writes_mode_keeps_definitions_and_writes(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    result = find_references(store, repo, "x", mode="writes")
    texts = {entry["text"] for entries in result["items"].values() for entry in entries}

    assert texts == {"sample.py:1", "sample.py:4", "sample.py:5", "sample.py:6"}


def test_comparison_operators_are_reads(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "operators.py").write_text(
        "x == y\n"
        "x <= y\n"
        "x >= y\n"
        "x != y\n"
        "x => y\n"
        "x[0] == y\n",
        encoding="utf-8",
    )

    result = find_references(store, repo, "x")
    hits = {entry["text"]: entry for entries in result["items"].values() for entry in entries}

    assert all(hits[f"operators.py:{line}"]["access"] == "read" for line in range(1, 7))


def test_reference_corpus_matches_uncorpused_results(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "words.py").write_text("xs = [1]\nmax_value = max(xs)\n", encoding="utf-8")
    (repo / "notes.md").write_text("call a.b here\nns::fn() and ns::fn\nx appears in docs\n", encoding="utf-8")
    (repo / "extra.py").write_text("print(x)\nvalue.a.b = 1\nns::fn\n", encoding="utf-8")
    corpus = ReferenceCorpus(repo)

    for symbol in ("x", "value", "xs", "a.b", "ns::fn", "missing_symbol", ""):
        assert find_references(store, repo, symbol, corpus=corpus) == find_references(store, repo, symbol), symbol
        assert find_references(store, repo, symbol, mode="writes", corpus=corpus) == find_references(
            store, repo, symbol, mode="writes"
        ), symbol


def _graph_store(tmp_path: Path) -> tuple[Path, CortexStore, list[GraphNode], list[GraphEdge]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    labels = ["axb", "a_b", "Foo_Bar", "FOO_BAR", "caf\u00e9", "CAF\u00c9", "100%done", "100xdone", "q", "zz", "Hub"]
    nodes = [GraphNode("file:f.py", "file", "f.py", "f.py")]
    nodes += [
        GraphNode(f"symbol:f.py:{label}", "function", label, "f.py", granularity="symbol", span_start=i + 1)
        for i, label in enumerate(labels)
    ]
    hub = "symbol:f.py:Hub"
    nodes += [
        GraphNode(f"symbol:f.py:n{i:03d}", "function", f"caller{i}", "f.py", granularity="symbol", span_start=100 + i)
        for i in range(260)
    ]
    edges = [GraphEdge(f"e{i:04d}", f"symbol:f.py:n{i % 260:03d}", hub, "calls") for i in range(260)]
    edges += [
        GraphEdge(f"c{i:04d}", "file:f.py", f"symbol:f.py:{label}", "references")
        for i, label in enumerate(labels)
    ]
    edges.append(GraphEdge("dangling", "file:f.py", "symbol:missing:Ghost_Target", "calls"))
    store = CortexStore(repo / ".cortex" / "cortex.db")
    store.reset_repo(repo)
    store.save_graph(repo, nodes, edges)
    return repo, store, nodes, list(reversed(edges))


def test_graph_reference_index_matches_sql_like_semantics(tmp_path: Path) -> None:
    repo, store, nodes, edges = _graph_store(tmp_path)
    index = GraphReferenceIndex(nodes, edges)
    symbols = [
        "a_b", "foo_bar", "FOO", "caf\u00e9", "CAF\u00c9", "100%done", "%", "_", "q", "zz", "ub",
        "", "caller", "symbol:f.py", "Ghost", "ghost_target", "nothing_matches_this", "ab%", "x_",
    ]

    for symbol in symbols:
        assert index.hits(symbol) == _graph_hits(store, repo, symbol), symbol


def test_corpus_whole_text_scan_handles_unicode_line_separators(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "mixed.py").write_text(
        "x = 1\r\nprint(x)\x0cx += 1 and x\u2028value x x\x85x.append(2)\x1cno_x_here\n  x[0] = 3",
        encoding="utf-8",
    )
    corpus = ReferenceCorpus(repo)

    for symbol in ("x", "value"):
        assert find_references(store, repo, symbol, corpus=corpus) == find_references(store, repo, symbol), symbol


def test_corpus_symbols_of_interest_falls_back_for_other_identifiers(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "other.py").write_text("value = x\nunlisted = value\n", encoding="utf-8")
    corpus = ReferenceCorpus(repo, {"x"})

    for symbol in ("x", "value", "unlisted", "nothing"):
        assert find_references(store, repo, symbol, corpus=corpus) == find_references(store, repo, symbol), symbol


def test_corpus_does_not_retain_files_over_the_index_size_limit(tmp_path: Path, monkeypatch) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "other.py").write_text("value = x\nunlisted = value\nx += 1\n", encoding="utf-8")
    monkeypatch.setattr("cortex.references.MAX_SOURCE_BYTES", 8)

    for symbols in (None, {"x"}):
        corpus = ReferenceCorpus(repo, symbols)
        assert corpus._files and not any(item.retained for item in corpus._files)
        for symbol in ("x", "value", "a.b", "nothing"):
            assert find_references(store, repo, symbol, corpus=corpus) == find_references(store, repo, symbol), symbol
        assert not any(item.retained or item._lines for item in corpus._files)
