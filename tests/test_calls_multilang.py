"""Task 8: Go, Rust, Swift, Java and Ruby call extraction (tree-sitter) and the name-only regex fallback.

Fixture: ``tests/fixtures/calls_multilang/<lang>/`` -- call-specific, one directory per language
(resolution never crosses language families, so a Java call is never matched to a Swift symbol of
the same name; ``test_resolution_never_crosses_language_families`` pins that).

Known extractor limits pinned here rather than hidden:

* Ruby: ``def``/``def self.x`` are symbols (approved narrow exception to the frozen baseline: only
  newly missing Ruby method symbols and their ``contains`` edges), owned lexically by their
  class/module; ``@x = T.new`` types instance variables; superclass/``include`` are ``bases``.
* Go/Rust methods are not contained by their type's span, so their nodes carry a ``qualifier``
  (owning type) that the resolver uses; Swift does the same for ``extension`` methods.
"""

from __future__ import annotations

import pytest

from cortex.callresolve import language_family, resolve_call_endpoints
from cortex.models import GraphEdge, GraphNode
from tests.call_helpers import (
    CONTRACT_KEYS, assert_ambiguous, assert_regex_name_only, assert_resolved, assert_unresolved,
    by_site, calls, fixture_graph,
)
from tests.golden_utils import TREE_SITTER_ONLY

FIXTURE = "calls_multilang"

