"""Perf/regression coverage for the P1-1..P1-3 dead-code scan fixes.

`analyze_dead_code` used to be O(candidates x corpus): `_qt_macro_flags`
re-scanned every source file with two label-interpolated regexes for every
single "class" candidate (cause 1a), `_reference_tier` walked and re-read the
whole repo per candidate via `find_references` (cause 1b), and both
`analyze_dead_code` and `_candidate_reason` independently re-fetched the same
file's content per symbol (cause 1c). See
docs/fix-plan-mcp-concurrency-wedge.md.

The fix reads the repo once into a shared `ReferenceCorpus`, builds one
in-memory `GraphReferenceIndex`, and keeps a shared path -> content map, all
per `analyze_dead_code` run. These tests pin that shape down (built once, not
once per candidate) and the exact cross-file / paren-form matching semantics
the old per-label regex scan implemented.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True, capture_output=True)


def _store(repo: Path) -> CortexStore:
    store = CortexStore(repo / ".cortex" / "cortex.db")
    store.reset_repo(repo)
    return store


def _write_source(repo: Path, rel: str, content: str) -> SourceRecord:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return SourceRecord(path=rel, content=content, kind="code", size_bytes=len(content), modified_at=0.0)


def _build_corpus(repo: Path, filler_files: int, class_candidates: int) -> tuple[CortexStore, list[GraphNode]]:
    """`filler_files` padding files (never candidates themselves) plus
    `class_candidates` unused, Qt/QML-marker-free classes -- the exact shape
    that used to trigger the per-candidate full-corpus regex scan in
    `_qt_macro_flags`. They are tree-sitter-backed so every candidate reaches
    the reference lookup."""
    repo.mkdir(parents=True, exist_ok=True)
    filler_body = "// filler\n" + ("static const int kValue = 1;  // padding line\n" * 150)

    sources: list[SourceRecord] = []
    nodes: list[GraphNode] = []
    for i in range(filler_files):
        sources.append(_write_source(repo, f"filler_{i:04d}.cpp", filler_body))
    for i in range(class_candidates):
        label = f"Candidate_{i:04d}"
        rel = f"{label}.hpp"
        content = f"class {label} {{\npublic:\n    void method();\n}};\n"
        sources.append(_write_source(repo, rel, content))
        nodes.append(
            GraphNode(
                node_id=f"symbol:{rel}:{label}",
                kind="class",
                label=label,
                source_ref=rel,
                granularity="symbol",
                span_start=1,
                span_end=4,
                metadata={"parser_backend": "treesitter"},
            )
        )

    _git_init(repo)
    store = _store(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])
    return store, nodes


def test_corpus_and_graph_index_are_built_once_per_run_not_per_candidate(tmp_path, monkeypatch) -> None:
    """Regression guard for the O(candidates x corpus) scan: count, rather
    than time, how many times the expensive corpus-wide work runs.
    Counting operations avoids timing flakiness while giving a hard
    guarantee that none of it is re-done per candidate."""
    import cortex.deadcode as deadcode
    import cortex.references as references

    repo = tmp_path / "repo"
    store, nodes = _build_corpus(repo, filler_files=60, class_candidates=40)

    counts = {"corpus": 0, "graph_index": 0, "repo_listings": 0, "find_references": 0}

    class CountedCorpus(references.ReferenceCorpus):
        def __init__(self, *args, **kwargs):
            counts["corpus"] += 1
            super().__init__(*args, **kwargs)

    class CountedGraphIndex(references.GraphReferenceIndex):
        def __init__(self, *args, **kwargs):
            counts["graph_index"] += 1
            super().__init__(*args, **kwargs)

    real_listing = references._iter_candidate_files
    real_find_references = deadcode.find_references

    def counted_listing(repo_root):
        counts["repo_listings"] += 1
        return real_listing(repo_root)

    def counted_find_references(*args, **kwargs):
        counts["find_references"] += 1
        return real_find_references(*args, **kwargs)

    monkeypatch.setattr(deadcode, "ReferenceCorpus", CountedCorpus)
    monkeypatch.setattr(deadcode, "GraphReferenceIndex", CountedGraphIndex)
    monkeypatch.setattr(references, "_iter_candidate_files", counted_listing)
    monkeypatch.setattr(deadcode, "find_references", counted_find_references)

    result = analyze_dead_code(repo, store=store, nodes=nodes, edges=[])

    assert len(result["findings"]) == 40
    assert counts["find_references"] == 40, "every candidate must reach the reference lookup"
    assert counts["corpus"] == 1, "reference corpus must be built once per run, not once per candidate"
    assert counts["repo_listings"] == 1, "the repo must be listed and read once per run"
    assert counts["graph_index"] == 1, "graph index must be built once per run, not once per candidate"


def test_wall_clock_does_not_scale_with_candidate_count(tmp_path) -> None:
    """Secondary, generously-bounded sanity check on top of the operation
    count guard above: quadrupling the candidate count against the same
    corpus must not come close to quadrupling wall time, and must stay well
    under a fixed ceiling either way."""
    small_repo = tmp_path / "small"
    small_store, small_nodes = _build_corpus(small_repo, filler_files=60, class_candidates=15)
    large_repo = tmp_path / "large"
    large_store, large_nodes = _build_corpus(large_repo, filler_files=60, class_candidates=60)

    # Warm up any one-time import / regex-compilation costs before timing.
    analyze_dead_code(small_repo, store=small_store, nodes=small_nodes, edges=[])
    analyze_dead_code(large_repo, store=large_store, nodes=large_nodes, edges=[])

    start = time.perf_counter()
    analyze_dead_code(small_repo, store=small_store, nodes=small_nodes, edges=[])
    small_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    result = analyze_dead_code(large_repo, store=large_store, nodes=large_nodes, edges=[])
    large_elapsed = time.perf_counter() - start

    assert len(result["findings"]) == 60
    # Generous ceiling: this must never be anywhere near the ~600ms/candidate
    # (~36s for 60 candidates) the old O(candidates x corpus) scan measured.
    assert large_elapsed < 5.0, large_elapsed
    # Generous ratio bound: 4x the candidates should not come close to 4x
    # the time, since per-candidate cost is now O(1) against a corpus built
    # once. Floor small_elapsed to absorb timer noise on a near-instant run.
    assert large_elapsed < max(small_elapsed, 0.01) * 6, (small_elapsed, large_elapsed)


def test_cross_file_qml_register_type_template_form_credits_class_as_low(tmp_path) -> None:
    """The scan this whole fix exists for: `class Foo` defined in one file,
    `qmlRegisterType<Foo>(...)` in a *different* file. `Foo` must be
    classified `low` confidence with the Qt/QML caveat -- exactly as the old
    per-candidate full-corpus scan did, but via the corpus-wide index."""
    repo = tmp_path / "repo"
    repo.mkdir()
    foo_source = _write_source(repo, "foo.cpp", "class Foo {\npublic:\n    void bar();\n};\n")
    _write_source(
        repo,
        "main.cpp",
        'void registerTypes() {\n'
        '    qmlRegisterType<Foo>("App", 1, 0, "Foo");\n'
        '}\n',
    )
    _git_init(repo)
    store = _store(repo)
    all_sources = [
        foo_source,
        SourceRecord(path="main.cpp", content=(repo / "main.cpp").read_text(encoding="utf-8"), kind="code", size_bytes=0, modified_at=0.0),
    ]
    store.save_sources(repo, all_sources)
    node = GraphNode(
        node_id="symbol:foo.cpp:Foo",
        kind="class",
        label="Foo",
        source_ref="foo.cpp",
        granularity="symbol",
        span_start=1,
        span_end=4,
        metadata={"parser_backend": "regex"},
    )
    store.save_graph(repo, [node], [])

    result = analyze_dead_code(repo, store=store, nodes=[node], edges=[])
    findings = {item["symbol"]: item for item in result["findings"]}

    assert findings["Foo"]["confidence"] == "low"
    assert findings["Foo"]["reason"] == "no incoming edges; Qt meta-object or dynamic-language caveat"


def test_paren_form_qml_register_type_credits_class_anywhere_in_the_call(tmp_path) -> None:
    """`qmlRegisterType(QUrl("qrc:/Bar.qml"), "App", 1, 0, "Bar");` must
    credit `Bar` -- the paren form matches the label anywhere inside the
    call's parens (up to the first `)`), not just in a type-argument
    position, mirroring the original `[^)]*\\b{label}\\b` semantics."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bar_source = _write_source(repo, "bar.cpp", "class Bar {\npublic:\n    void baz();\n};\n")
    _write_source(
        repo,
        "register.cpp",
        'void registerTypes() {\n'
        '    qmlRegisterType(QUrl("qrc:/Bar.qml"), "App", 1, 0, "Bar");\n'
        '}\n',
    )
    _git_init(repo)
    store = _store(repo)
    all_sources = [
        bar_source,
        SourceRecord(path="register.cpp", content=(repo / "register.cpp").read_text(encoding="utf-8"), kind="code", size_bytes=0, modified_at=0.0),
    ]
    store.save_sources(repo, all_sources)
    node = GraphNode(
        node_id="symbol:bar.cpp:Bar",
        kind="class",
        label="Bar",
        source_ref="bar.cpp",
        granularity="symbol",
        span_start=1,
        span_end=4,
        metadata={"parser_backend": "regex"},
    )
    store.save_graph(repo, [node], [])

    result = analyze_dead_code(repo, store=store, nodes=[node], edges=[])
    findings = {item["symbol"]: item for item in result["findings"]}

    assert findings["Bar"]["confidence"] == "low"
    assert findings["Bar"]["reason"] == "no incoming edges; Qt meta-object or dynamic-language caveat"
