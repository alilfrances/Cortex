"""Call-extraction fixes found while vendoring the Task 11 OSS eval snapshots.

Each case is a minimal reproduction of a defect observed on real upstream code:

* oKcerG/SortFilterProxyModel ``filters/filter.cpp``: ``if (m_enabled) Q_EMIT invalidated();``
  -- in an unbraced control-flow body tree-sitter parses ``Q_EMIT`` as an ``ERROR``
  node followed by a plain ``call_expression``, so the signal emission was also
  recorded as a ``calls`` edge (Qt parity rule: ``emit`` stays an ``emits`` edge only).
* gorilla/mux ``mux.go``: ``func Vars(r *http.Request)`` after ``func (r *Router) ...``
  methods -- the Go receiver name was kept in an ``id(frame)``-keyed table, CPython
  reused the id of a collected method frame, and ``r.Context()`` in a plain function
  was classified as an implicit-``this`` call.
* ``call_sites._memo`` keyed per-batch indexes on ``id(known_paths)``; a later batch
  (the eval harness ingests several repos in one process) could be served a
  previous batch's index when the new set reused the old id.
"""

from __future__ import annotations

import gc

from cortex.graph import build_graph
from cortex.models import SourceRecord
from cortex.structural import call_sites
from tests.call_helpers import calls
from tests.golden_utils import TREE_SITTER_ONLY


def _source(path: str, content: str) -> SourceRecord:
    return SourceRecord(path=path, content=content, kind="code", size_bytes=len(content.encode()), modified_at=0.0)


QT_FILTER_H = """\
#include <QObject>
class Filter : public QObject {
    Q_OBJECT
public:
    void invalidate();
    void refresh();
Q_SIGNALS:
    void invalidated();
    void refreshed();
};
"""

QT_FILTER_CPP = """\
#include "filter.h"

void Filter::invalidate()
{
    if (m_enabled)
        Q_EMIT invalidated();
    while (m_pending)
        emit refreshed();
    refresh();
}

void Filter::refresh()
{
    Q_EMIT refreshed();
}
"""


@TREE_SITTER_ONLY
def test_unbraced_emit_is_an_emits_edge_not_a_call() -> None:
    _nodes, edges = build_graph([_source("filter.h", QT_FILTER_H), _source("filter.cpp", QT_FILTER_CPP)], [])
    call_targets = {(e.source, e.metadata["callee"]) for e in calls(edges)}

    assert ("symbol:filter.cpp:invalidate", "invalidated") not in call_targets
    assert ("symbol:filter.cpp:invalidate", "refreshed") not in call_targets
    # The ordinary call in the same body is still extracted and resolved.
    assert ("symbol:filter.cpp:invalidate", "refresh") in call_targets
    emits = {(e.source, e.target) for e in edges if e.relation == "emits"}
    assert ("symbol:filter.cpp:invalidate", "symbol:filter.h:invalidated") in emits
    assert ("symbol:filter.cpp:invalidate", "symbol:filter.h:refreshed") in emits


def _go_source() -> str:
    parts = ["package mux\n", 'import "net/http"\n', "type Router struct{}\n"]
    # Interleave many receiver methods and plain functions that reuse the
    # receiver's name for an unrelated parameter, so a stale receiver binding
    # (the original defect) is all but certain to be observed.
    for index in range(40):
        parts.append(f"func (r *Router) M{index}() {{ r.Helper() }}\n")
        parts.append(f"func Plain{index}(r *http.Request) {{ r.Context() }}\n")
    parts.append("func (r *Router) Helper() {}\n")
    return "\n".join(parts)


@TREE_SITTER_ONLY
def test_go_receiver_name_never_leaks_into_plain_functions() -> None:
    _nodes, edges = build_graph([_source("mux.go", _go_source())], [])
    plain = [e for e in calls(edges) if e.metadata["callee"] == "Context"]
    methods = [e for e in calls(edges) if e.metadata["callee"] == "Helper"]

    assert len(plain) == 40 and len(methods) == 40
    assert {e.metadata["receiver_kind"] for e in plain} == {"var"}
    assert {e.metadata["receiver_kind"] for e in methods} == {"this"}
    assert {e.target for e in methods} == {"symbol:mux.go:Helper"}


def test_memo_never_serves_a_previous_batch_index() -> None:
    builds: list[frozenset[str]] = []

    def build(known: set[str]) -> frozenset[str]:
        builds.append(frozenset(known))
        return frozenset(known)

    for index in range(25):
        known = {f"repo{index}/a.go", f"repo{index}/b.go"}
        assert call_sites._memo("test-memo", known, build) == frozenset(known)
        assert call_sites._memo("test-memo", known, build) == frozenset(known)  # same batch: cached
        del known
        gc.collect()
    assert len(builds) == 25
    call_sites._CACHE.pop("test-memo", None)