# lang -> {"resolved": [(file, line, callee, target, tier)], "unresolved": [(file, line, callee)],
#          "ambiguous": [(file, line, callee, [candidates])]}
CASES = {
    "go": {
        "resolved": [
            ("go/main.go", 13, "Step", "symbol:go/main.go:Step", "this"),
            ("go/main.go", 14, "Save", "symbol:go/store.go:Save", "declared_type"),   # s.repo.Save(): field *Repo
            ("go/main.go", 15, "Help", "symbol:go/util/util.go:Help", "scoped"),      # helpers.Help(): import alias
            ("go/main.go", 17, "M", "symbol:go/store.go:M", "declared_type"),         # var x Foo
            ("go/main.go", 19, "M", "symbol:go/store.go:M", "declared_type"),         # y := &Foo{}
            ("go/main.go", 20, "local", "symbol:go/main.go:local", "same_file"),
            ("go/main.go", 21, "shared", "symbol:go/store.go:shared", "same_package"),  # other file, same package
            ("go/main.go", 23, "Save", "symbol:go/store.go:Save", "declared_type"),   # r *Repo parameter
        ],
        "unresolved": [("go/main.go", 22, "Println")],                                # fmt: external package
        "ambiguous": [("go/main.go", 25, "Do", ["symbol:go/a/w.go:Do", "symbol:go/b/w.go:Do"])],
    },
    "rust": {
        "resolved": [
            ("rust/src/main.rs", 10, "step", "symbol:rust/src/main.rs:step", "this"),   # self.step()
            ("rust/src/main.rs", 11, "save", "symbol:rust/src/store.rs:save", "declared_type"),  # self.repo.save()
            ("rust/src/main.rs", 12, "make", "symbol:rust/src/main.rs:make", "scoped"),  # Svc::make()
            ("rust/src/main.rs", 13, "free", "symbol:rust/src/util.rs:free", "scoped"),  # util::free(): module
            ("rust/src/main.rs", 14, "helper", "symbol:rust/src/util.rs:helper", "import_scoped"),  # use crate::util::helper
            ("rust/src/main.rs", 15, "new", "symbol:rust/src/store.rs:new", "scoped"),   # Foo::new()
            ("rust/src/main.rs", 16, "m", "symbol:rust/src/store.rs:m", "declared_type"),  # let x: Foo
            ("rust/src/main.rs", 17, "default", "symbol:rust/src/store.rs:default", "scoped"),
            ("rust/src/main.rs", 18, "n", "symbol:rust/src/store.rs:n", "declared_type"),  # let y = Bar::default()
            ("rust/src/main.rs", 19, "save", "symbol:rust/src/store.rs:save", "declared_type"),  # r: &Repo
            ("rust/src/main.rs", 20, "local", "symbol:rust/src/main.rs:local", "same_file"),
        ],
        "unresolved": [],
        "ambiguous": [("rust/src/main.rs", 34, "draw", ["symbol:rust/src/a.rs:draw", "symbol:rust/src/b.rs:draw"])],
    },
    "swift": {
        "resolved": [
            ("swift/Sample.swift", 5, "step", "symbol:swift/Sample.swift:step", "this"),     # implicit self
            ("swift/Sample.swift", 6, "step", "symbol:swift/Sample.swift:step", "this"),     # self.step()
            ("swift/Sample.swift", 7, "save", "symbol:swift/Models.swift:save", "declared_type"),  # property: Repo
            ("swift/Sample.swift", 8, "make", "symbol:swift/Sample.swift:make", "scoped"),   # Svc.make()
            ("swift/Sample.swift", 9, "Foo", "symbol:swift/Models.swift:Foo", "repo_unique"),
            ("swift/Sample.swift", 10, "m", "symbol:swift/Models.swift:m", "declared_type"),  # let x: Foo
            ("swift/Sample.swift", 11, "Bar", "symbol:swift/Models.swift:Bar", "repo_unique"),
            ("swift/Sample.swift", 12, "n", "symbol:swift/Models.swift:n", "declared_type"),   # let y = Bar()
            ("swift/Sample.swift", 13, "save", "symbol:swift/Models.swift:save", "declared_type"),
            ("swift/Sample.swift", 14, "free", "symbol:swift/Models.swift:free", "repo_unique"),
            ("swift/Sample.swift", 15, "inheritedOp", "symbol:swift/Models.swift:inheritedOp", "inherited"),
            ("swift/Sample.swift", 27, "step", "symbol:swift/Sample.swift:step", "this"),     # inside `extension Svc`
        ],
        "unresolved": [],
        "ambiguous": [("swift/Sample.swift", 32, "draw", ["symbol:swift/PanelA.swift:draw", "symbol:swift/PanelB.swift:draw"])],
    },
    "java": {
        "resolved": [
            ("java/Svc.java", 7, "step", "symbol:java/Svc.java:step", "this"),               # implicit this
            ("java/Svc.java", 8, "step", "symbol:java/Svc.java:step", "this"),
            ("java/Svc.java", 9, "save", "symbol:java/Repo.java:save", "declared_type"),     # field Repo
            ("java/Svc.java", 10, "help", "symbol:java/com/x/Util.java:help", "scoped"),     # import com.x.Util
            ("java/Svc.java", 11, "make", "symbol:java/Svc.java:make", "scoped"),            # Svc.make()
            ("java/Svc.java", 12, "Foo", "symbol:java/Foo.java:Foo", "repo_unique"),         # new Foo()
            ("java/Svc.java", 13, "m", "symbol:java/Foo.java:m", "declared_type"),
            ("java/Svc.java", 14, "Bar", "symbol:java/Bar.java:Bar", "repo_unique"),
            ("java/Svc.java", 15, "n", "symbol:java/Bar.java:n", "declared_type"),           # var y = new Bar()
            ("java/Svc.java", 16, "save", "symbol:java/Repo.java:save", "declared_type"),    # parameter Repo r
            ("java/Svc.java", 17, "q", "symbol:java/Baz.java:q", "declared_type"),           # new Baz().q()
            ("java/Svc.java", 17, "Baz", "symbol:java/Baz.java:Baz", "repo_unique"),
            ("java/Svc.java", 18, "inheritedOp", "symbol:java/Base.java:inheritedOp", "inherited"),
            ("java/Svc.java", 19, "inheritedOp", "symbol:java/Base.java:inheritedOp", "inherited"),  # super.
        ],
        "unresolved": [],
        "ambiguous": [("java/Amb.java", 3, "draw", ["symbol:java/a/Panel.java:draw", "symbol:java/b/Panel.java:draw"])],
    },
    "ruby": {
        "resolved": [
            ("ruby/svc.rb", 7, "Repo", "symbol:ruby/repo.rb:Repo", "repo_unique"),          # @repo = Repo.new
            ("ruby/svc.rb", 11, "step", "symbol:ruby/svc.rb:step", "this"),                # bare `step`: implicit self
            ("ruby/svc.rb", 12, "step", "symbol:ruby/svc.rb:step", "this"),                # self.step
            ("ruby/svc.rb", 13, "save", "symbol:ruby/repo.rb:save", "declared_type"),      # @repo typed by `@repo = Repo.new`
            ("ruby/svc.rb", 14, "help", "symbol:ruby/util.rb:help", "scoped"),             # Util.help (def self.help)
            ("ruby/svc.rb", 15, "go", "symbol:ruby/util.rb:go", "scoped"),                 # Util::Inner.go
            ("ruby/svc.rb", 16, "Foo", "symbol:ruby/foo.rb:Foo", "repo_unique"),           # x = Foo.new
            ("ruby/svc.rb", 17, "m", "symbol:ruby/foo.rb:m", "declared_type"),             # x.m, x = Foo.new
            ("ruby/svc.rb", 18, "helper", "symbol:ruby/helpers.rb:helper", "repo_unique"),  # top-level def in another file
            ("ruby/svc.rb", 20, "inherited_op", "symbol:ruby/base.rb:inherited_op", "inherited"),  # class Svc < Base
            ("ruby/svc.rb", 21, "mixed", "symbol:ruby/mixin.rb:mixed", "inherited"),       # include Mixin
        ],
        "unresolved": [("ruby/svc.rb", 22, "other")],                                       # untyped parameter receiver
        "ambiguous": [("ruby/svc.rb", 19, "Widget", ["symbol:ruby/a.rb:Widget", "symbol:ruby/b.rb:Widget"])],
    },
}
LANGS = sorted(CASES)


