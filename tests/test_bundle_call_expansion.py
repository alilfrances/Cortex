from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cortex.bundle import CALL_EXPANSION_BONUS, generate_bundle
from cortex.ingest import ingest_repository

FILES = {
    # Keyword hit: the manager talks about the task in its own words.
    "src/manager.cpp": (
        "#include \"base.hpp\"\n"
        "// device card state wrong after reconnect: the manager restores the card state on reconnect\n"
        "void Manager::reconnect(Widget* widget) {\n"
        "    widget->apply(1);\n"
        "}\n"
    ),
    # The behaviour lives here, in words the task never uses.
    "src/base.hpp": "class Base {\npublic:\n    virtual void apply(int v);\nprotected:\n    int slot_ = 0;\n};\n",
    "src/base.cpp": "#include \"base.hpp\"\nvoid Base::apply(int v) { slot_ = v; }\n",
    "src/widget.hpp": "#include \"base.hpp\"\nclass Widget : public Base {\npublic:\n    void apply(int v) override;\n};\n",
    "src/widget.cpp": "#include \"widget.hpp\"\nvoid Widget::apply(int v) { Base::apply(v); }\n",
    # Unrelated noise.
    "src/other.cpp": "void unrelated() { int x = 1; }\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    root = tmp_path / "repo"
    for rel, content in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def test_files_linked_by_calls_and_inheritance_are_lifted_by_the_expansion(repo):
    bundle = generate_bundle(
        repo_path=repo, task="device card state wrong after reconnect", budget=4000, output_format="json"
    )
    scores = {item["path"]: item["score"] for item in bundle["items"] if item["kind"] == "code"}
    assert "src/manager.cpp" in scores
    # Neither file shares a word with the task; both are one resolved call edge from the manager.
    assert scores["src/widget.cpp"] >= CALL_EXPANSION_BONUS
    assert scores["src/widget.cpp"] > scores.get("src/other.cpp", 0.0)
    # The expansion lifts files linked to the best match, not the whole repository.
    assert scores.get("src/other.cpp", 0.0) < CALL_EXPANSION_BONUS
