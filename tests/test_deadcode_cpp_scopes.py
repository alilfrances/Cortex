from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository

NAMESPACE_FILES = {
    "lib/ns.h": "namespace Outer::Inner { int used(); }\nnamespace Plain { int p(); }\n",
    "lib/ns.cpp": (
        '#include "ns.h"\n'
        "namespace Outer::Inner {\nint used() { return 1; }\n}\n"
        "namespace Plain {\nint p() { return Outer::Inner::used(); }\n}\n"
    ),
    "app/main.cpp": '#include "../lib/ns.h"\nint main() { return Plain::p(); }\n',
    "app/versioned.cpp": "inline namespace v1 {\nint q() { return 0; }\n}\n",
}


def _findings(tmp_path: Path, monkeypatch, files: dict[str, str]) -> dict[tuple[str, str], dict]:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    for rel, content in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    ingest_repository(repo)
    return {(item["file"], item["symbol"]): item for item in analyze_dead_code(repo)["findings"]}


def test_namespace_nodes_are_never_reported(tmp_path, monkeypatch):
    findings = _findings(tmp_path, monkeypatch, NAMESPACE_FILES)
    symbols = {symbol for _, symbol in findings}
    for name in ("Inner", "Plain", "Outer", "v1"):
        assert name not in symbols, (name, findings)
    # A real unreferenced function inside a namespace is still reported.
    assert ("app/versioned.cpp", "q") in findings


SPECIAL_MEMBER_FILES = {
    "core/foo.h": (
        "namespace app::core {\n"
        "class Foo {\npublic:\n    Foo();\n    ~Foo();\n    int x();\n    int unused();\n};\n"
        "}\n"
    ),
    "core/foo.cpp": (
        '#include "foo.h"\n'
        "namespace app::core {\n"
        "Foo::Foo() {}\n"
        "Foo::~Foo() {}\n"
        "int Foo::x() { return 1; }\n"
        "int Foo::unused() { return 2; }\n"
        "}\n"
    ),
    "ui/main.cpp": (
        '#include "../core/foo.h"\n'
        "int main() { auto* f = new app::core::Foo(); f->x(); delete f; return 0; }\n"
    ),
}


def test_out_of_line_constructors_and_destructors_are_never_reported(tmp_path, monkeypatch):
    findings = _findings(tmp_path, monkeypatch, SPECIAL_MEMBER_FILES)
    in_foo = {symbol for file, symbol in findings if file == "core/foo.cpp"}
    assert "Foo" not in in_foo and "~Foo" not in in_foo, findings
    # The control: a method defined but never called is still dead.
    assert ("core/foo.cpp", "unused") in findings
