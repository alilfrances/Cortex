"""R1 (0.14.0 review §F cause 6): a C++ pure virtual declaration (`virtual void f() = 0;`) is a symbol.

The tree-sitter extractor used to emit nothing for it, so a call through a base pointer stayed
`name:` and `overrides.find_overriders` could not start from it.  Only `= 0` declarations are
symbols: other member declarations would change dead_code and every graph golden.  The regex
backend emits no method declarations at all (only Qt signal/slot members), so these tests are
tree-sitter-only.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.deadcode import analyze_dead_code
from cortex.graph import build_graph
from cortex.ingest import _classify_path, ingest_repository
from cortex.models import SourceRecord
from cortex.overrides import find_overriders
from cortex.structural.treesitter_backend import extract_treesitter_edges
from tests.golden_utils import TREE_SITTER_ONLY

pytestmark = TREE_SITTER_ONLY

BASE_H = """\
class Base {
public:
    virtual void f() = 0;
    virtual int g(int a) const = 0;
    virtual Item* make(int a,
                       int b) = 0;
    virtual void spaced() = 0 ;
    virtual void defaulted(int a = 0) = 0;
    virtual void plain();
    virtual void deleted() = delete;
    virtual ~Base() = default;
    int count = 0;
    void (*callback)() = 0;
    static int helper();
};
"""


def _symbols(path: str, content: str) -> dict[str, tuple]:
    nodes, edges = extract_treesitter_edges(path, content, set())
    contains = {e.target for e in edges if e.relation == "contains"}
    out = {}
    for n in nodes:
        if n.granularity == "symbol":
            assert n.node_id in contains, n.node_id
            out[n.label] = (n.kind, n.span_start, n.span_end, n.signature, n.metadata)
    return out


def test_pure_virtual_declarations_are_func_symbols_and_other_declarations_are_not():
    symbols = _symbols("src/base.h", BASE_H)

    # `deleted` was already a symbol (a `function_definition`); `plain`, `count`, `callback` and `helper` stay none
    assert set(symbols) == {"Base", "f", "g", "make", "spaced", "defaulted", "deleted"}
    assert symbols["f"] == ("func", 3, 3, "virtual void f() = 0;", {"lineno": 3})
    assert symbols["g"][3] == "virtual int g(int a) const = 0;"
    assert symbols["make"][:3] == ("func", 5, 6)  # pointer return type, two lines
    assert symbols["make"][3] == "virtual Item* make(int a, int b) = 0;"
    assert symbols["spaced"][3] == "virtual void spaced() = 0 ;"
    assert "qualifier" not in symbols["f"][4]


def test_pure_virtuals_of_two_classes_in_one_file_share_the_first_node():
    content = "class A { virtual void run() = 0; };\nclass B { virtual void run() = 0; };\n"
    nodes, _edges = extract_treesitter_edges("src/two.h", content, set())

    assert [n.node_id for n in nodes if n.label == "run"] == ["symbol:src/two.h:run"]


def test_a_struct_and_a_pure_virtual_named_like_an_existing_symbol_do_not_duplicate():
    content = "void run();\nvoid run() {}\nstruct S { virtual void run() = 0; };\n"
    nodes, _edges = extract_treesitter_edges("src/dup.h", content, set())

    assert [n.node_id for n in nodes if n.label == "run"] == ["symbol:src/dup.h:run"]


def _sources() -> list[SourceRecord]:
    files = {
        "src/base.h": "class Base {\npublic:\n    virtual void process(int v) = 0;\n};\n",
        "src/impl.h": '#include "base.h"\nclass Impl : public Base {\npublic:\n    void process(int v) override;\n};\n',
        "src/impl.cpp": '#include "impl.h"\n\nvoid Impl::process(int v) {\n    int w = v;\n    w += 1;\n}\n',
        "src/user.cpp": '#include "base.h"\n\nvoid use(Base* b) {\n    b->process(3);\n}\n',
    }
    return [SourceRecord(path=p, content=c, kind=_classify_path(Path(p)), size_bytes=len(c), modified_at=0.0) for p, c in files.items()]


def test_a_call_through_a_base_pointer_resolves_to_the_pure_virtual_and_its_overriders_are_found():
    nodes, edges = build_graph(_sources(), [])

    call = next(e for e in edges if e.relation == "calls" and e.metadata.get("callee") == "process")
    assert call.source == "symbol:src/user.cpp:use"
    assert call.target == "symbol:src/base.h:process"
    bases = {e.source: [e.target] for e in edges if e.relation == "inherits" and e.target.startswith("symbol:")}
    assert find_overriders(nodes, bases) == {"symbol:src/base.h:process": ["symbol:src/impl.cpp:process"]}


def _dead_code(tmp_path: Path, monkeypatch, files: dict[str, str]) -> dict[tuple[str, str], dict]:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=repo, check=True)
    ingest_repository(repo)
    return {(item["file"], item["symbol"]): item for item in analyze_dead_code(repo)["findings"]}


def test_a_pure_virtual_is_never_reported_as_dead_code(tmp_path, monkeypatch):
    files = {
        "src/base.h": (
            "class Base {\npublic:\n"
            "    virtual void called() = 0;\n"      # called through a base pointer
            "    virtual void overridden() = 0;\n"  # overridden, never called
            "    virtual void unused() = 0;\n"      # an interface nobody uses yet
            "    void realUnused();\n"              # a plain declaration is not a symbol: nothing changes
            "};\n"
        ),
        "src/impl.h": '#include "base.h"\nclass Impl : public Base {\npublic:\n    void overridden() override {}\n};\n',
        "src/user.cpp": '#include "base.h"\nvoid use(Base* b) {\n    b->called();\n}\n',
    }

    found = _dead_code(tmp_path, monkeypatch, files)

    assert not {pair for pair in found if pair[0] == "src/base.h"}, sorted(found)


def test_an_out_of_line_override_inside_a_namespace_is_still_an_override(tmp_path, monkeypatch):
    """The overload `data(a, b)` calls `data(a)`: the call now resolves to the pure virtual declaration, so the
    out-of-line definition loses its (accidental, self) incoming edge.  It must be reported as an override
    (the `ns` namespace around it is a class node that used to hide the `Role::` qualifier), never as plain dead code."""
    files = {
        "src/role.h": "namespace ns {\nclass Role {\npublic:\n    virtual int data(int a, int b);\n    virtual int data(int a) = 0;\n};\n}\n",
        "src/role.cpp": (
            '#include "role.h"\nnamespace ns {\n\nint Role::data(int a, int b)\n{\n    return data(a);\n}\n\n}\n'
        ),
    }

    found = _dead_code(tmp_path, monkeypatch, files)

    item = found[("src/role.cpp", "data")]
    assert item["confidence"] == "low"
    assert "overrides a base-class or framework method" in item["reason"], item