@pytest.fixture(scope="module")
def ts_edges():
    return fixture_graph(FIXTURE)[1]


def _rows(kind: str):
    return [pytest.param(lang, row, id=f"{lang}-{row[0]}:{row[1]}:{row[2]}") for lang in LANGS for row in CASES[lang][kind]]


@TREE_SITTER_ONLY
@pytest.mark.parametrize("lang,row", _rows("resolved"))
def test_resolved_sites(ts_edges, lang, row) -> None:
    file, line, callee, target, tier = row
    assert_resolved(by_site(ts_edges), file, line, callee, target, tier)


@TREE_SITTER_ONLY
@pytest.mark.parametrize("lang,row", _rows("unresolved"))
def test_external_and_untyped_sites_stay_name_based(ts_edges, lang, row) -> None:
    edge = assert_unresolved(by_site(ts_edges), *row)
    assert edge.confidence == "EXTRACTED"


@TREE_SITTER_ONLY
@pytest.mark.parametrize("lang,row", _rows("ambiguous"))
def test_ambiguity_keeps_name_with_sorted_candidates(ts_edges, lang, row) -> None:
    assert_ambiguous(by_site(ts_edges), *row)


@TREE_SITTER_ONLY
def test_contract_and_edge_ids(ts_edges) -> None:
    for edge in calls(ts_edges):
        assert CONTRACT_KEYS <= set(edge.metadata)
    assert len({e.edge_id for e in calls(ts_edges)}) == len(calls(ts_edges))
    sites = by_site(ts_edges)
    md = sites[("go/main.go", 13, "Step")].metadata
    assert (md["receiver_kind"], md["enclosing_class"], md["implicit_receiver"] if "implicit_receiver" in md else False) == ("this", "Svc", False)
    md = sites[("go/main.go", 20, "local")].metadata
    assert md["package_scoped"] is True and md["receiver_kind"] == "none"
    assert sites[("rust/src/main.rs", 10, "step")].metadata["receiver_kind"] == "self"
    assert sites[("rust/src/main.rs", 12, "make")].metadata["scope"] == "Svc"
    assert sites[("swift/Sample.swift", 27, "step")].metadata["enclosing_class"] == "Svc"
    assert sites[("java/Svc.java", 19, "inheritedOp")].metadata["super"] is True
    assert sites[("java/Svc.java", 12, "Foo")].metadata["receiver_kind"] == "new"
    assert sites[("ruby/svc.rb", 12, "step")].metadata["receiver_kind"] == "self"
    assert sites[("ruby/svc.rb", 11, "step")].metadata["enclosing_class"] == "Svc"
    assert sites[("ruby/svc.rb", 11, "step")].source == "symbol:ruby/svc.rb:run"      # method owns its calls
    assert sites[("ruby/svc.rb", 7, "Repo")].source == "symbol:ruby/svc.rb:initialize"
    assert sites[("ruby/svc.rb", 17, "m")].metadata["receiver_type"] == "Foo"  # x = Foo.new
    assert sites[("ruby/svc.rb", 13, "save")].metadata["receiver_type"] == "Repo"  # @repo = Repo.new (class field)


