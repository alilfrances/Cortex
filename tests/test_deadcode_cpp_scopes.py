from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from tests.golden_utils import TREE_SITTER_ONLY

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


# Methods called only from their own class. These resolve on 0.13.0 and 0.14.0; the test is a fence so that a
# later change to `_Index.locality()` or to namespace handling cannot break them silently.
IN_CLASS_CALL_FILES = {
    "app/inl.h": (
        "class Inl {\npublic:\n"
        "    int a() { b(); return this->c(); }\n"
        "    int b() { return 1; }\n"
        "    int c() { return 2; }\n"
        "};\n"
    ),
    "app/widget.h": (
        "class Widget {\npublic:\n"
        "    int run();\n"
        "    int helperOut();\n"
        "    int helperInline() { return 3; }\n"
        "};\n"
    ),
    "app/widget.cpp": (
        '#include "widget.h"\n'
        "int Widget::run() { return helperOut() + helperInline(); }\n"
        "int Widget::helperOut() { return 4; }\n"
    ),
    "core/bar.h": "namespace app { namespace core {\nclass Bar {\npublic:\n    int x();\n    int y();\n};\n} }\n",
    "core/bar.cpp": (
        '#include "bar.h"\n'
        "namespace app { namespace core {\n"
        "int Bar::x() { return Bar::y(); }\n"
        "int Bar::y() { return 5; }\n"
        "} }\n"
    ),
    "core/baz.h": "namespace core {\nclass Baz {\npublic:\n    int x();\n    int y();\n};\n}\n",
    "core/baz.cpp": (
        '#include "baz.h"\n'
        "using namespace core;\n"
        "int Baz::x() { return y(); }\n"
        "int Baz::y() { return 6; }\n"
    ),
    "app/main.cpp": (
        '#include "inl.h"\n#include "widget.h"\n#include "../core/bar.h"\n#include "../core/baz.h"\n'
        "int main() {\n"
        "    Inl i; i.a();\n"
        "    Widget w; w.run();\n"
        "    app::core::Bar bar; bar.x();\n"
        "    core::Baz baz; baz.x();\n"
        "    return 0;\n"
        "}\n"
    ),
}


@TREE_SITTER_ONLY
def test_methods_called_only_from_their_own_class_are_not_reported(tmp_path, monkeypatch):
    findings = _findings(tmp_path, monkeypatch, IN_CLASS_CALL_FILES)
    # The classes themselves are reported until Task 1.4 (they are used through declarations only); the
    # methods, which is what this fence is about, never are.
    classes = {"Inl", "Widget", "Bar", "Baz"}
    reported = sorted(f"{file}:{symbol}" for file, symbol in findings if symbol not in classes)
    assert reported == [], reported
