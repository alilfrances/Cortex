"""Plan Task 5.5: `cortex_references` returns the source line of every hit; dead code does not change."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from cortex import deadcode
from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from cortex.references import SNIPPET_MAX_CHARS, find_references
from tests.test_references_writes import _references_repo

ROOT = Path(__file__).resolve().parents[1]


def _hits(result: dict) -> dict[str, dict]:
    return {entry["text"]: entry for entries in result["items"].values() for entry in entries}


def test_a_hit_carries_its_stripped_source_line(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    hits = _hits(find_references(store, repo, "x", snippets=True))

    assert hits["sample.py:1"]["snippet"] == "def x(value):"  # graph hit (the definition)
    assert hits["sample.py:3"]["snippet"] == "read = x"  # grep hit
    assert hits["sample.py:5"]["snippet"] == "x += 2"
    assert hits["sample.py:8"]["snippet"] == "return x"  # indentation stripped
    # `text` still parses as path:line, which dead-code analysis depends on.
    assert all(text.rpartition(":")[2].isdigit() for text in hits)


def test_snippets_are_off_by_default(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)

    assert inspect.signature(find_references).parameters["snippets"].default is False
    assert all("snippet" not in entry for entry in _hits(find_references(store, repo, "x")).values())


def test_a_long_line_is_cut(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    (repo / "long.py").write_text("x = 1  # " + "w" * 400 + "\n", encoding="utf-8")

    snippet = _hits(find_references(store, repo, "x", snippets=True))["long.py:1"]["snippet"]

    assert snippet == ("x = 1  # " + "w" * 400)[:SNIPPET_MAX_CHARS] + "…"


def test_snippets_count_against_the_budget_before_the_fit(tmp_path: Path) -> None:
    repo, store = _references_repo(tmp_path)
    lines = "".join(f"x = {index}  # " + "padding " * 12 + "\n" for index in range(60))
    (repo / "many.py").write_text(lines, encoding="utf-8")

    plain = find_references(store, repo, "x", budget=300)
    with_snippets = find_references(store, repo, "x", budget=300, snippets=True)

    assert plain["truncated"] and with_snippets["truncated"]
    assert 0 < with_snippets["returned_count"] < plain["returned_count"]
    assert sum(len(entries) for entries in with_snippets["items"].values()) == with_snippets["returned_count"]


def test_dead_code_findings_do_not_depend_on_snippets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "calls_cpp"
    for source in (ROOT / "tests" / "fixtures" / "calls_cpp").rglob("*"):
        if source.is_file():
            target = repo / source.relative_to(ROOT / "tests" / "fixtures" / "calls_cpp")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    ingest_repository(repo)

    default = analyze_dead_code(repo)
    real = deadcode.find_references

    def forced_off(*args, **kwargs):
        kwargs["snippets"] = False
        return real(*args, **kwargs)

    monkeypatch.setattr(deadcode, "find_references", forced_off)
    off = analyze_dead_code(repo)

    assert default["findings"] == off["findings"]
    assert default["findings"], "the fixture should have findings for the comparison to mean something"