@TREE_SITTER_ONLY
def test_method_qualifiers_and_bases_are_the_only_added_node_metadata() -> None:
    nodes, _ = fixture_graph(FIXTURE)
    by_id = {n.node_id: n for n in nodes}
    assert by_id["symbol:go/main.go:Step"].metadata["qualifier"] == "Svc"
    assert by_id["symbol:rust/src/store.rs:save"].metadata["qualifier"] == "Repo"
    assert by_id["symbol:swift/Sample.swift:ext"].metadata["qualifier"] == "Svc"
    assert "qualifier" not in by_id["symbol:swift/Sample.swift:step"].metadata  # contained by its class
    assert by_id["symbol:ruby/svc.rb:Svc"].metadata["bases"] == ["Base", "Mixin"]
    assert by_id["symbol:java/Svc.java:Svc"].metadata["bases"] == ["Base"]
    assert by_id["symbol:swift/Sample.swift:Svc"].metadata["bases"] == ["Base"]


@TREE_SITTER_ONLY
def test_resolution_never_crosses_language_families(ts_edges) -> None:
    assert language_family("a.hpp") == language_family("b.c") != language_family("c.rs")
    assert language_family("x.ts") == language_family("y.jsx")
    for edge in calls(ts_edges):
        if edge.target.startswith("symbol:"):
            src = edge.metadata["source_file"]
            assert language_family(edge.target.split(":")[1]) == language_family(src), edge
        for cand in edge.metadata.get("candidates", []):
            assert language_family(cand.split(":")[1]) == language_family(edge.metadata["source_file"])


def test_ruby_member_tiers_on_a_handcrafted_graph() -> None:
    """Unit-level tier check; the parser-backed end-to-end Ruby cases are in CASES above."""
    def n(path, name, kind, span):
        return GraphNode(f"symbol:{path}:{name}", kind, name, path, "symbol", "", span[0], span[1], {})

    nodes = [
        n("svc.rb", "Svc", "class", (1, 9)), n("svc.rb", "step", "func", (2, 3)), n("svc.rb", "run", "func", (4, 8)),
        n("foo.rb", "Foo", "class", (1, 4)), n("foo.rb", "m", "func", (2, 3)),
    ]

    def call(callee, **md):
        base = {"lineno": 5, "source_file": "svc.rb", "callee": callee, "receiver": None, "receiver_kind": "none",
                "scope": None, "receiver_type": None, "enclosing_class": "Svc"}
        base.update(md)
        return GraphEdge(f"c:{callee}:{base['receiver_kind']}", "symbol:svc.rb:run", f"name:{callee}", "calls",
                         "STRUCTURAL", "EXTRACTED", 1.0, base)

    implicit, selfcall = call("step"), call("step", receiver="self", receiver_kind="self")
    typed = call("m", receiver="x", receiver_kind="var", receiver_type="Foo")
    resolve_call_endpoints(nodes, [implicit, selfcall, typed])
    assert (implicit.target, implicit.metadata["resolution"]) == ("symbol:svc.rb:step", "this")
    assert (selfcall.target, selfcall.metadata["resolution"]) == ("symbol:svc.rb:step", "this")
    assert (typed.target, typed.metadata["resolution"]) == ("symbol:foo.rb:m", "declared_type")


