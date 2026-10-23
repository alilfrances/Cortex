from __future__ import annotations

import subprocess
from pathlib import Path

from cortex import bundle as bundle_mod
from cortex.bundle import generate_bundle
from cortex.models import GraphNode, SourceRecord
from cortex.store import CortexStore


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    return repo


def _source(path: str, content: str, kind: str = "code") -> SourceRecord:
    return SourceRecord(
        path=path,
        content=content,
        kind=kind,
        size_bytes=len(content),
        modified_at=0.0,
        content_hash=path,
    )


def _file_node(path: str) -> GraphNode:
    return GraphNode(node_id=f"file:{path}", kind="file", label=path, source_ref=path)


def _symbol_node(path: str, name: str, start: int, end: int) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind="func",
        label=name,
        source_ref=path,
        granularity="symbol",
        signature=f"def {name}():",
        span_start=start,
        span_end=end,
    )


def test_symbol_span_from_lower_ranked_file_surfaces(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    # X: keyword magnet made of comments and imports only.
    noise_lines = ["import os", "import sys"]
    noise_lines += ["# frobnicate flux frobnicate flux fix" for _ in range(30)]
    noise = "\n".join(noise_lines) + "\n"

    # Y: large file whose bulk is unrelated padding, plus the real fix site.
    padding = "\n".join(
        f"def pad_{i}():\n    return unrelated_widget_{i} + gadget_{i}\n" for i in range(60)
    )
    target = "def frobnicate_flux():\n    return flux + 1\n"
    y_content = target + "\n" + padding + "\n"

    sources = [_source("notes.py", noise), _source("y.py", y_content)]
    y_lines = y_content.splitlines()
    nodes = [
        _file_node("notes.py"),
        _file_node("y.py"),
        _symbol_node("y.py", "frobnicate_flux", 1, 2),
    ] + [
        _symbol_node("y.py", f"pad_{i}", y_lines.index(f"def pad_{i}():") + 1, y_lines.index(f"def pad_{i}():") + 2)
        for i in range(60)
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=400, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    assert items, "bundle must not be empty"

    symbol_items = [item for item in items if item["kind"] == "symbol"]
    assert any(
        item["item_id"] == "symbol-span:symbol:y.py:frobnicate_flux" and item["path"] == "y.py"
        for item in symbol_items
    )

    code_like = [item for item in items if item["kind"] in ("code", "symbol")]
    first = code_like[0]
    assert "frobnicate_flux" in first["content"]
    for item in code_like:
        body_lines = [line for line in item["content"].splitlines() if line.strip()]
        assert not all(line.strip().startswith(("import ", "#include")) for line in body_lines)


def test_symbol_item_metadata_and_no_include_block_content(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    padding = "\n".join(
        f"def filler_{i}():\n    return other_thing_{i}\n" for i in range(80)
    )
    content = "import os\nimport sys\n\ndef frobnicate_flux():\n    return flux + 1\n\n" + padding + "\n"
    sources = [_source("engine.py", content)]
    nodes = [
        _file_node("engine.py"),
        _symbol_node("engine.py", "frobnicate_flux", 4, 5),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    observed_kinds = []
    original_count = bundle_mod.count_text_tokens

    def recording_count(text, kind="text"):
        if text.startswith("def frobnicate_flux"):
            observed_kinds.append(kind)
        return original_count(text, kind=kind)

    monkeypatch.setattr(bundle_mod, "count_text_tokens", recording_count)
    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=200, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    assert items
    span_items = [item for item in items if item["kind"] == "symbol"]
    assert len(span_items) == 1
    span = span_items[0]
    assert span["metadata"]["source_kind"] == "code"
    assert span["metadata"]["node_id"] == "symbol:engine.py:frobnicate_flux"
    assert span["metadata"]["span_start"] == 4
    assert span["metadata"]["span_end"] == 5
    assert "frobnicate_flux" in span["content"]
    assert "import os" not in span["content"]
    assert observed_kinds and set(observed_kinds) == {"code"}


def test_whole_file_still_returned_when_it_fits(tmp_path):
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)

    content = "def frobnicate_flux():\n    return flux + 1\n"
    sources = [_source("small.py", content)]
    nodes = [
        _file_node("small.py"),
        _symbol_node("small.py", "frobnicate_flux", 1, 2),
    ]
    store.reset_repo(repo)
    store.save_sources(repo, sources)
    store.save_graph(repo, nodes, [])

    bundle = generate_bundle(repo_path=repo, task="fix frobnicate flux", budget=4000, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    items = bundle["items"]
    code_items = [item for item in items if item["kind"] == "code"]
    assert len(code_items) == 1
    assert code_items[0]["content"] == content
    assert not any(item["kind"] == "symbol" for item in items)


# --- file-level ranking and the deadline reserve (0.12) ---


class _FakeDeadline:
    enabled = True
    budget_seconds = 30.0

    def __init__(self, remaining: float) -> None:
        self.remaining = remaining


def _tiny_repo(tmp_path):
    repo = _make_repo(tmp_path)
    store = CortexStore(tmp_path / "db.sqlite")
    sources = [
        _source("auth.py", "def login():\n    return verify()\n"),
        _source("verify.py", "def verify():\n    return True\n"),
    ]
    nodes = [_file_node("auth.py"), _file_node("verify.py"), _symbol_node("auth.py", "login", 1, 2), _symbol_node("verify.py", "verify", 1, 2)]
    return repo, store, sources, (nodes, [])


def test_bundle_skips_graph_rank_when_the_time_reserve_is_gone(tmp_path, monkeypatch):
    repo, store, sources, graph = _tiny_repo(tmp_path)
    monkeypatch.setattr(bundle_mod, "current_deadline", lambda: _FakeDeadline(remaining=1.0))
    calls = []
    monkeypatch.setattr(bundle_mod, "personalized_pagerank", lambda *a, **k: calls.append(1) or {})

    result = generate_bundle(repo, "login", 2000, db_path=tmp_path / "db.sqlite", output_format="json", sources=sources, graph=graph)

    assert calls == []
    assert result["degraded"] == ["graph_rank"]
    assert "Graph ranking skipped to fit the time budget." in result["confidence_notes"]
    assert result["items"]  # keyword results are still returned


def test_bundle_runs_graph_rank_when_time_remains(tmp_path, monkeypatch):
    repo, store, sources, graph = _tiny_repo(tmp_path)
    monkeypatch.setattr(bundle_mod, "current_deadline", lambda: _FakeDeadline(remaining=29.0))

    result = generate_bundle(repo, "login", 2000, db_path=tmp_path / "db.sqlite", output_format="json", sources=sources, graph=graph)

    assert "degraded" not in result


# --- spread the budget across files (0.12, Task 5.3) ---


def _packed(tmp_path, content: str, symbols: list[tuple[str, int, int]], task: str, budget: int) -> dict:
    repo = _make_repo(tmp_path)
    db_path = tmp_path / "cortex.db"
    store = CortexStore(db_path)
    nodes = [_file_node("w.py")] + [_symbol_node("w.py", name, start, end) for name, start, end in symbols]
    store.reset_repo(repo)
    store.save_sources(repo, [_source("w.py", content)])
    store.save_graph(repo, nodes, [])
    bundle = generate_bundle(repo_path=repo, task=task, budget=budget, db_path=db_path, output_format="json")
    assert isinstance(bundle, dict)
    return bundle


def test_a_span_nested_in_a_selected_span_is_not_selected_again(tmp_path):
    padding = "\n".join(f"def pad_{i}():\n    return unrelated_widget_{i} + gadget_{i}\n" for i in range(60))
    content = (
        "class ReconnectHandler:\n"
        "    def reconnect_device(self):\n"
        "        return self.reconnect_state\n"
        "\n"
        f"{padding}\n"
    )
    bundle = _packed(
        tmp_path, content, [("ReconnectHandler", 1, 3), ("reconnect_device", 2, 3)], "reconnect device state", budget=400
    )

    spans = [item["item_id"] for item in bundle["items"] if item["kind"] == "symbol"]
    # The whole file does not fit, but the class span and the method span (its last two
    # lines) both do; selecting both would print the same lines twice.  Since the proportional name
    # bonus (0.14) the method (2 of 3 task terms) outscores the class (1 of 3) and is selected first:
    # the enclosing class span must then be skipped too.
    assert len(spans) == 1, spans


def test_a_span_enclosing_a_selected_span_is_not_selected_either(tmp_path):
    padding = "\n".join(f"def pad_{i}():\n    return unrelated_widget_{i} + gadget_{i}\n" for i in range(60))
    content = (
        "class ReconnectDeviceHandler:\n"
        "    def reconnect_device(self):\n"
        "        return self.reconnect_state\n"
        "\n"
        f"{padding}\n"
    )
    bundle = _packed(
        tmp_path, content, [("ReconnectDeviceHandler", 1, 3), ("reconnect_device", 2, 3)], "reconnect device state", budget=400
    )
    spans = [item["item_id"] for item in bundle["items"] if item["kind"] == "symbol"]
    assert len(spans) == 1, spans


def test_the_spread_task_reaches_the_helpers_its_spans_call_into(tmp_path, monkeypatch):
    from evals.run_evals import SPREAD_GOLD_TASKS, build_fixture_repos

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_SEMANTIC", "0")
    from cortex.ingest import ingest_repository

    task = SPREAD_GOLD_TASKS[0]
    repo = build_fixture_repos(tmp_path)["spread_repo"]
    ingest_repository(repo)

    bundle = generate_bundle(repo, task=task.description, budget=task.budget, output_format="json")

    paths = {item["path"] for item in bundle["items"]}
    assert set(task.expected_files) <= paths
    assert bundle["total_tokens"] <= task.budget


def test_a_files_first_spans_may_use_the_whole_budget(tmp_path):
    body = "\n".join(f"    value_{i} = reconnect_{i} + timeout" for i in range(25))
    content = f"def reconnect_timeout():\n{body}\n    return 1\n"
    end = len(content.splitlines())
    bundle = _packed(tmp_path, content, [("reconnect_timeout", 1, end), ("reconnect_other", 1, 2)], "reconnect timeout", budget=500)

    # The held-back share must never squeeze the only good hit of a file.
    assert any("reconnect_timeout" in item["content"] for item in bundle["items"])
