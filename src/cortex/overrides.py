"""Overriding methods of a class hierarchy, derived at query time.

The graph holds no `overrides` relation (the resolver never adds edges, and a new relation
would change relations / impact / dead-code output).  `cortex_query` derives it instead from
what the graph already has: the resolved `inherits` edges between class symbols and the
class membership the call resolver uses (span containment, ``Cls::`` qualifiers).  A method
of a derived class that has the name of a method of one of its bases overrides it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence

from .callresolve import MAX_INHERIT_DEPTH, ClassIndex, _last_segment
from .models import GraphNode

METHOD_KINDS = frozenset({"func", "method"})


def find_overriders(nodes: Iterable[GraphNode], bases_of: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Base method symbol id -> sorted symbol ids of the same-named methods of derived classes.

    `bases_of` maps a class symbol to its resolved base class symbols.  Only classes that take
    part in a resolved inheritance edge are looked at, so the cost follows the hierarchy, not
    the repository.  Constructors and destructors never override.
    """
    if not bases_of:
        return {}
    derived_of: defaultdict[str, set[str]] = defaultdict(set)
    hierarchy: set[str] = set()
    for derived, bases in bases_of.items():
        for base in bases:
            if base != derived:
                derived_of[base].add(derived)
                hierarchy.update((derived, base))
    if not derived_of:
        return {}

    node_list = list(nodes)
    classes = {n.node_id: n for n in node_list if n.granularity == "symbol" and n.node_id in hierarchy}
    labels = {node.label for node in classes.values()}
    files = {node.source_ref for node in classes.values()}
    for node in node_list:
        if node.granularity != "symbol" or node.source_ref in files:
            continue
        qualifier = node.metadata.get("qualifier") if isinstance(node.metadata, dict) else None
        if isinstance(qualifier, str) and qualifier and _last_segment(qualifier) in labels:
            files.add(node.source_ref)
    index = ClassIndex(node for node in node_list if node.granularity == "symbol" and node.source_ref in files)

    def methods(class_id: str, name: str) -> list[str]:
        return [m for m in index.class_members(class_id, name) if index.symbols[m].kind in METHOD_KINDS]

    overridden_by: dict[str, list[str]] = {}
    for base_id in sorted(derived_of):
        base = classes.get(base_id)
        if base is None:
            continue
        names = {*index.declared_members.get(base_id, {}), *index.qualified_defs.get(base.label, {})}
        names = {name for name in names if name != base.label and not name.startswith("~")}
        descendants = _descendants(base_id, derived_of)
        for name in sorted(names):
            base_methods = methods(base_id, name)
            if not base_methods:
                continue
            overriders = sorted({m for derived in descendants for m in methods(derived, name)} - set(base_methods))
            if not overriders:
                continue
            for method in base_methods:
                overridden_by[method] = sorted({*overridden_by.get(method, ()), *overriders})
    return overridden_by


def _descendants(class_id: str, derived_of: Mapping[str, set[str]]) -> list[str]:
    """Every class deriving from `class_id`, breadth first, at most MAX_INHERIT_DEPTH levels down."""
    seen = {class_id}
    queue = deque([(class_id, 0)])
    found: list[str] = []
    while queue:
        current, depth = queue.popleft()
        if depth >= MAX_INHERIT_DEPTH:
            continue
        for child in sorted(derived_of.get(current, ())):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append((child, depth + 1))
    return found