# --------------------------------------------------------------------------- #
# Regex fallback (all five families)
# --------------------------------------------------------------------------- #


def _regex_sites() -> set[tuple[str, int, str]]:
    sites: set[tuple[str, int, str]] = set()
    for lang in LANGS:
        rows = CASES[lang]
        sites |= {(f, line, c) for f, line, c, _, _ in rows["resolved"]}
        sites |= set(rows["unresolved"])
        sites |= {(f, line, c) for f, line, c, _ in rows["ambiguous"]}
    # Ruby's paren-less calls (`step`, `self.step`, `Foo.new`) have no `name(` for a regex to see.
    return {s for s in sites if s[0] != "ruby/svc.rb"} | {("ruby/svc.rb", 18, "helper")}


def test_regex_fallback_is_name_only_low_for_all_families(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORTEX_FORCE_REGEX", "1")
    _, edges = fixture_graph(FIXTURE)
    assert_regex_name_only(edges, _regex_sites())
    sites = by_site(edges)
    # unique, same-file definitions are still not resolved by the fallback
    assert sites[("go/main.go", 20, "local")].target == "name:local"
    assert sites[("rust/src/main.rs", 20, "local")].target == "name:local"
    assert sites[("java/Svc.java", 7, "step")].target == "name:step"


@TREE_SITTER_ONLY
def test_swift_orphan_stays_high_and_called_function_is_no_longer_a_candidate(tmp_path, monkeypatch) -> None:
    import json

    from cortex.ingest import ingest_repository
    from cortex.mcp.tools import call_tool
    from tests.baseline_helpers import copy_fixture_repo, FIXTURES

    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    repo = copy_fixture_repo(FIXTURES / FIXTURE / "swift", tmp_path / "repo")
    ingest_repository(repo, commit_limit=5)
    result = call_tool("cortex_dead_code", {"repo_path": str(repo), "format": "detailed", "budget": 9999})
    findings = {f["symbol"]: f["confidence"] for f in json.loads(result["content"][0]["text"])["findings"]}
    assert "free" not in findings and "inheritedOp" not in findings and "step" not in findings  # called
    assert findings.get("ext") == "high"  # nothing calls the extension method: orphan stays high


def test_go_unqualified_calls_never_leave_the_package_and_methods_are_not_free() -> None:
    def n(path, name, qualifier=None):
        md = {"qualifier": qualifier} if qualifier else {}
        return GraphNode(f"symbol:{path}:{name}", "func", name, path, "symbol", "", 1, 2, md)

    nodes = [n("a/x.go", "caller"), n("b/y.go", "helper"), n("a/z.go", "sibling"), n("a/m.go", "Run", "Svc")]

    def call(callee):
        md = {"lineno": 1, "source_file": "a/x.go", "callee": callee, "receiver": None, "receiver_kind": "none",
              "scope": None, "receiver_type": None, "enclosing_class": None,
              "implicit_receiver": False, "package_scoped": True}
        return GraphEdge(f"e:{callee}", "symbol:a/x.go:caller", f"name:{callee}", "calls", "STRUCTURAL", "EXTRACTED", 1.0, md)

    other_pkg, sibling, method = call("helper"), call("sibling"), call("Run")
    resolve_call_endpoints(nodes, [other_pkg, sibling, method])
    assert other_pkg.target == "name:helper"                      # package b: needs `pkg.helper()`
    assert (sibling.target, sibling.metadata["resolution"]) == ("symbol:a/z.go:sibling", "same_package")
    assert method.target == "name:Run"                            # a method is only reachable through a receiver
