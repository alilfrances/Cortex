"""Deterministic, indexed, repo-wide resolution of `calls` edges.

Call-site metadata contract
---------------------------
Every extractor that emits a `calls` edge for call-graph resolution writes::

    source        enclosing symbol id (``file:<path>`` at file scope)
    target        ``name:<callee>``
    relation      "calls", layer "STRUCTURAL"
    metadata      lineno, source_file, callee,
                  receiver        str | None   receiver text as written
                  receiver_kind   "none" | "this" | "self" | "scoped" | "var" | "new" | "chain"
                  scope           str | None   ``Cls`` of ``Cls::m`` / alias of ``pkg.m``
                  receiver_type   str | None   declared type name when statically known
                  receiver_base_type  str | None  (C/C++ ``chain`` only) declared type of the chain's leftmost
                                  identifier when it is a parameter or a local: ``item`` of ``item.m_owner->f()``
                                  (``Item *item``), ``ctx`` of ``ctx->a()->f()``; absent for ``this``, a member
                                  and a free function's result
                  enclosing_class str | None   class the call site sits in
    optional      scope_module  (Python: dotted module a scoped call refers to)
                  import_module / alias  (Python: ``from m import f as g``)
                  super         (Python: ``super().m()`` skips the class's own members)

`name:`-targeted edges whose metadata lacks ``receiver_kind`` (QML calls, edges
written before the contract) are never touched here.

Resolution
----------
`resolve_call_endpoints` builds hash indexes once (O(nodes + edges)) and then
resolves each call edge from its metadata alone -- never from its previous
target -- so the result is the same for a full ingest and for an incremental
re-resolution, and an edge whose previous target disappeared (or became
ambiguous) is reset first.  Tiers, in order, stopping at the first that yields
at least one symbol::

    scoped         ``Cls::m`` / ``pkg.m`` / module-alias calls
    this           ``this``/``self``/implicit-this -> enclosing class members
    declared_type  receiver whose declared/constructed type is known
    inherited      the above, via ``inherits`` edges (BFS, depth <= 6)
    same_file      free callee defined in the caller's file
    same_package   Go only: free callee in the caller's directory (package)
    import_scoped  free callee defined in a file the caller imports/includes
    repo_unique    free callee with exactly one definition repo-wide
    chain          C/C++ only: a call on a call's result (``a->b()->c()``), typed by the return type of the
                   inner call's resolved target (or by a declared member of that type); a pass of its own,
                   after every other edge of the family, so the inner edges are already resolved

More than one candidate at the deciding tier keeps the ``name:`` target and
records ``candidates`` (sorted, <= 8), ``ambiguous = True``, confidence LOW.
A C/C++ edge that depends on a class lookup and stayed unresolved or ambiguous gets one second
attempt in which ``_Index.prefer`` also narrows same-label classes by the include ring of the calling
file (``MAX_INCLUDE_RING`` hops; then of its single header sibling); only an answer with exactly
one candidate replaces the first outcome.  Out-of-line members are found by the class label, so once the
lookup is down to one of several same-label classes `_Index.class_members` keeps only the definitions that
belong to it (its file, its implementation sibling, its include ring) and resolves when exactly one remains.
Nothing is ever resolved by scanning the corpus per candidate.
"""

from __future__ import annotations

import builtins
import functools
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Iterable

from .externals import CPP_FAMILY, QML_FAMILY, classify_base, classify_call
from .models import GraphEdge, GraphNode
from .testpatterns import is_test_path

MAX_CANDIDATES = 8
MAX_INHERIT_DEPTH = 6
# How many include hops `_Index.include_ring` follows from the calling file and from its header sibling.
MAX_INCLUDE_RING = 3
# `call_sites._MAX_RECEIVER`: a receiver text this long was cut, so its tail is not the chain's tail.
_MAX_RECEIVER_TEXT = 80
# The longest C/C++ chain `a->b()->c()->d()` (calls in it, the edge's own included) that is followed.
MAX_CHAIN_LINKS = 4
_HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx"}
_IMPL_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_PY_BUILTINS = frozenset(dir(builtins))
# `preferred` is written when locality step 2 ("production over mock") decided the edge: the call graph keeps
# that answer, `impacted_tests` does not walk it (a test target may compile the mock instead).
# `external` (see `externals`) marks an `inherits` base or a `calls` callee that is outside the repository;
# the target is unchanged.
_RESOLUTION_KEYS = ("resolution", "candidates", "ambiguous", "preferred", "external")


def is_contract_call(edge: GraphEdge) -> bool:
    return (
        edge.relation == "calls"
        and isinstance(edge.metadata, dict)
        and "receiver_kind" in edge.metadata
        and bool(edge.metadata.get("callee"))
        # Regex-fallback calls are name-only by contract (LOW, no typing): the
        # resolver must never promote them to a symbol id.
        and not edge.metadata.get("name_only")
    )


_FAMILIES = {
    ".c": "c", ".h": "c", ".cpp": "c", ".cc": "c", ".cxx": "c", ".hpp": "c", ".hh": "c", ".hxx": "c",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".py": "py", ".go": "go", ".rs": "rs", ".swift": "swift", ".java": "java", ".rb": "rb",
}


@functools.lru_cache(maxsize=65536)
def language_family(path: str) -> str:
    """Resolution never crosses language families: a Java call is never matched against a
    Swift or Rust symbol that happens to share a name (each family gets its own index)."""
    suffix = PurePosixPath(path).suffix.lower()
    return _FAMILIES.get(suffix, suffix or "?")


def _edge_path(edge: GraphEdge) -> str:
    if edge.source.startswith("file:"):
        return edge.source.removeprefix("file:")
    if edge.source.startswith("symbol:"):
        return edge.source.removeprefix("symbol:").partition(":")[0]
    source_file = edge.metadata.get("source_file") if isinstance(edge.metadata, dict) else None
    return str(source_file or "")


def _symbol_path(node_id: str) -> tuple[str, str]:
    """``symbol:<path>:<qualname>`` -> (path, qualname)."""
    rest = node_id.removeprefix("symbol:")
    path, _, qual = rest.partition(":")
    return path, qual


def _last_segment(text: str) -> str:
    text = text.split("<", 1)[0]
    return text.replace("::", ".").rsplit(".", 1)[-1].strip()


def _base_label(target: str) -> str:
    """Class label named by an ``inherits`` target (`name:Ns::Base`, `module:Base`,
    `symbol:path:Base`)."""
    if target.startswith("symbol:"):
        return _last_segment(_symbol_path(target)[1])
    _, _, rest = target.partition(":")
    return _last_segment(rest)


def _inherits_label(edge: GraphEdge) -> str:
    """Class label an `inherits` edge names, also after its target was rewritten to a symbol id."""
    original = edge.metadata.get("unresolved_target") if isinstance(edge.metadata, dict) else None
    return _base_label(str(original or edge.target))


# Directories whose classes are stand-ins for the real ones (the test rules in `testpatterns` cover
# `tests/`, `test/`, `spec/`, ...).
_AUXILIARY_DIRS = frozenset({
    "mock", "mocks", "stub", "stubs", "fake", "fakes", "fixture", "fixtures", "testdata", "testing",
    "example", "examples", "sample", "samples", "benchmark", "benchmarks",
})


def _is_auxiliary(path: str) -> bool:
    """A test, mock, stub or fixture file: never the intended base of production code."""
    return is_test_path(path) or any(part.lower() in _AUXILIARY_DIRS for part in PurePosixPath(path).parts[:-1])


