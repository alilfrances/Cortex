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
    import_scoped  free callee defined in a file the caller imports/includes
    repo_unique    free callee with exactly one definition repo-wide

More than one candidate at the deciding tier keeps the ``name:`` target and
records ``candidates`` (sorted, <= 8), ``ambiguous = True``, confidence LOW.
Nothing is ever resolved by scanning the corpus per candidate.
"""

from __future__ import annotations

import builtins
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Iterable

from .models import GraphEdge, GraphNode

MAX_CANDIDATES = 8
MAX_INHERIT_DEPTH = 6
_HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx"}
_IMPL_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_PY_BUILTINS = frozenset(dir(builtins))
_RESOLUTION_KEYS = ("resolution", "candidates", "ambiguous")


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


def _dotted_suffixes(path: str) -> list[str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return [".".join(parts[i:]) for i in range(len(parts))]


class _Index:
    """All hash indexes the resolver needs, built in one pass over the graph."""

    def __init__(self, nodes: Iterable[GraphNode], edges: list[GraphEdge]) -> None:
        self.node_ids: set[str] = set()
        symbols: dict[str, GraphNode] = {}
        py_files: list[str] = []
        stems: defaultdict[str, list[str]] = defaultdict(list)
        for node in nodes:
            self.node_ids.add(node.node_id)
            if node.granularity == "symbol":
                symbols.setdefault(node.node_id, node)
            elif node.kind == "file":
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

        # Declared member variable types recorded on class nodes (`member_types`
        # metadata), so `m_model->f()` in an out-of-line `Foo::bar()` can be typed
        # from the header that declares `m_model`.
        self.member_types: dict[str, dict[str, str]] = {}
        for node_id, node in symbols.items():
            mt = node.metadata.get("member_types") if isinstance(node.metadata, dict) else None
            if node.kind == "class" and isinstance(mt, dict) and mt:
                self.member_types[node_id] = {str(k): str(v) for k, v in mt.items()}

        self.declared_members: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.qualified_defs: defaultdict[str, defaultdict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
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
            if owner is not None:
                self.declared_members[owner][node.label].append(node_id)
            if isinstance(qualifier, str) and qualifier:
                self.qualified_defs[_last_segment(qualifier)][node.label].append(node_id)
            namespaced = owner is not None and self.is_namespace(owner)
            if (owner is None or namespaced) and not (isinstance(qualifier, str) and qualifier):
                self.free_by_path_label[(node.source_ref, node.label)].append(node_id)
                if not (node.source_ref.endswith(".py") and "." in _symbol_path(node_id)[1]):
                    self.free_by_label[node.label].append(node_id)
                    self.exported_by_path_label[(node.source_ref, node.label)].append(node_id)

        imports: defaultdict[str, set[str]] = defaultdict(set)
        inherits: list[GraphEdge] = []
        for edge in edges:
            if edge.relation == "imports" and edge.source.startswith("file:") and edge.target.startswith("file:"):
                imports[edge.source.removeprefix("file:")].add(edge.target.removeprefix("file:"))
            elif edge.relation == "inherits" and edge.source in symbols:
                inherits.append(edge)
        self.imports: dict[str, list[str]] = {path: sorted(targets) for path, targets in imports.items()}

        self.module_exact: dict[str, list[str]] = {}
        self.module_suffix: defaultdict[str, list[str]] = defaultdict(list)
        for path in sorted(set(py_files)):
            suffixes = _dotted_suffixes(path)
            if suffixes:
                self.module_exact.setdefault(suffixes[0], []).append(path)
            for dotted in suffixes:
                self.module_suffix[dotted].append(path)

        bases: defaultdict[str, set[str]] = defaultdict(set)
        for edge in sorted(inherits, key=lambda e: e.edge_id):
            if edge.target in symbols and symbols[edge.target].kind == "class":
                bases[edge.source].add(edge.target)
                continue
            candidates = self.classes_by_label.get(_base_label(edge.target), [])
            if len(candidates) > 1:
                path = symbols[edge.source].source_ref
                preferred = self.prefer(candidates, path)
                candidates = preferred
            bases[edge.source].update(candidates)
        self.bases: dict[str, list[str]] = {cid: sorted(ids - {cid}) for cid, ids in bases.items()}

    def is_namespace(self, class_id: str) -> bool:
        node = self.symbols.get(class_id)
        return node is not None and node.signature.lstrip().startswith("namespace")

    def reachable(self, from_path: str) -> list[str]:
        """Files the caller imports/includes (one hop), plus the implementation
        files of every imported header."""
        paths = list(self.imports.get(from_path, ()))
        extra = [impl for path in paths for impl in self.impl_siblings.get(path, ())]
        return sorted(set(paths) | set(extra))

    # -- candidate narrowing ------------------------------------------------

    def prefer(self, candidate_ids: list[str], from_path: str) -> list[str]:
        """Same file first, then files the caller imports; else unchanged."""
        if len(candidate_ids) < 2:
            return candidate_ids
        same = [c for c in candidate_ids if self.symbols[c].source_ref == from_path]
        if same:
            return same
        imported = set(self.reachable(from_path))
        near = [c for c in candidate_ids if self.symbols[c].source_ref in imported]
        return near or candidate_ids

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

    def class_members(self, class_id: str, name: str) -> list[str]:
        declared = self.declared_members.get(class_id, {}).get(name)
        if declared:
            return declared
        label = self.symbols[class_id].label
        return self.qualified_defs.get(label, {}).get(name, [])

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


def _member_type(edge: GraphEdge, index: _Index, source_file: str, member: str) -> str | None:
    """Declared type of member variable `member` of the enclosing class (or a base)."""
    classes = _enclosing_classes(edge, index, source_file)
    if not classes:
        return None
    frontier = sorted(set(classes))
    visited = set(frontier)
    depth = 0
    while frontier and depth <= MAX_INHERIT_DEPTH:
        found = sorted({index.member_types[c][member] for c in frontier if member in index.member_types.get(c, {})})
        if found:
            return found[0] if len(found) == 1 else None
        nxt: list[str] = []
        for cid in frontier:
            for base in index.bases.get(cid, ()):
                if base not in visited:
                    visited.add(base)
                    nxt.append(base)
        frontier = sorted(nxt)
        depth += 1
    return None


def _tier_from_lookup(found: tuple[str, list[str]] | None, direct_tier: str) -> _Outcome:
    if found is None:
        return None
    kind, ids = found
    return ("inherited" if kind == "inherited" else direct_tier), ids


def _resolve_scoped(edge: GraphEdge, index: _Index, source_file: str) -> _Outcome:
    md = edge.metadata
    callee = str(md["callee"])
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
    if label:
        class_ids = index.prefer(index.classes_by_label.get(label, []), source_file)
        if class_ids:
            return _tier_from_lookup(index.lookup(class_ids, callee), "scoped")
        qualified = index.qualified_defs.get(label, {}).get(callee)
        if qualified:
            return "scoped", list(qualified)
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
        if not classes:
            return None
        found = index.lookup(classes, callee, skip_direct=bool(md.get("super")))
        return _tier_from_lookup(found, "this")

    receiver_type = md.get("receiver_type")
    if not receiver_type and kind == "var" and md.get("receiver_member"):
        receiver_type = _member_type(edge, index, source_file, str(md["receiver_member"]))
    if receiver_type:
        label = _last_segment(str(receiver_type))
        classes = index.prefer(index.classes_by_label.get(label, []), source_file)
        if not classes:
            return None
        return _tier_from_lookup(index.lookup(classes, callee), "declared_type")

    if kind == "var" and python and md.get("receiver"):
        # `Cls.method()` on a class defined in this same file (not imported)
        owner = index.by_path_qual.get((source_file, str(md["receiver"])))
        if owner is not None and owner.kind == "class":
            return _tier_from_lookup(index.lookup([owner.node_id], callee), "scoped")

    if kind == "new":
        return _resolve_new(edge, index, source_file)

    if kind != "none":
        return None  # untyped receiver: nothing static to resolve against

    if not python:
        classes = _enclosing_classes(edge, index, source_file)
        if classes:
            outcome = _tier_from_lookup(index.lookup(classes, callee), "this")
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
    if widen_typed and (
        md.get("enclosing_class")
        or md.get("receiver_type")
        or md.get("receiver_member")
        or md.get("scope")
        or md.get("scope_module")
        or md.get("receiver_kind") in ("this", "self")
    ):
        return True
    return False


def resolve_call_endpoints(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    affected_files: set[str] | None = None,
    changed_labels: set[str] | None = None,
    widen_typed: bool = False,
) -> list[GraphEdge]:
    """Rewrite contract `calls` edges' `name:<callee>` targets to symbol ids.

    With no scope arguments every contract call edge is (re)resolved -- the
    full-ingest case.  The incremental pass supplies the batch's files,
    the labels of symbols added or removed by the batch, and `widen_typed`
    when classes or inheritance changed (member/base lookups can then move
    without the callee label changing); only matching edges, plus edges whose
    previously resolved target vanished, are re-resolved.  Edges are mutated
    in place and returned (never added or removed).
    """
    scoped = affected_files is not None or changed_labels is not None or widen_typed
    files = affected_files or set()
    labels = changed_labels or set()
    calls = [edge for edge in edges if is_contract_call(edge)]
    if not calls:
        return edges
    if scoped:
        node_ids = {node.node_id for node in nodes}
        calls = [edge for edge in calls if _is_affected(edge, node_ids, files, labels, widen_typed)]
        if not calls:
            return edges
    index = _Index(nodes, edges)
    for edge in calls:
        _reset(edge)
        outcome = _resolve_one(edge, index)
        if outcome is None:
            continue
        tier, candidate_ids = outcome
        candidate_ids = sorted(set(candidate_ids))
        if len(candidate_ids) == 1:
            edge.target = candidate_ids[0]
            edge.metadata["resolution"] = tier
        elif len(candidate_ids) > 1:
            if edge.confidence != "LOW":
                edge.metadata["extracted_confidence"] = edge.confidence
                edge.confidence = "LOW"
            edge.metadata["candidates"] = candidate_ids[:MAX_CANDIDATES]
            edge.metadata["ambiguous"] = True
    return edges