LAMBDA_CPP = """\
class Filter {
public:
    bool accepts(int row) const;
};
bool Filter::accepts(int row) const { return row > 0; }

class Model {
public:
    bool filterRow(int row) const;
};
bool Model::filterRow(int row) const
{
    auto pick = [=] (Filter* filter) {
        return filter->accepts(row);
    };
    auto untyped = [] (auto filter) { return filter->accepts(1); };
    return pick(nullptr) && untyped(nullptr);
}
"""


@TREE_SITTER_ONLY
def test_cpp_lambda_parameters_are_declared_types() -> None:
    """SortFilterProxyModel `filterAcceptsRow`: `std::all_of(..., [=](Filter* filter) {
    return filter->filterAcceptsRow(...); })` -- the lambda parameter's declared type
    resolves the call; an `auto` parameter stays unresolved."""
    _nodes, edges = build_graph([_source("model.cpp", LAMBDA_CPP)], [])
    sites = {(e.metadata["lineno"], e.metadata["callee"]): e for e in calls(edges)}

    typed = sites[(14, "accepts")]
    assert typed.source == "symbol:model.cpp:filterRow"
    assert typed.metadata["receiver_type"] == "Filter"
    assert typed.target == "symbol:model.cpp:accepts"
    assert typed.metadata["resolution"] == "declared_type"
    assert sites[(16, "accepts")].metadata["receiver_type"] is None


PRIVATE_TS = """\
export default class Queue {
	add<T>(task: T): T {
		return this.#add(task);
	}

	async #add<T>(task: T): Promise<T> {
		this.#setup();
		return task;
	}

	#setup(): void {
		this.addEventListener('add', () => undefined);
	}
}
"""


@TREE_SITTER_ONLY
def test_ts_private_methods_are_symbols_with_their_own_names() -> None:
    """sindresorhus/p-queue: `async #add<T>()` used to be labeled `T` and `#setup()` took
    the name of the first identifier in its body (`addEventListener`), so inherited
    `EventTarget.addEventListener` calls resolved to the wrong method."""
    nodes, edges = build_graph([_source("queue.ts", PRIVATE_TS)], [])
    labels = sorted(n.label for n in nodes if n.kind != "file")
    sites = {(e.metadata["lineno"], e.metadata["callee"]): e for e in calls(edges)}

    assert {"#add", "#setup", "add", "Queue"} <= set(labels)
    assert "T" not in labels and "addEventListener" not in labels
    assert sites[(3, "#add")].target == "symbol:queue.ts:#add"
    assert sites[(7, "#setup")].target == "symbol:queue.ts:#setup"
    assert sites[(12, "addEventListener")].target == "name:addEventListener"


RUST_MACROS = """\
pub struct Version;
impl Version {
    pub fn parse(text: &str) -> Version { Version }
    pub fn matches(&self, other: &Version) -> bool { true }
}
fn helper(v: &Version) -> bool { true }

fn check(req: &Version, text: &str) {
    assert!(req.matches(&Version::parse(text)), "no match {}", text);
    assert_eq!(helper(req), true);
    println!("{}", format!("{}", text));
}

macro_rules! noise {
    ($x:expr) => { not_a_call($x) };
}

#[derive(Debug)]
struct Marker;
"""


@TREE_SITTER_ONLY
def test_rust_calls_inside_macro_arguments_are_call_sites() -> None:
    """dtolnay/semver tests: `assert!(req.matches(&parsed))` -- tree-sitter-rust keeps macro
    arguments as token trees, which hid the dominant test idiom's calls.  Attribute and
    `macro_rules!` bodies are never read as calls; nested macros (`format!`) are skipped."""
    _nodes, edges = build_graph([_source("lib.rs", RUST_MACROS)], [])
    sites = {(e.metadata["lineno"], e.metadata["callee"]): e for e in calls(edges)}

    matches = sites[(9, "matches")]
    assert matches.source == "symbol:lib.rs:check" and matches.metadata["in_macro"] is True
    assert matches.metadata["receiver_type"] == "Version"
    assert matches.target == "symbol:lib.rs:matches"
    assert sites[(9, "parse")].target == "symbol:lib.rs:parse"
    assert sites[(10, "helper")].target == "symbol:lib.rs:helper"
    callees = {callee for _line, callee in sites}
    assert not {"format", "println", "not_a_call", "derive", "Debug"} & callees