def _dotted_suffixes(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


class ClassIndex:
    """Class structure of a set of symbol nodes: which class holds which member.

    Membership is span containment (innermost class), with a dotted-qualname fallback for Python
    nodes that carry no span; out-of-line definitions (`Foo::bar` in a .cpp) are found through the
    `qualifier` metadata.  The call resolver builds it for the whole graph (`_Index`); the query
    side builds it for the few classes of a hierarchy (`overrides.find_overriders`).
    """

    def __init__(self, nodes: Iterable[GraphNode]) -> None:
        symbols: dict[str, GraphNode] = {}
        for node in nodes:
            if node.granularity == "symbol":
                symbols.setdefault(node.node_id, node)
        self.symbols = symbols
        self.by_path_qual: dict[tuple[str, str], GraphNode] = {}
        self.classes_by_label: defaultdict[str, list[str]] = defaultdict(list)
        per_file: defaultdict[str, list[GraphNode]] = defaultdict(list)
        for node_id in sorted(symbols):
            node = symbols[node_id]
            _, qual = _symbol_path(node_id)
            self.by_path_qual[(node.source_ref, qual)] = node
            if node.kind == "class":
                self.classes_by_label[node.label].append(node_id)
            per_file[node.source_ref].append(node)

        # Class membership: span containment (innermost class), with a
        # dotted-qualname fallback for Python nodes that carry no span.
        self.container: dict[str, str] = {}
        for path, items in per_file.items():
            spanned = [n for n in items if n.span_start is not None]
            spanned.sort(key=lambda n: (n.span_start, -(n.span_end or n.span_start), n.kind != "class", n.node_id))
            stack: list[GraphNode] = []
            for node in spanned:
                end = node.span_end or node.span_start
                while stack and (stack[-1].span_end or stack[-1].span_start) < node.span_start:
                    stack.pop()
                if stack and stack[-1].node_id != node.node_id:
                    top = stack[-1]
                    if top.span_start <= node.span_start and end <= (top.span_end or top.span_start):
                        self.container[node.node_id] = top.node_id
                if node.kind == "class":
                    stack.append(node)
            if path.endswith(".py"):
                for node in items:
                    if node.node_id in self.container:
                        continue
                    _, qual = _symbol_path(node.node_id)
                    parent, dot, _ = qual.rpartition(".")
                    if dot:
                        owner = self.by_path_qual.get((path, parent))
                        if owner is not None and owner.kind == "class":
                            self.container[node.node_id] = owner.node_id

        self.declared_members: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.qualified_defs: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for node_id in sorted(symbols):
            node = symbols[node_id]
            owner = self.container.get(node_id)
            qualifier = node.metadata.get("qualifier") if isinstance(node.metadata, dict) else None
            if owner is not None:
                self.declared_members[owner][node.label].append(node_id)
            if isinstance(qualifier, str) and qualifier:
                self.qualified_defs[_last_segment(qualifier)][node.label].append(node_id)

    def is_namespace(self, class_id: str) -> bool:
        node = self.symbols.get(class_id)
        return node is not None and node.signature.lstrip().startswith("namespace")

    def class_members(self, class_id: str, name: str) -> list[str]:
        declared = self.declared_members.get(class_id, {}).get(name)
        if declared:
            return declared
        label = self.symbols[class_id].label
        return self.qualified_defs.get(label, {}).get(name, [])


def _common_prefix(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Number of leading path segments two directories share."""
    count = 0
    for left, right in zip(a, b):
        if left != right:
            break
        count += 1
    return count


class _Index(ClassIndex):
    """All hash indexes the resolver needs, built in one pass over the graph."""

    def __init__(self, nodes: Iterable[GraphNode], edges: list[GraphEdge]) -> None:
        nodes = list(nodes)
        # Reasons the edge being resolved was narrowed ("production": locality step 2); set only by `_resolve_into`.
        self.trace: set[str] | None = None
        self.node_ids: set[str] = set()
        py_files: list[str] = []
        stems: defaultdict[str, list[str]] = defaultdict(list)
        for node in nodes:
            self.node_ids.add(node.node_id)
            if node.granularity != "symbol" and node.kind == "file":
                if node.source_ref.endswith(".py"):
                    py_files.append(node.source_ref)
                pure = PurePosixPath(node.source_ref)
                stems[pure.stem].append(node.source_ref)
        # A header's implementation files (same stem, any directory: `include/x.hpp` / `src/x.cpp`): an include
        # of `util.hpp` makes definitions in `util.cpp` reachable.
        self.impl_siblings: dict[str, list[str]] = {}
        for paths in stems.values():
            headers = [p for p in paths if PurePosixPath(p).suffix.lower() in _HEADER_SUFFIXES]
            impls = sorted(p for p in paths if PurePosixPath(p).suffix.lower() in _IMPL_SUFFIXES)
            if impls:
                for header in headers:
                    self.impl_siblings[header] = impls
        # The header(s) an implementation file is the sibling of (the inverse of `impl_siblings`).
        self.header_siblings: dict[str, list[str]] = {}
        for header, impls in self.impl_siblings.items():
            for impl in impls:
                self.header_siblings.setdefault(impl, []).append(header)
        # Set while `_resolve_into` makes its second attempt at an edge that stayed unresolved or ambiguous:
        # `prefer` then also narrows by `include_ring`.
        self.deep = False
        # The family's call edges by (source, lineno, callee), for the chain pass (see `attach_calls`), and the
        # return type label of a function symbol, parsed from its signature on first use (`return_label`).
        self._calls: list[GraphEdge] = []
        self._call_sites: dict[tuple[str, int, str], list[GraphEdge]] | None = None
        self._return_labels: dict[str, str | None] = {}
        self._rings: dict[tuple[str, ...], frozenset[str]] = {}
        super().__init__(nodes)
        symbols = self.symbols

        # Declared member variable types recorded on class nodes (`member_types`
        # metadata), so `m_model->f()` in an out-of-line `Foo::bar()` can be typed
        # from the header that declares `m_model`.
        self.member_types: dict[str, dict[str, str]] = {}
        for node_id, node in symbols.items():
            mt = node.metadata.get("member_types") if isinstance(node.metadata, dict) else None
            if node.kind == "class" and isinstance(mt, dict) and mt:
                self.member_types[node_id] = {str(k): str(v) for k, v in mt.items()}

        self.free_by_label: defaultdict[str, list[str]] = defaultdict(list)
        # `free_by_path_label` feeds same-file lookups (nested defs are visible
        # there, subject to lexical scope); `exported_by_path_label` and
        # `free_by_label` feed cross-file tiers and exclude Python functions
        # nested in other functions, which no other file can name.
        self.free_by_path_label: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        self.exported_by_path_label: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        for node_id in sorted(symbols):
            node = symbols[node_id]
            owner = self.container.get(node_id)
            qualifier = node.metadata.get("qualifier") if isinstance(node.metadata, dict) else None
            namespaced = owner is not None and self.is_namespace(owner)
            if (owner is None or namespaced) and not (isinstance(qualifier, str) and qualifier):
                self.free_by_path_label[(node.source_ref, node.label)].append(node_id)
                if not (node.source_ref.endswith(".py") and "." in _symbol_path(node_id)[1]):
                    self.free_by_label[node.label].append(node_id)
                    self.exported_by_path_label[(node.source_ref, node.label)].append(node_id)

        # Every name the family defines as a free function, a class or an out-of-line member: a free call to
        # one of them is never classified as external (`min`, `close`).
        self.defined_names: set[str] = set(self.free_by_label) | set(self.classes_by_label)
        for members in self.qualified_defs.values():
            self.defined_names.update(members)

        self.free_by_dir_label: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
        for (path, label), ids in self.exported_by_path_label.items():
            self.free_by_dir_label[(str(PurePosixPath(path).parent), label)].extend(ids)

        imports: defaultdict[str, set[str]] = defaultdict(set)
        module_imports: defaultdict[str, set[str]] = defaultdict(set)
        inherits: list[GraphEdge] = []
        for edge in edges:
            if edge.relation == "imports" and edge.source.startswith("file:"):
                if edge.target.startswith("file:"):
                    imports[edge.source.removeprefix("file:")].add(edge.target.removeprefix("file:"))
                elif edge.target.startswith("module:"):
                    module_imports[edge.source.removeprefix("file:")].add(edge.target.removeprefix("module:"))
            elif edge.relation == "inherits" and edge.source in symbols:
                inherits.append(edge)
        self.imports: dict[str, list[str]] = {path: sorted(targets) for path, targets in imports.items()}
        # What a file imports that did not resolve to a file of the repository (`#include <gtest/gtest.h>`,
        # `import QtQuick 2.15`), as the extractor wrote it; `classify_base` reads it.
        self.module_imports: dict[str, list[str]] = {path: sorted(targets) for path, targets in module_imports.items()}
        self._file_dirs = {str(PurePosixPath(n.source_ref).parent) for n in nodes if n.kind == "file" and n.source_ref}
        self._dir_suffixes: set[str] | None = None

        self.module_exact: dict[str, list[str]] = {}
        self.module_suffix: defaultdict[str, list[str]] = defaultdict(list)
        for path in sorted(set(py_files)):
            suffixes = _dotted_suffixes(path)
            if suffixes:
                self.module_exact.setdefault(suffixes[0], []).append(path)
            for dotted in suffixes:
                self.module_suffix[dotted].append(path)

        bases: defaultdict[str, set[str]] = defaultdict(set)
        # edge_id -> the class ids its base label names after `prefer` (per edge: the rewrite of
        # `inherits` targets needs the answer for one edge, `bases` unions a class's edges).
        self.edge_bases: dict[str, list[str]] = {}
        # Classes with a base that no class of the family carries the label of (`QWidget`, a library class):
        # a member the repository cannot find on them may well be inherited from it.
        self.outside_base_classes: set[str] = set()
        self._outside_base_cache: dict[str, bool] = {}
        for edge in sorted(inherits, key=lambda e: e.edge_id):
            if edge.target in symbols and symbols[edge.target].kind == "class":
                bases[edge.source].add(edge.target)
                continue
            found = self.base_candidates(edge.source, _inherits_label(edge))
            self.edge_bases[edge.edge_id] = found
            bases[edge.source].update(found)
            if not found:
                self.outside_base_classes.add(edge.source)
        for cid, node in symbols.items():
            declared = node.metadata.get("bases") if isinstance(node.metadata, dict) else None
            if node.kind == "class" and isinstance(declared, list) and not bases.get(cid):
                for base in declared:
                    found = self.classes_by_label.get(_last_segment(str(base)), [])
                    if len(found) > 1:
                        found = self.prefer(found, node.source_ref)
                    bases[cid].update(found)
        self.bases: dict[str, list[str]] = {cid: sorted(ids - {cid}) for cid, ids in bases.items()}

    def class_members(self, class_id: str, name: str) -> list[str]:
        """`ClassIndex.class_members`, with the out-of-line definitions narrowed to the chosen class.

        An out-of-line member is found by the class *label*, so two same-label classes (`Bar` in two modules)
        each list the definitions of both.  Once the lookup is down to one class, keep the definitions that
        belong to it (`own_definitions`); anything but exactly one answer leaves today's list."""
        found = super().class_members(class_id, name)
        if len(found) < 2 or self.declared_members.get(class_id, {}).get(name) or self.is_namespace(class_id):
            return found
        if len(self.classes_by_label.get(self.symbols[class_id].label, ())) < 2:
            return found
        own = self.own_definitions(class_id, found)
        return own if len(own) == 1 else found

    def own_definitions(self, class_id: str, definitions: list[str]) -> list[str]:
        """The `definitions` (out-of-line members found by the class label) whose file belongs to `class_id`.

        Successive narrowing, each step applying only if it leaves one or more definitions (the shape of
        `locality`), stopping at one:
        1. the class's own file;
        2. its implementation sibling (`impl_siblings` of its header, `narrow`ed to the header's directory);
        3. a file in the include ring of the class's file, in either direction (the `.inl` its header includes,
           the `.cpp` that includes it).
        """
        class_file = self.symbols[class_id].source_ref
        pool = list(definitions)
        siblings = set(self.impl_siblings.get(class_file, ()))
        class_ring = self.include_ring([class_file])
        steps = (
            lambda ids: [d for d in ids if self.symbols[d].source_ref == class_file],
            lambda ids: self.narrow([d for d in ids if self.symbols[d].source_ref in siblings], class_file),
            lambda ids: [
                d for d in ids
                if self.symbols[d].source_ref in class_ring or class_file in self.include_ring([self.symbols[d].source_ref])
            ],
        )
        for step in steps:
            narrowed = step(pool)
            if narrowed:
                pool = narrowed
            if len(pool) == 1:
                break
        return pool

    def attach_calls(self, calls: list[GraphEdge]) -> None:
        """The family's contract call edges, resolved or not: the chain pass looks up the inner call of
        ``a->b()->c()`` among them (same source symbol, same line, the callee ``b``)."""
        self._calls = calls
        self._call_sites = None

    def calls_at(self, source: str, lineno: int, callee: str) -> list[GraphEdge]:
        if self._call_sites is None:
            sites: dict[tuple[str, int, str], list[GraphEdge]] = defaultdict(list)
            for edge in self._calls:
                sites[(edge.source, int(edge.metadata.get("lineno") or 0), str(edge.metadata["callee"]))].append(edge)
            self._call_sites = sites
        return self._call_sites.get((source, lineno, callee), [])

    def return_label(self, symbol_id: str) -> str | None:
        """Class label a function symbol returns, from its signature (``Item* Manager::current()`` -> ``Item``);
        ``None`` for a constructor, ``void``, ``auto`` and the built-in types."""
        if symbol_id not in self._return_labels:
            node = self.symbols.get(symbol_id)
            self._return_labels[symbol_id] = (
                _return_label(node.signature, node.label) if node is not None and node.kind != "class" else None
            )
        return self._return_labels[symbol_id]

    def has_outside_base(self, class_id: str) -> bool:
        """`class_id` or a class it derives from (repository bases, depth <= `MAX_INHERIT_DEPTH`) has a base
        outside the repository."""
        cached = self._outside_base_cache.get(class_id)
        if cached is None:
            frontier, visited, depth, cached = [class_id], {class_id}, 0, False
            while frontier and depth <= MAX_INHERIT_DEPTH and not cached:
                cached = any(cid in self.outside_base_classes for cid in frontier)
                frontier = sorted({b for cid in frontier for b in self.bases.get(cid, ()) if b not in visited})
                visited.update(frontier)
                depth += 1
            self._outside_base_cache[class_id] = cached
        return cached

    def is_repo_module(self, module: str) -> bool:
        """A QML import that names a directory of the repository (``import "components"``, ``import App.Ui``)."""
        if self._dir_suffixes is None:
            suffixes: set[str] = set()
            for directory in self._file_dirs:
                parts = PurePosixPath(directory).parts
                suffixes.update("/".join(parts[i:]) for i in range(len(parts)))
            self._dir_suffixes = suffixes
        text = module.strip("\"'")
        parts = [p for p in (text.split("/") if "/" in text or text.startswith(".") else text.split(".")) if p not in ("", ".", "..")]
        return "/".join(parts) in self._dir_suffixes

    def external_imports(self, path: str, family: str) -> list[str]:
        """The imports of `path` that can make one of its bases external: the headers it includes and the
        modules it imports, minus (for QML) the modules that are directories of the repository."""
        modules = self.module_imports.get(path, [])
        if family == QML_FAMILY:
            return [m for m in modules if not self.is_repo_module(m)]
        return [*self.imports.get(path, ()), *modules]

    def base_candidates(self, class_id: str, label: str) -> list[str]:
        """The classes labelled `label` that `class_id` can derive from: not itself, narrowed by `prefer`."""
        candidates = [c for c in self.classes_by_label.get(label, []) if c != class_id]
        if len(candidates) > 1:
            candidates = self.prefer(candidates, self.symbols[class_id].source_ref)
        return candidates

    def narrow(self, candidate_ids: list[str], from_path: str) -> list[str]:
        """Same file, else same directory (package/module), else unchanged."""
        if len(candidate_ids) < 2:
            return list(candidate_ids)
        same = [c for c in candidate_ids if self.symbols[c].source_ref == from_path]
        if same:
            return same
        here = str(PurePosixPath(from_path).parent)
        near = [c for c in candidate_ids if str(PurePosixPath(self.symbols[c].source_ref).parent) == here]
        return near or list(candidate_ids)

    def reachable(self, from_path: str) -> list[str]:
        """Files the caller imports/includes (one hop), plus the implementation
        files of every imported header."""
        paths = list(self.imports.get(from_path, ()))
        extra = [impl for path in paths for impl in self.impl_siblings.get(path, ())]
        return sorted(set(paths) | set(extra))

    # -- candidate narrowing ------------------------------------------------

    def prefer(self, candidate_ids: list[str], from_path: str) -> list[str]:
        """Same file first, then files the caller imports, then the likeliest real definition; else unchanged.

        The last step is what separates the real class from the stand-ins that share its label in a
        large repository (mocks and fixtures, a class nested in an unrelated .cpp, a class of another
        language), see `locality`.
        """
        if len(candidate_ids) < 2:
            return candidate_ids
        same = [c for c in candidate_ids if self.symbols[c].source_ref == from_path]
        if same:
            return same
        imported = set(self.reachable(from_path))
        near = [c for c in candidate_ids if self.symbols[c].source_ref in imported]
        if near and len(near) < len(candidate_ids) and self.trace is not None and not _is_auxiliary(from_path):
            # A same-named mock header can replace the included one on a test target's include path
            # (review 0.13.0 section C), so impacted_tests must not walk this edge; the call graph keeps the answer.
            kept = {PurePosixPath(self.symbols[c].source_ref).name for c in near}
            if any(
                c not in near
                and _is_auxiliary(self.symbols[c].source_ref)
                and PurePosixPath(self.symbols[c].source_ref).name in kept
                for c in candidate_ids
            ):
                self.trace.add("production")
        result = self.locality(near or candidate_ids, from_path)
        if self.deep:
            siblings = self.header_siblings.get(from_path, ())
            for ring in (self.include_ring([from_path]), self.include_ring(siblings) if len(siblings) == 1 else frozenset()):
                if len(result) < 2:
                    break
                inside = [c for c in result if self.symbols[c].source_ref in ring]
                if inside and len(inside) < len(result):
                    result = self.locality(inside, from_path)
        return result

    def include_ring(self, roots: Iterable[str]) -> frozenset[str]:
        """The `roots` and the files within `MAX_INCLUDE_RING` include hops of them.  `prefer` tries the calling
        file first, then its header sibling (`foo.cpp` -> `foo.h`: what the header includes the implementation
        sees too; only when exactly one header carries the stem, otherwise it is a guess)."""
        key = tuple(sorted(roots))
        ring = self._rings.get(key)
        if ring is None:
            seen = set(key)
            frontier = sorted(seen)
            for _ in range(MAX_INCLUDE_RING):
                frontier = sorted({t for path in frontier for t in self.imports.get(path, ()) if t not in seen})
                seen.update(frontier)
                if not frontier:
                    break
            ring = self._rings[key] = frozenset(seen)
        return ring

    def locality(self, candidate_ids: list[str], from_path: str) -> list[str]:
        """Narrow same-label classes that no include relation separates; each step applies only if it leaves one.

        1. the caller's language family (a JS helper is not the base of a C++ class);
        2. outside test/mock/fixture trees when the caller itself is production code;
        3. declared in a header, not nested in an implementation file (a .cpp-local class is not
           visible to any other file);
        4. the nearest directory: the longest common directory prefix (in path segments) with the
           caller's, so a mock base one directory above its users beats a production header far away.

        Steps 3 and 4 are skipped when every candidate is a namespace: a namespace is split over
        its header and the .cpp files that define its members, so no one node is "the" declaration
        (see `namespace_members`).
        """
        pool = list(candidate_ids)
        if len(pool) < 2:
            return pool
        production_only = (
            (lambda c: not _is_auxiliary(self.symbols[c].source_ref)) if not _is_auxiliary(from_path) else None
        )
        steps = [
            lambda c: language_family(self.symbols[c].source_ref) == language_family(from_path),
            production_only,
            lambda c: PurePosixPath(self.symbols[c].source_ref).suffix.lower() in _HEADER_SUFFIXES,
        ]
        namespaces = all(self.is_namespace(c) for c in pool)
        for keep in steps[:2] if namespaces else steps:
            if keep is None:
                continue
            narrowed = [c for c in pool if keep(c)]
            if narrowed:
                if keep is production_only and len(narrowed) < len(pool) and self.trace is not None:
                    self.trace.add("production")
                pool = narrowed
            if len(pool) == 1:
                return pool
        if namespaces:
            return pool
        # Nearest = longest shared prefix, then fewest directories below it: from `src/a.cpp`,
        # `src/b.h` beats `src/x/b.h` as the old same-directory step did.
        caller = PurePosixPath(from_path).parent.parts
        nearness = {}
        for c in pool:
            parts = PurePosixPath(self.symbols[c].source_ref).parent.parts
            shared = _common_prefix(parts, caller)
            nearness[c] = (shared, shared - len(parts))
        nearest = max(nearness.values())
        return [c for c in pool if nearness[c] == nearest]

    def namespace_members(self, label: str, callee: str, source_file: str) -> list[str]:
        """Members `callee` of every namespace node labelled `label`, narrowed like `prefer`.

        A namespace is one entity spread over several nodes (the header's `namespace Ns {}` and one
        per .cpp that defines members), so the candidate classes cannot be narrowed first: the
        header-only node would hide the members that live in the .cpp ones.
        """
        pool = [c for c in self.classes_by_label.get(label, []) if self.is_namespace(c)]
        if not _is_auxiliary(source_file):
            production = [c for c in pool if not _is_auxiliary(self.symbols[c].source_ref)]
            if production and len(production) < len(pool) and self.trace is not None:
                self.trace.add("production")  # the same rule as `locality` step 2
            pool = production or pool
        members = sorted({m for ns in pool for m in self.class_members(ns, callee)})
        if len(members) > 1:
            members = self.prefer(members, source_file)
        return members

    def module_paths(self, dotted: str) -> list[str]:
        exact = self.module_exact.get(dotted)
        if exact:
            return exact
        return self.module_suffix.get(dotted, [])

    def collapse(self, candidate_ids: list[str]) -> list[str]:
        """A header declaration and its same-stem implementation file name one
        entity: keep the declaring (header) symbol."""
        if len(candidate_ids) < 2:
            return candidate_ids
        headers: dict[tuple[str, str], str] = {}
        for cid in candidate_ids:
            path = PurePosixPath(self.symbols[cid].source_ref)
            if path.suffix.lower() in _HEADER_SUFFIXES:
                headers[(str(path.parent), path.stem)] = cid
        if not headers:
            return candidate_ids
        kept = []
        for cid in candidate_ids:
            path = PurePosixPath(self.symbols[cid].source_ref)
            if path.suffix.lower() not in _HEADER_SUFFIXES and (str(path.parent), path.stem) in headers:
                continue
            kept.append(cid)
        return kept

    # -- member lookup ------------------------------------------------------

    def lookup(self, class_ids: list[str], name: str, *, skip_direct: bool = False) -> tuple[str, list[str]] | None:
        """Members named `name` of `class_ids`; then of their bases level by
        level (BFS, depth <= MAX_INHERIT_DEPTH, sorted, visited set)."""
        frontier = sorted(set(class_ids))
        visited = set(frontier)
        depth = 0
        while frontier and depth <= MAX_INHERIT_DEPTH:
            if not (skip_direct and depth == 0):
                found = sorted({m for cid in frontier for m in self.class_members(cid, name)})
                if found:
                    return ("direct" if depth == 0 else "inherited"), found
            next_frontier: list[str] = []
            for cid in frontier:
                for base in self.bases.get(cid, ()):
                    if base not in visited:
                        visited.add(base)
                        next_frontier.append(base)
            frontier = sorted(next_frontier)
            depth += 1
        return None


def _call_source_file(edge: GraphEdge, index: _Index) -> str:
    source_file = edge.metadata.get("source_file")
    if isinstance(source_file, str) and source_file:
        return source_file
    if edge.source.startswith("file:"):
        return edge.source.removeprefix("file:")
    node = index.symbols.get(edge.source)
    return node.source_ref if node is not None else ""


def _reset(edge: GraphEdge) -> None:
    md = edge.metadata
    edge.target = f"name:{md['callee']}"
    for key in _RESOLUTION_KEYS:
        md.pop(key, None)
    previous = md.pop("extracted_confidence", None)
    if previous is not None:
        edge.confidence = previous


_Outcome = tuple[str, list[str]] | None  # (tier, candidate ids) -- None = unresolved


def _enclosing_classes(edge: GraphEdge, index: _Index, source_file: str) -> list[str]:
    name = edge.metadata.get("enclosing_class")
    if not name:
        return []
    direct = index.by_path_qual.get((source_file, str(name)))
    if direct is not None and direct.kind == "class":
        return [direct.node_id]
    return index.prefer(index.classes_by_label.get(_last_segment(str(name)), []), source_file)


def _member_type_candidates(edge: GraphEdge, index: _Index, source_file: str, member: str) -> list[str]:
    """Declared types of member variable `member` at the nearest level (enclosing class, then bases) that
    declares it: sorted, empty when no class declares it, more than one when the declarations disagree."""
    classes = _enclosing_classes(edge, index, source_file)
    return _member_labels(index, classes, member) if classes else []


def _member_type(edge: GraphEdge, index: _Index, source_file: str, member: str) -> str | None:
    """Declared type of member variable `member` of the enclosing class (or a base)."""
    found = _member_type_candidates(edge, index, source_file, member)
    return found[0] if len(found) == 1 else None


# -- chains: ``a->b()->c()`` ---------------------------------------------------------------------------

_TYPE_KEYWORDS = frozenset({
    "static", "virtual", "inline", "constexpr", "consteval", "constinit", "explicit", "const", "volatile", "friend",
    "extern", "typename", "mutable", "struct", "class", "enum", "union", "register", "thread_local",
})
_BUILTIN_TYPES = frozenset({
    "void", "auto", "bool", "char", "wchar_t", "char8_t", "char16_t", "char32_t", "short", "int", "long", "float",
    "double", "signed", "unsigned", "size_t", "ssize_t", "ptrdiff_t", "nullptr_t",
})
_MACRO_WORD = re.compile(r"[A-Z][A-Z0-9_]{2,}$")
_TRAILING_QUALIFIER = re.compile(r"(?:\s*[*&]+|\s*\b(?:const|volatile))\s*$")
_LEADING_WORD = re.compile(r"(\w+)\s+(?=\S)")
_CALL_NAME = re.compile(r"([A-Za-z_]\w*)\s*(?:<[^<>]*>)?\s*$")


def _matching(text: str, start: int, opening: str, closing: str, step: int = 1) -> int | None:
    """Index of the bracket that closes the one at `start` (scanning forward) or opens it (backward, `step=-1`)."""
    depth = 0
    index = start
    while 0 <= index < len(text):
        char = text[index]
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += step
    return None


def _type_label(text: str) -> str | None:
    """Class label a written type names: pointers, references and qualifiers stripped, the last `::` segment,
    the pointee of a known smart pointer (``std::unique_ptr<Item>``), the wrapper's own name for any other
    template (``QList<Item*>`` -> ``QList``); ``None`` for a built-in or anything that is not one name."""
    from .structural.call_sites import _SMART_POINTERS

    text = text.strip()
    while True:
        lead = _LEADING_WORD.match(text)
        if lead and (lead.group(1) in _TYPE_KEYWORDS or _MACRO_WORD.match(lead.group(1))):
            text = text[lead.end():]
            continue
        break
    while True:
        stripped = _TRAILING_QUALIFIER.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = text.strip().removeprefix("::")
    segments: list[str] = []  # split on `::` outside template arguments
    depth, start = 0, 0
    for position, char in enumerate(text):
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        elif char == ":" and depth == 0 and text.startswith("::", position):
            segments.append(text[start:position])
            start = position + 2
    segments.append(text[start:])
    label, arguments = segments[-1].strip(), ""
    angle = label.find("<")
    if angle != -1:
        close = _matching(label, angle, "<", ">")
        if close is None or label[close + 1:].strip():
            return None
        label, arguments = label[:angle].strip(), label[angle + 1:close]
    if not re.fullmatch(r"[A-Za-z_]\w*", label) or label in _BUILTIN_TYPES or label in _TYPE_KEYWORDS:
        return None
    if arguments and label in _SMART_POINTERS:
        depth, first = 0, []
        for char in arguments:
            depth += char == "<"
            depth -= char == ">"
            if char == "," and depth == 0:
                break
            first.append(char)
        return _type_label("".join(first))
    return label


def _return_label(signature: str, name: str) -> str | None:
    """Class label named by the return type in a function signature: what stands before ``[Class::]name(``,
    without storage and specifier keywords, export macros, ``template <...>``, ``*``, ``&`` and the qualifier."""
    if not name or name.startswith("operator"):
        return None
    line = signature.split("\n", 1)[0]
    match = re.search(r"(?<![\w~])(?:~?\w+(?:<[^<>]*>)?\s*::\s*)*" + re.escape(name) + r"\s*\(", line)
    if match is None:
        return None
    prefix = re.sub(r"\[\[.*?\]\]", " ", line[:match.start()]).strip()
    while prefix.startswith("template"):
        angle = prefix.find("<")
        close = _matching(prefix, angle, "<", ">") if angle != -1 else None
        if close is None:
            return None
        prefix = prefix[close + 1:].strip()
    return _type_label(prefix) if prefix else None


def _chain_links(receiver: str) -> int:
    """Calls in a chain whose receiver text is `receiver`, the chain edge's own call included."""
    depth, groups = 0, 0
    for char in receiver:
        if char == "(":
            if depth == 0:
                groups += 1
            depth += 1
        elif char == ")":
            depth -= 1
    return groups + 1


def _chain_callees(receiver: str) -> set[str]:
    """Names called inside a chain's receiver text (``a->b()->c()`` -> ``b``, ``c``)."""
    return set(re.findall(r"([A-Za-z_]\w*)\s*(?:<[^<>]*>)?\s*\(", receiver))


def _member_labels(index: _Index, classes: list[str], member: str) -> list[str]:
    """Declared types of member variable `member` at the nearest level (`classes`, then their bases) that
    declares it: sorted, empty when none does, more than one when the declarations disagree."""
    frontier = sorted(set(classes))
    visited = set(frontier)
    depth = 0
    while frontier and depth <= MAX_INHERIT_DEPTH:
        found = sorted({index.member_types[c][member] for c in frontier if member in index.member_types.get(c, {})})
        if found:
            return found
        nxt: list[str] = []
        for cid in frontier:
            for base in index.bases.get(cid, ()):
                if base not in visited:
                    visited.add(base)
                    nxt.append(base)
        frontier = sorted(nxt)
        depth += 1
    return []


def _chain_classes(edge: GraphEdge, index: _Index, source_file: str, text: str) -> list[str]:
    """The repository classes the expression `text` (the receiver of a chain edge) evaluates to; empty when
    the expression is not typed.  Three forms, read from the end of the text:

    * ``<prefix>name(args)``: the inner call.  Its edge is the one of the same source symbol and line, callee
      `name` and receiver `prefix`; it must have resolved to one symbol, whose return type is the class;
    * ``<base>.field`` / ``<base>->field``: the declared type of member `field` in the classes `base` is;
    * ``name`` / ``this``: a member of the class the call sits in / that class."""
    text = text.strip()
    if not text:
        return []
    if text.endswith(")"):
        opening = _matching(text, len(text) - 1, ")", "(", -1)
        name = _CALL_NAME.search(text[:opening]) if opening else None
        if name is None:
            return []
        head = text[:name.start()].rstrip()
        prefix = head
        for separator in ("->", ".", "::"):
            if head.endswith(separator):
                prefix = head[: -len(separator)]
                break
        else:
            if head:
                return []  # `new Item()`, a cast, a subscript...: not a call on a named prefix
        wanted = "".join(prefix.split())
        found = [
            e for e in index.calls_at(edge.source, int(edge.metadata.get("lineno") or 0), name.group(1))
            if "".join(str(e.metadata.get("receiver") or "").split()) == wanted
        ]
        if len(found) != 1 or not found[0].target.startswith("symbol:") or found[0].metadata.get("ambiguous"):
            return []
        label = index.return_label(found[0].target)
        context = index.symbols[found[0].target].source_ref
        return index.prefer(index.classes_by_label.get(label, []), context) if label else []
    accessor = max(text.rfind("->"), text.rfind("."))
    if accessor == -1:
        if text == "this":
            return _enclosing_classes(edge, index, source_file)
        if not re.fullmatch(r"[A-Za-z_]\w*", text):
            return []
        # the leftmost link: a parameter or local (its declared type, which shadows a member of the same name),
        # else a member of the class the call sits in
        base = edge.metadata.get("receiver_base_type")
        labels = [str(base)] if base else _member_type_candidates(edge, index, source_file, text)
    else:
        field = text[accessor + (2 if text.startswith("->", accessor) else 1):].strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", field):
            return []
        classes = _chain_classes(edge, index, source_file, text[:accessor])
        labels = _member_labels(index, classes, field) if classes else []
    if len(labels) != 1:
        return []
    label = _last_segment(labels[0])
    return index.prefer(index.classes_by_label.get(label, []), source_file)


def _resolve_chain(edge: GraphEdge, index: _Index, source_file: str) -> _Outcome:
    """``a->b()->c()``: type the receiver by the chain's inner call (see `_chain_classes`) and look the callee
    up in that class and its bases.  Several candidates are ambiguous, as in the other tiers.  A class whose
    member is missing but that derives from something outside the repository marks the edge external
    (``inherited``), as for any other typed call."""
    md = edge.metadata
    receiver = str(md.get("receiver") or "")
    if not receiver or len(receiver) >= _MAX_RECEIVER_TEXT or _chain_links(receiver) > MAX_CHAIN_LINKS:
        return None
    classes = _chain_classes(edge, index, source_file, receiver)
    if not classes:
        return None
    outcome = _members(index, classes, index.symbols[classes[0]].label, str(md["callee"]), source_file, "chain")
    if outcome is None:
        if _inherited_from_outside(classes, index):
            md["external"] = "inherited"
        return None
    return "chain", outcome[1]


def _members(index: _Index, class_ids: list[str], label: str, callee: str, source_file: str, tier: str) -> _Outcome:
    """Members of `class_ids`; with no class node (Go types, Rust/Swift impl blocks whose type
    node lives elsewhere) fall back to methods whose `qualifier` names the type."""
    if class_ids:
        outcome = _tier_from_lookup(index.lookup(class_ids, callee), tier)
        if outcome is not None:
            return outcome
    virtual = index.qualified_defs.get(label, {}).get(callee)
    if virtual:
        return tier, index.narrow(list(virtual), source_file)
    return None


def _tier_from_lookup(found: tuple[str, list[str]] | None, direct_tier: str) -> _Outcome:
    if found is None:
        return None
    kind, ids = found
    return ("inherited" if kind == "inherited" else direct_tier), ids


def _resolve_scope_file(edge: GraphEdge, index: _Index) -> _Outcome:
    """`ns.f()` / `Cls.m()` where the extractor resolved the import binding to a file."""
    md = edge.metadata
    callee = str(md["callee"])
    if md.get("scope_dir"):  # Go `pkg.F()`: exported free symbols of the package directory
        found = list(index.free_by_dir_label.get((str(md["scope_dir"]), callee), ()))
        return ("scoped", found) if found else None
    path = md.get("scope_file")
    if not path:
        return None  # package / unresolved specifier: nothing in the repo to name
    if md.get("scope_namespace"):
        found = list(index.exported_by_path_label.get((str(path), callee), ()))
        return ("scoped", found) if found else None
    label = _last_segment(str(md.get("scope") or ""))
    class_ids = [c for c in index.classes_by_label.get(label, []) if index.symbols[c].source_ref == path]
    return _tier_from_lookup(index.lookup(class_ids, callee), "scoped") if class_ids else None


def _resolve_import_binding(edge: GraphEdge, index: _Index) -> _Outcome:
    """A free call/`new` whose callee is an explicitly imported binding: only the file the
    import names can define it (an explicit import beats same-file/repo-wide guesses)."""
    md = edge.metadata
    path = md.get("import_file")
    if not path:
        return None  # external package or unresolved specifier: stays name-based
    callee = str(md["callee"])
    found = list(index.exported_by_path_label.get((str(path), callee), ()))
    return ("import_scoped", found) if found else None


def _resolve_scoped(edge: GraphEdge, index: _Index, source_file: str) -> _Outcome:
    md = edge.metadata
    callee = str(md["callee"])
    if "scope_file" in md:
        return _resolve_scope_file(edge, index)
    scope_module = md.get("scope_module")
    scope = str(md.get("scope") or "")
    if scope_module:
        paths = index.module_paths(str(scope_module))
        if paths:
            found: list[str] = []
            for path in paths:
                found.extend(index.exported_by_path_label.get((path, callee), ()))
                if not found and path.endswith("__init__.py"):
                    # package re-export: one hop through what __init__ imports
                    for hop in index.imports.get(path, ()):
                        found.extend(index.exported_by_path_label.get((hop, callee), ()))
            return ("scoped", sorted(set(found))) if found else None
        parent, _, last = str(scope_module).rpartition(".")
        class_ids: list[str] = []
        if parent:
            for path in index.module_paths(parent):
                class_ids.extend(
                    cid for cid in index.classes_by_label.get(last, ()) if index.symbols[cid].source_ref == path
                )
        if class_ids:
            return _tier_from_lookup(index.lookup(sorted(set(class_ids)), callee), "scoped")
        return None  # module/class outside the repo (stdlib, third party)
    label = _last_segment(scope)
    labelled = index.classes_by_label.get(label, []) if label else []
    if labelled and all(index.is_namespace(c) for c in labelled):
        found = index.namespace_members(label, callee, source_file)
        return ("scoped", found) if found else None
    if label:
        class_ids = index.prefer(index.classes_by_label.get(label, []), source_file)
        return _members(index, class_ids, label, callee, source_file, "scoped")
    return None


def _resolve_free(edge: GraphEdge, index: _Index, source_file: str) -> _Outcome:
    md = edge.metadata
    callee = str(md["callee"])
    python = source_file.endswith(".py")

    same = list(index.free_by_path_label.get((source_file, callee), ()))
    if python and same:
        # lexical scoping: a module-level def, or one nested in the caller's scope chain
        caller = _symbol_path(edge.source)[1] if edge.source.startswith("symbol:") else ""
        same = [
            cid for cid in same
            if (parent := _symbol_path(cid)[1].rpartition(".")[0]) == ""
            or caller == parent
            or caller.startswith(parent + ".")
        ]
    if same:
        return "same_file", index.collapse(same)

    if md.get("package_scoped"):
        # Go: unqualified names live in the same package (directory) and nowhere else.
        package = list(index.free_by_dir_label.get((str(PurePosixPath(source_file).parent), callee), ()))
        return ("same_package", package) if package else None

    imported: list[str] = []
    import_module = md.get("import_module")
    if import_module:
        for path in index.module_paths(str(import_module)):
            hit = index.exported_by_path_label.get((path, callee), ())
            if not hit and path.endswith("__init__.py"):
                hit = [c for hop in index.imports.get(path, ()) for c in index.exported_by_path_label.get((hop, callee), ())]
            imported.extend(hit)
    if not imported:
        for path in index.reachable(source_file):
            imported.extend(index.exported_by_path_label.get((path, callee), ()))
    if imported:
        return "import_scoped", index.collapse(sorted(set(imported)))

    if python and callee in _PY_BUILTINS:
        return None
    unique = index.free_by_label.get(callee, [])
    if unique:
        return "repo_unique", index.collapse(list(unique))
    return None


def _resolve_new(edge: GraphEdge, index: _Index, source_file: str) -> _Outcome:
    """`new T(...)` / `T(...)` construction: the class named T."""
    callee = str(edge.metadata["callee"])
    classes = index.classes_by_label.get(callee, [])
    if not classes:
        return None
    near = index.prefer(classes, source_file)
    reachable = set(index.reachable(source_file))
    if all(index.symbols[c].source_ref == source_file for c in near):
        tier = "same_file"
    elif all(index.symbols[c].source_ref in reachable for c in near):
        tier = "import_scoped"
    else:
        tier = "repo_unique"
    return tier, index.collapse(list(near))


def _resolve_one(edge: GraphEdge, index: _Index) -> _Outcome:
    md = edge.metadata
    callee = str(md["callee"])
    kind = md.get("receiver_kind") or "none"
    source_file = _call_source_file(edge, index)
    python = source_file.endswith(".py")

    if md.get("scope") or md.get("scope_module"):
        return _resolve_scoped(edge, index, source_file)

    if kind in ("this", "self"):
        classes = _enclosing_classes(edge, index, source_file)
        if md.get("super"):
            return _tier_from_lookup(index.lookup(classes, callee, skip_direct=True), "this") if classes else None
        label = _last_segment(str(md.get("enclosing_class") or ""))
        return _members(index, classes, label, callee, source_file, "this") if label else None

    receiver_type = md.get("receiver_type")
    if not receiver_type and kind == "var" and md.get("receiver_member"):
        receiver_type = _member_type(edge, index, source_file, str(md["receiver_member"]))
    if receiver_type:
        label = _last_segment(str(receiver_type))
        classes = index.classes_by_label.get(label, [])
        type_file = md.get("receiver_type_file")
        if type_file:
            classes = [c for c in classes if index.symbols[c].source_ref == type_file]
        else:
            classes = index.prefer(classes, source_file)
        return _members(index, classes, label, callee, source_file, "declared_type")

    if kind == "var" and python and md.get("receiver"):
        # `Cls.method()` on a class defined in this same file (not imported)
        owner = index.by_path_qual.get((source_file, str(md["receiver"])))
        if owner is not None and owner.kind == "class":
            return _tier_from_lookup(index.lookup([owner.node_id], callee), "scoped")

    if kind == "new":
        if "import_spec" in md:
            return _resolve_import_binding(edge, index)
        return _resolve_new(edge, index, source_file)

    if kind == "chain" and not python and language_family(source_file) == CPP_FAMILY:
        return _resolve_chain(edge, index, source_file)

    if kind != "none":
        return None  # untyped receiver: nothing static to resolve against

    if "import_spec" in md:
        return _resolve_import_binding(edge, index)

    if not python and md.get("implicit_receiver", True):
        label = _last_segment(str(md.get("enclosing_class") or ""))
        if label:
            outcome = _members(index, _enclosing_classes(edge, index, source_file), label, callee, source_file, "this")
            if outcome is not None:
                return outcome
    return _resolve_free(edge, index, source_file)


def _is_affected(
    edge: GraphEdge,
    node_ids: set[str],
    affected_files: set[str],
    changed_labels: set[str],
    widen_typed: bool,
) -> bool:
    md = edge.metadata
    if md.get("source_file") in affected_files or md.get("callee") in changed_labels:
        return True
    if edge.target.startswith("symbol:") and edge.target not in node_ids:
        return True  # previously resolved target no longer exists
    if md.get("receiver_kind") == "chain" and (
        # a chain follows the inner calls' return types: re-resolve it when a function it calls was touched
        # (a member it reads, or a class it ends in, moves with `widen_typed`)
        widen_typed
        or (changed_labels and not changed_labels.isdisjoint(_chain_callees(str(md.get("receiver") or ""))))
    ):
        return True
    if widen_typed and (
        md.get("enclosing_class")
        or md.get("receiver_type")
        or md.get("receiver_member")
        or md.get("scope")
        or md.get("scope_module")
        or md.get("scope_file")
        or md.get("scope_dir")
        or md.get("receiver_kind") in ("this", "self")
    ):
        return True
    return False


def _is_resolvable_inherits(edge: GraphEdge) -> bool:
    """An `inherits` edge from a class symbol whose base is named by label (`name:`/`module:`), or was
    (a rewritten edge keeps the label in `metadata.unresolved_target`)."""
    if edge.relation != "inherits" or not edge.source.startswith("symbol:"):
        return False
    if edge.target.startswith(("name:", "module:")):
        return True
    return edge.target.startswith("symbol:") and isinstance(edge.metadata, dict) and "unresolved_target" in edge.metadata


def _reset_inherits(edge: GraphEdge) -> None:
    """Back to the base as the extractor wrote it, ahead of a fresh resolution."""
    md = edge.metadata
    original = md.pop("unresolved_target", None)
    if original:
        edge.target = str(original)
    for key in _RESOLUTION_KEYS:
        md.pop(key, None)
    previous = md.pop("extracted_confidence", None)
    if previous is not None:
        edge.confidence = previous


def _inherits_affected(edge: GraphEdge, node_ids: set[str], affected_files: set[str], changed_labels: set[str]) -> bool:
    """Re-resolve an `inherits` edge when its class is in the batch, its resolved base vanished, or a class
    carrying the base's label was added or removed (the answer of `prefer` can move with either)."""
    if _edge_path(edge) in affected_files:
        return True
    if edge.target.startswith("symbol:") and edge.target not in node_ids:
        return True
    return _inherits_label(edge) in changed_labels


def _resolve_inherits_into(edge: GraphEdge, index: _Index) -> None:
    """Rewrite one `inherits` edge: a unique base class becomes its `symbol:` id (the label stays in
    `metadata.unresolved_target`); several candidates keep `name:` and record them, like an ambiguous call."""
    candidates = index.edge_bases.get(edge.edge_id)
    if candidates is None:
        candidates = index.base_candidates(edge.source, _inherits_label(edge))
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        edge.metadata["unresolved_target"] = edge.target
        edge.target = candidates[0]
        edge.metadata["resolution"] = "label" if len(index.classes_by_label.get(_inherits_label(edge), ())) == 1 else "preferred"
    elif len(candidates) > 1:
        if edge.confidence != "LOW":
            edge.metadata["extracted_confidence"] = edge.confidence
            edge.confidence = "LOW"
        edge.metadata["candidates"] = candidates[:MAX_CANDIDATES]
        edge.metadata["ambiguous"] = True
    else:
        # No class carries the label in the file's language family: the base is outside the repository or
        # unknown.  Name the reason in metadata, leave the target as written.
        path = _edge_path(edge)
        family = language_family(path)
        rule = classify_base(_inherits_label(edge), edge.target, index.external_imports(path, family), family)
        if rule:
            edge.metadata["external"] = rule


def resolve_call_endpoints(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    affected_files: set[str] | None = None,
    changed_labels: set[str] | None = None,
    widen_typed: bool = False,
) -> list[GraphEdge]:
    """Rewrite contract `calls` edges' `name:<callee>` targets to symbol ids, and `inherits` edges'
    `name:<Base>` targets to the base class's symbol id.

    With no scope arguments every contract call edge and every `inherits` edge is (re)resolved -- the
    full-ingest case.  The incremental pass supplies the batch's files, the labels of symbols added or
    removed by the batch, and `widen_typed` when classes or inheritance changed (member/base lookups can
    then move without the callee label changing); only matching call edges, plus edges whose previously
    resolved target vanished, are re-resolved.  An `inherits` edge is re-resolved when its class is in the
    batch, its resolved base vanished, or a class labelled like its base was added or removed
    (`metadata.unresolved_target` keeps the label of a rewritten edge).  Inheritance is its own pass:
    it runs even when the graph has no call to resolve.  Edges are mutated in place and returned (never
    added or removed).
    """
    scoped = affected_files is not None or changed_labels is not None or widen_typed
    files = affected_files or set()
    labels = changed_labels or set()
    calls = [edge for edge in edges if is_contract_call(edge)]
    all_calls = calls
    inherits = [edge for edge in edges if _is_resolvable_inherits(edge)]
    node_ids = {node.node_id for node in nodes} if scoped else set()
    if scoped:
        calls = [edge for edge in calls if _is_affected(edge, node_ids, files, labels, widen_typed)]
        inherits = [edge for edge in inherits if _inherits_affected(edge, node_ids, files, labels)]
    if not calls and not inherits:
        return edges
    for edge in inherits:
        _reset_inherits(edge)
    by_family: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in calls:
        by_family[language_family(_call_path(edge))].append(edge)
    inherits_by_family: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in inherits:
        inherits_by_family[language_family(_edge_path(edge))].append(edge)
    families = sorted(set(by_family) | set(inherits_by_family))
    if len(families) == 1 and len({language_family(n.source_ref) for n in nodes if n.source_ref}) == 1:
        partitions = {families[0]: (nodes, edges)}
    else:
        node_buckets: dict[str, list[GraphNode]] = defaultdict(list)
        for node in nodes:
            node_buckets[language_family(node.source_ref)].append(node)
        edge_buckets: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in edges:
            if edge.relation in ("imports", "inherits"):
                edge_buckets[language_family(_edge_path(edge))].append(edge)
        partitions = {fam: (node_buckets.get(fam, []), edge_buckets.get(fam, [])) for fam in families}
    for family in families:
        fam_nodes, fam_edges = partitions[family]
        index = _Index(fam_nodes, fam_edges)
        for edge in inherits_by_family.get(family, ()):
            _resolve_inherits_into(edge, index)
        family_calls = by_family.get(family, [])
        if family == CPP_FAMILY:
            # Chains last, shortest receiver first: a chain reads the resolved targets of the calls inside its
            # receiver, and a chain inside that receiver has a shorter one.
            plain = [e for e in family_calls if e.metadata.get("receiver_kind") != "chain"]
            chains = sorted(
                (e for e in family_calls if e.metadata.get("receiver_kind") == "chain"),
                key=lambda e: len(str(e.metadata.get("receiver") or "")),
            )
            family_calls = plain + chains
            if chains:
                index.attach_calls([e for e in all_calls if language_family(_call_path(e)) == family])
        for edge in family_calls:
            _resolve_into(edge, index)
    return edges


def _call_path(edge: GraphEdge) -> str:
    source_file = edge.metadata.get("source_file")
    return str(source_file) if source_file else _edge_path(edge)


def _mark_external_call(edge: GraphEdge, index: _Index) -> None:
    """Record in `metadata.external` why a call nothing resolved stays `name:` (target unchanged).

    Called only when no candidate exists at all (never for an ambiguous edge).  A free name that the
    repository defines anywhere in the family is never external, and neither is a call whose scope or
    receiver type is a repository class: the repository, not a library, owns that member.  The exception is a
    member that the class and its repository bases lack while a class on that path has a base outside the
    repository: the call is marked ``inherited`` (`QWidget::update()` on `class W : public QWidget`).
    """
    md = edge.metadata
    family = language_family(_call_path(edge))
    if family != CPP_FAMILY:
        return
    callee = str(md["callee"])
    receiver_type = md.get("receiver_type")
    if not receiver_type and md.get("receiver_kind") == "var" and md.get("receiver_member"):
        receiver_type = _member_type(edge, index, _call_source_file(edge, index), str(md["receiver_member"]))
        if receiver_type:
            md = {**md, "receiver_type": receiver_type}
    typed = False
    for text in (md.get("scope"), receiver_type):
        if text:
            typed = True
            label = _last_segment(str(text))
            classes = index.prefer(index.classes_by_label.get(label, []), _call_source_file(edge, index))
            if classes:
                if _inherited_from_outside(classes, index):
                    edge.metadata["external"] = "inherited"
                return
            if index.qualified_defs.get(label):
                return
    if not typed and callee in index.defined_names:
        # a method of some other class, but this class derives from something outside the repository
        if _implicit_receiver(md) and _inherited_from_outside(_enclosing_classes(edge, index, _call_source_file(edge, index)), index):
            edge.metadata["external"] = "inherited"
        return
    rule = classify_call(md, family)
    if not rule and not typed and _implicit_receiver(md):
        if _inherited_from_outside(_enclosing_classes(edge, index, _call_source_file(edge, index)), index):
            rule = "inherited"
    if rule:
        edge.metadata["external"] = rule


def _implicit_receiver(md: dict) -> bool:
    """`f()` inside a method (an implicit `this`) or `this->f()`."""
    kind = md.get("receiver_kind") or "none"
    return bool(md.get("enclosing_class")) and (kind in ("this", "self") or (kind == "none" and md.get("implicit_receiver", True)))


def _inherited_from_outside(class_ids: list[str], index: _Index) -> bool:
    """Every candidate class has, itself or among its repository bases, a base outside the repository: a member
    the repository cannot find on it is probably inherited from that base (`QWidget::update()`)."""
    return bool(class_ids) and all(index.has_outside_base(c) for c in class_ids)


def _narrowable(edge: GraphEdge, outcome: _Outcome, index: _Index) -> bool:
    """A C/C++ edge that depends on a class lookup (`enclosing_class`, a receiver type or member, a scope) and
    stayed unresolved or ambiguous."""
    if outcome is not None and len(set(outcome[1])) < 2:
        return False
    md = edge.metadata
    return language_family(_call_path(edge)) == CPP_FAMILY and bool(
        md.get("enclosing_class") or md.get("receiver_type") or md.get("receiver_base_type") or md.get("receiver_member")
        or md.get("scope")
    )


def _resolve_into(edge: GraphEdge, index: _Index) -> None:
    _reset(edge)
    trace = index.trace = set()
    try:
        outcome = _resolve_one(edge, index)
        if _narrowable(edge, outcome, index):
            # Several classes share the label and nothing local told them apart: look at the include ring of the
            # calling file too.  Only an answer with exactly one candidate replaces the first one, so a call that
            # resolved before never moves.
            index.deep = True
            second_trace = index.trace = set()
            deeper = _resolve_one(edge, index)
            if deeper is not None and len(set(deeper[1])) == 1:
                outcome, trace = deeper, second_trace
            elif outcome is not None:
                index.deep = False
        if outcome is None:
            _mark_external_call(edge, index)
    finally:
        index.trace = None
        index.deep = False
    if outcome is None:
        return
    tier, candidate_ids = outcome
    candidate_ids = sorted(set(candidate_ids))
    if len(candidate_ids) == 1:
        edge.target = candidate_ids[0]
        edge.metadata["resolution"] = tier
        if "production" in trace:
            edge.metadata["preferred"] = "production"
    elif len(candidate_ids) > 1:
        if edge.confidence != "LOW":
            edge.metadata["extracted_confidence"] = edge.confidence
            edge.confidence = "LOW"
        edge.metadata["candidates"] = candidate_ids[:MAX_CANDIDATES]
        edge.metadata["ambiguous"] = True
