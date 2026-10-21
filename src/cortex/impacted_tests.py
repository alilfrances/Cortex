"""Impacted-test selection for ``cortex_risk`` / ``cortex risk`` (``tests_to_run``).

Given a diff, the persisted graph and the co-change map, select the test files
that a reviewer should run first, each with the tiers (``via``) that selected it:

``structural``
    Reverse reachability over STRUCTURAL edges (``target -> source``) from the
    symbols whose spans contain changed lines -- or from the file itself when a
    change falls outside every symbol -- bounded at depth 3, stopping at test
    files.  Score ``1 / depth``.
``cochange``
    A COCHANGE partner of a changed file that is a test, weight >= 0.5.
``pair``
    A test whose file name pairs with a changed source file (``a.py`` /
    ``test_a.py``).  Score 0.6.
``changed``
    A test file that is itself part of the diff.  Score 1.0.

The selection is only advisory unless it can be vouched for: ``tests_run_all``
is set (with a reason) whenever the index is unavailable, a build/config file
changed, a changed file is deleted or unindexed, no test is indexed, or any
changed non-documentation file was reached by no tier.  Everything is
deterministic: inputs are indexed once into hash maps (O(nodes + edges)), and
every iteration order is sorted.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Mapping, Sequence

from .callresolve import _base_label
from .deadline import check_deadline
from .models import GraphEdge
from .testpatterns import is_test_path, pairs_with_test, test_base_name

STRUCTURAL_MAX_DEPTH = 3
# A base class label shared by more repo classes than this cannot be resolved by
# name (typically a fabricated or ubiquitous `QObject`): attaching its `inherits`
# edges to all of them would select every subclass's tests.
LABEL_ATTACH_MAX = 3
COCHANGE_TEST_THRESHOLD = 0.50
PAIR_SCORE = 0.6
CHANGED_SCORE = 1.0

# Reverse hops that carry "a change here affects that" across the graph.
# ``contains`` is deliberately absent: a file containing a changed symbol
# does not make every importer of the file impacted.
STRUCTURAL_RELATIONS = frozenset({
    "calls", "imports", "references", "inherits", "instantiates", "connects",
    "handles", "emits", "binds", "reads", "writes", "aliases", "builds", "registers",
})
# The file of a changed symbol is still the unit that is compiled into a test
# target or registered as a QML resource.
_WIRING_RELATIONS = frozenset({"builds", "registers", "instantiates"})
# A changed member affects whoever instantiates/constructs its owning type, and
# (through `inherits`) the users of its subclasses.
_OWNER_RELATIONS = frozenset({"instantiates", "calls", "inherits"})
_HUB_FORWARD_RELATIONS = frozenset({"builds", "registers"})

_RUNNABLE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".rb", ".go", ".rs",
    ".c", ".cpp", ".cc", ".cxx", ".qml",
}
# Languages whose test files are recognised by directory alone (no naming rule).
_DIRECTORY_TEST_SUFFIXES = {".rs", ".c", ".cpp", ".cc", ".cxx", ".qml"}
_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
_CPP_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}
_CPP_SUFFIXES = {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"}
_CLASS_KINDS = {"class", "struct", "object", "component"}

BUILD_CONFIG_NAMES = {
    "cmakelists.txt", "conftest.py", "setup.cfg", "setup.py", "pyproject.toml", "package.json",
    "package-lock.json", "tsconfig.json", "go.mod", "go.sum", "gemfile", "gemfile.lock",
    "pom.xml", "build.gradle", "package.swift", "pytest.ini", "tox.ini", "makefile",
}
BUILD_CONFIG_SUFFIXES = {".cmake", ".qrc", ".toml", ".yaml", ".yml", ".pro", ".pri", ".gradle"}
_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".txt", ".adoc"}
_DOC_NAMES = {".gitignore", ".gitattributes", ".editorconfig", "license", "notice", "authors", "copying"}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class ChangedLines:
    """New-side line numbers of meaningful (non-blank) changes in one file."""

    lines: set[int] = field(default_factory=set)
    hunks: int = 0


def parse_changed_lines(raw: str) -> dict[str, ChangedLines]:
    """Map each file of a ``--unified=0`` diff to its changed new-side lines.

    Additions contribute their own line numbers; a pure deletion
    (``+c,0``) contributes the lines on both sides of the gap (``c`` and
    ``c + 1``).  Blank-only additions/deletions are ignored.  A deleted file
    is keyed by its old path, every other file by its new path.
    """
    result: dict[str, ChangedLines] = {}
    current: str | None = None
    old_path: str | None = None
    new_line = 0
    deletion_point: int | None = None
    deletion_meaningful = False

    def flush_deletion() -> None:
        nonlocal deletion_point, deletion_meaningful
        if current is not None and deletion_point is not None and deletion_meaningful:
            point = max(1, deletion_point)
            result[current].lines.update({point, point + 1})
        deletion_point = None
        deletion_meaningful = False

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            flush_deletion()
            current = old_path = None
            continue
        if line.startswith("--- ") and current is None:
            old_path = _header_path(line[4:])
            continue
        if line.startswith("+++ ") and current is None:
            current = _header_path(line[4:]) or old_path
            if current:
                result.setdefault(current, ChangedLines())
            continue
        if current is None:
            continue
        match = _HUNK_RE.match(line)
        if match:
            flush_deletion()
            result[current].hunks += 1
            new_start = int(match.group(3))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            new_line = new_start
            if new_count == 0:
                deletion_point = new_start
            continue
        if line.startswith("+"):
            if line[1:].strip():
                result[current].lines.add(new_line)
            new_line += 1
        elif line.startswith("-") and deletion_point is not None and line[1:].strip():
            deletion_meaningful = True
    flush_deletion()
    return result


def _header_path(value: str) -> str | None:
    path = value.strip().split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def is_runnable_test(path: str) -> bool:
    """A test *file to run*, not a helper/fixture/header living in a test dir."""
    pure = PurePosixPath(path.replace("\\", "/"))
    suffix = pure.suffix.lower()
    if suffix not in _RUNNABLE_SUFFIXES or not is_test_path(path):
        return False
    if suffix == ".rs" and "tests" in pure.parts[:-1]:
        # Cargo integration-test targets are `tests/*.rs` and `tests/<name>/main.rs`;
        # `tests/util/mod.rs` and other nested modules are shared helpers (traversed,
        # not selected), as on dtolnay/semver.
        parent = pure.parts[-2]
        if parent == "tests":
            return pure.name != "mod.rs"
        return pure.name == "main.rs" and len(pure.parts) >= 3 and pure.parts[-3] == "tests"
    if suffix in _DIRECTORY_TEST_SUFFIXES or "__tests__" in pure.parts[:-1]:
        return True
    if suffix in _JS_SUFFIXES and len(pure.parts) >= 2 and pure.parts[-2].lower() in {"test", "tests"}:
        # `node --test test/*.ts`, mocha's `./test/*.js`, AVA's `test/` default: a script
        # directly inside a top-level-style test directory is a test file; nested
        # `test/fixtures/**` / `test/helpers/**` support files are not.
        return not pure.name.startswith("_")
    # The file name itself must follow the language's test naming convention.
    return test_base_name(pure.name)[1]


def is_build_config(path: str) -> bool:
    pure = PurePosixPath(path.replace("\\", "/"))
    return pure.name.lower() in BUILD_CONFIG_NAMES or pure.suffix.lower() in BUILD_CONFIG_SUFFIXES


def is_documentation(path: str) -> bool:
    pure = PurePosixPath(path.replace("\\", "/"))
    name = pure.name.lower()
    return pure.suffix.lower() in _DOC_SUFFIXES or name in _DOC_NAMES or name.startswith(("license", "copying"))


@dataclass
class _Hit:
    score: float
    reason: str


class _Selection:
    def __init__(self) -> None:
        self.hits: dict[str, dict[str, _Hit]] = defaultdict(dict)
        self.covered: set[str] = set()  # changed files at least one test was selected for
        self.tests_for: dict[str, set[str]] = defaultdict(set)  # changed file -> tests selected for it

    def add(self, test: str, tier: str, score: float, reason: str, *, for_path: str | None) -> None:
        previous = self.hits[test].get(tier)
        if previous is None or (score, _neg(reason)) > (previous.score, _neg(previous.reason)):
            self.hits[test][tier] = _Hit(score, reason)
        if for_path is not None:
            self.covered.add(for_path)
            self.tests_for[for_path].add(test)

    def items(self) -> list[dict[str, Any]]:
        result = []
        for test, tiers in self.hits.items():
            via = sorted(tiers)
            result.append({
                "test": test,
                "via": via,
                "reason": "; ".join(f"{tier}: {tiers[tier].reason}" for tier in via),
                "score": round(max(hit.score for hit in tiers.values()), 3),
            })
        result.sort(key=lambda item: (-item["score"], item["test"]))
        return result


def _neg(text: str) -> tuple[int, ...]:
    # Prefer the lexicographically smallest reason on equal scores.
    return tuple(-ord(ch) for ch in text)


class _GraphIndex:
    """Hash indexes over the persisted graph, built once per risk call."""

    def __init__(self, nodes: Sequence[Any], edges: Sequence[Any]) -> None:
        self.nodes: dict[str, Any] = {node.node_id: node for node in nodes}
        self.symbols_by_path: dict[str, list[Any]] = defaultdict(list)
        for node in nodes:
            if (
                getattr(node, "granularity", "") == "symbol"
                and isinstance(node.span_start, int)
                and isinstance(node.span_end, int)
                and node.kind not in {"module", "target"}
                and not node.node_id.startswith("import:")
            ):
                self.symbols_by_path[node.source_ref].append(node)
        for symbols in self.symbols_by_path.values():
            symbols.sort(key=lambda item: (item.span_start, -item.span_end, item.node_id))
        self.reverse: dict[str, list[Any]] = defaultdict(list)
        self.forward_hub: dict[str, list[Any]] = defaultdict(list)
        for edge in edges:
            if edge.layer != "STRUCTURAL" or edge.relation not in STRUCTURAL_RELATIONS:
                continue
            self.reverse[edge.target].append(edge)
            if edge.relation in _HUB_FORWARD_RELATIONS:
                self.forward_hub[edge.source].append(edge)
        for bucket in (*self.reverse.values(), *self.forward_hub.values()):
            bucket.sort(key=lambda item: (item.relation, item.source, item.target, item.edge_id))
        self._cpp_equivalents = self._build_cpp_equivalents(nodes)
        self._classes_by_label: dict[str, list[Any]] = defaultdict(list)
        for node in nodes:
            if getattr(node, "granularity", "") == "symbol" and node.kind in _CLASS_KINDS:
                self._classes_by_label[node.label].append(node)
        for bucket in self._classes_by_label.values():
            bucket.sort(key=lambda item: item.node_id)
        # `inherits` edges keep the base as written (`name:qqsfpm::Sorter`,
        # `module:Base`), so a reverse walk keyed by symbol id never crossed them.
        # Attach each to every repo class carrying the base's label: over-selecting
        # a same-named class's subclasses' tests is the safe side for test selection.
        attached: set[str] = set()
        for edge in edges:
            if edge.layer != "STRUCTURAL" or edge.relation != "inherits" or edge.target in self.nodes:
                continue
            candidates = self._classes_by_label.get(_base_label(edge.target), [])
            if len(candidates) > LABEL_ATTACH_MAX:
                continue
            for base in candidates:
                if base.node_id != edge.source:
                    self.reverse[base.node_id].append(edge)
                    attached.add(base.node_id)
        # JS/TS/Java/Swift/Ruby classes record their bases on the node (`metadata.bases`)
        # instead of emitting `inherits` edges; give the walk the same hop.
        for node in nodes:
            metadata = node.metadata if isinstance(node.metadata, Mapping) else {}
            declared = metadata.get("bases") if getattr(node, "granularity", "") == "symbol" else None
            if not isinstance(declared, list):
                continue
            for raw in sorted({str(item) for item in declared}):
                candidates = self._classes_by_label.get(_base_label(f"name:{raw}"), [])
                if len(candidates) > LABEL_ATTACH_MAX:
                    continue
                for base in candidates:
                    if base.node_id == node.node_id:
                        continue
                    self.reverse[base.node_id].append(
                        GraphEdge(
                            edge_id=f"impacted:bases:{node.node_id}:{base.node_id}",
                            source=node.node_id,
                            target=base.node_id,
                            relation="inherits",
                            layer="STRUCTURAL",
                            confidence="EXTRACTED",
                            weight=1.0,
                            metadata={"source_file": node.source_ref, "from_metadata": True},
                        )
                    )
                    attached.add(base.node_id)
        for node_id in attached:
            self.reverse[node_id].sort(key=lambda item: (item.relation, item.source, item.target, item.edge_id))

    # -- C/C++ declaration <-> out-of-line definition --------------------------
    def _build_cpp_equivalents(self, nodes: Sequence[Any]) -> dict[str, list[str]]:
        """Group a header member declaration with its out-of-line definitions.

        Key ``(class, member label)``: the class is the definition's
        ``qualifier`` metadata, or the class whose span contains the
        declaration in the same file.  Only C/C++ files; only members.
        """
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for path, symbols in self.symbols_by_path.items():
            if PurePosixPath(path).suffix.lower() not in _CPP_SUFFIXES:
                continue
            classes = [node for node in symbols if node.kind in _CLASS_KINDS]
            for node in symbols:
                if node.kind in _CLASS_KINDS:
                    continue
                metadata = node.metadata if isinstance(node.metadata, Mapping) else {}
                owner = str(metadata.get("qualifier") or "")
                if not owner:
                    containing = [
                        cls for cls in classes
                        if cls.span_start <= node.span_start and cls.span_end >= node.span_end
                    ]
                    if containing:
                        owner = min(containing, key=lambda cls: (cls.span_end - cls.span_start, cls.node_id)).label
                if owner:
                    groups[(owner.split("::")[-1], node.label)].append(node.node_id)
        equivalents: dict[str, list[str]] = {}
        for members in groups.values():
            if len(members) < 2:
                continue
            ordered = sorted(set(members))
            for member in ordered:
                equivalents[member] = [other for other in ordered if other != member]
        return equivalents

    def equivalents(self, node_id: str) -> list[str]:
        return self._cpp_equivalents.get(node_id, [])

    def owners(self, node: Any) -> list[Any]:
        """Enclosing types of a changed symbol (span containment, or C++ qualifier)."""
        owners: dict[str, Any] = {}
        for candidate in self.symbols_by_path.get(node.source_ref, []):
            if (
                candidate.node_id != node.node_id
                and candidate.kind in _CLASS_KINDS
                and candidate.span_start <= node.span_start
                and candidate.span_end >= node.span_end
            ):
                owners[candidate.node_id] = candidate
        metadata = node.metadata if isinstance(node.metadata, Mapping) else {}
        qualifier = str(metadata.get("qualifier") or "").split("::")[-1]
        if qualifier and PurePosixPath(node.source_ref).suffix.lower() in _CPP_SUFFIXES:
            for candidate in self._classes_by_label.get(qualifier, []):
                if PurePosixPath(candidate.source_ref).suffix.lower() in _CPP_SUFFIXES:
                    owners[candidate.node_id] = candidate
        return [owners[key] for key in sorted(owners)]

    def innermost_symbol(self, path: str, line: int) -> Any | None:
        best = None
        for node in self.symbols_by_path.get(path, []):
            if node.span_start > line:
                break
            if node.span_end >= line and (
                best is None or (node.span_end - node.span_start, node.node_id) < (best.span_end - best.span_start, best.node_id)
            ):
                best = node
        return best

    def path_of(self, node_id: str) -> str:
        node = self.nodes.get(node_id)
        return node.source_ref if node is not None else ""


def _is_hub(node_id: str, index: _GraphIndex) -> bool:
    if node_id.startswith("target:"):
        return True
    path = index.path_of(node_id) if node_id.startswith("file:") else ""
    return bool(path) and is_build_config(path)


def _label(node_id: str, index: _GraphIndex) -> str:
    node = index.nodes.get(node_id)
    if node is None:
        return node_id
    if node.node_id.startswith("file:"):
        return node.source_ref
    return f"{node.source_ref}:{node.label}"


def _structural(
    seeds: list[tuple[str, int, str]],
    changed_path: str,
    index: _GraphIndex,
    selection: _Selection,
) -> None:
    """Bounded reverse BFS from one changed file's seeds.

    ``seeds`` are ``(node_id, depth, mode)`` with mode ``full`` (every
    structural relation), ``wiring`` (build/registration/instantiation of the
    changed symbol's file) or ``owner`` (instantiation/construction of the
    changed member's type).  Declaration/definition equivalents of a C/C++
    member are visited at the same depth.
    """
    allowed = {
        "full": STRUCTURAL_RELATIONS,
        "wiring": _WIRING_RELATIONS,
        "owner": _OWNER_RELATIONS,
        "include": frozenset({"imports"}),
    }
    best: dict[tuple[str, str], int] = {}
    frontier: dict[tuple[str, str], tuple[int, str]] = {}
    for node_id, depth, mode in seeds:
        key = (node_id, mode)
        if key not in frontier or depth < frontier[key][0]:
            frontier[key] = (depth, node_id)

    def reach_test(test: str, depth: int, relation: str, origin: str) -> None:
        selection.add(
            test, "structural", 1.0 / depth,
            f"{relation} path (depth {depth}) from {_label(origin, index)}",
            for_path=changed_path,
        )

    # Process strictly by depth so the first time a node is expanded is at its
    # minimal depth; ties are processed in sorted order.
    pending = sorted(((depth, node_id, mode, node_id) for (node_id, mode), (depth, _) in frontier.items()))
    while pending:
        depth, node_id, mode, origin = pending.pop(0)
        key = (node_id, mode)
        if key in best and best[key] <= depth:
            continue
        best[key] = depth
        if mode == "full":
            for other in index.equivalents(node_id):
                if (other, "full") not in best:
                    pending.append((depth, other, "full", origin))
        if depth >= STRUCTURAL_MAX_DEPTH:
            continue
        next_items: list[tuple[int, str, str, str]] = []
        for edge in index.reverse.get(node_id, []):
            if edge.relation not in allowed[mode]:
                continue
            if mode == "owner" and edge.relation == "calls":
                node = index.nodes.get(node_id)
                if node is None or node.kind not in _CLASS_KINDS:
                    continue
            next_depth = depth + 1
            metadata = edge.metadata if isinstance(edge.metadata, Mapping) else {}
            site = str(metadata.get("source_file") or "")
            if site and site != index.path_of(edge.source) and is_runnable_test(site):
                # e.g. connect(&d, &Device::found, &d, &Device::onFound) written
                # in a test: both endpoints are header symbols, the site is the test.
                reach_test(site, next_depth, edge.relation, origin)
            source_path = index.path_of(edge.source)
            if source_path and is_runnable_test(source_path):
                reach_test(source_path, next_depth, edge.relation, origin)
                continue
            if edge.source not in index.nodes:
                continue
            next_items.append((next_depth, edge.source, "full", origin))
            if (
                edge.relation in _HUB_FORWARD_RELATIONS
                and _is_hub(edge.source, index)
                and next_depth + 1 <= STRUCTURAL_MAX_DEPTH
            ):
                # A CMake target / .qrc that builds or registers the changed
                # file: the tests it also builds are impacted (sibling hop).
                for forward in index.forward_hub.get(edge.source, []):
                    target_path = index.path_of(forward.target)
                    if target_path and is_runnable_test(target_path):
                        reach_test(target_path, next_depth + 1, forward.relation, origin)
        pending.extend(next_items)
        pending.sort()


def _runnable_tests_by_base(paths: Iterable[str]) -> dict[str, list[str]]:
    """Normalised file-name base -> sorted runnable test paths, so pairing a changed file
    is one lookup instead of a scan (and sort) of every known path."""
    index: dict[str, list[str]] = defaultdict(list)
    for position, candidate in enumerate(sorted(paths)):
        if position % 5000 == 0:
            check_deadline()
        if is_runnable_test(candidate):
            index[test_base_name(candidate)[0]].append(candidate)
    return index


def select_tests(
    changed: Sequence[Any],
    raw_zero_context: str,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    *,
    all_paths: Iterable[str],
    indexed_paths: set[str],
    cochange: Mapping[str, Sequence[tuple[str, float, Any]]],
    index_status: str,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Return ``(tests_to_run, tests_run_all, tests_run_all_reason)``."""
    tests_to_run, run_all, reason, _by_file, _uncovered = select_tests_with_coverage(
        changed, raw_zero_context, nodes, edges,
        all_paths=all_paths, indexed_paths=indexed_paths, cochange=cochange, index_status=index_status,
    )
    return tests_to_run, run_all, reason


def select_tests_with_coverage(
    changed: Sequence[Any],
    raw_zero_context: str,
    nodes: Sequence[Any],
    edges: Sequence[Any],
    *,
    all_paths: Iterable[str],
    indexed_paths: set[str],
    cochange: Mapping[str, Sequence[tuple[str, float, Any]]],
    index_status: str,
) -> tuple[list[dict[str, Any]], bool, str, dict[str, list[str]], list[str]]:
    """`select_tests` plus which tests were chosen for each changed file and which
    reviewable changed files no test reaches (what the run-all reason refers to)."""
    index = _GraphIndex(nodes, edges)
    lines_by_path = parse_changed_lines(raw_zero_context)
    known_paths = set(all_paths) | set(indexed_paths)
    selection = _Selection()
    deleted: list[str] = []
    unindexed: list[str] = []
    reviewable: list[str] = []  # changed files that must be covered by some tier
    tests_by_base: dict[str, list[str]] | None = None  # built on the first non-test file

    for item in sorted(changed, key=lambda value: value.path):
        check_deadline()
        path = item.path
        if item.status == "D":
            if is_runnable_test(path):
                continue
            if not is_documentation(path):
                deleted.append(path)
            continue
        if is_runnable_test(path):
            selection.add(path, "changed", CHANGED_SCORE, "test file is part of this diff", for_path=path)
            continue
        if is_documentation(path):
            continue
        reviewable.append(path)
        if path not in indexed_paths:
            unindexed.append(path)

        # (a) structural
        file_id = f"file:{path}"
        seeds: list[tuple[str, int, str]] = []
        changed_lines = lines_by_path.get(path) or (
            lines_by_path.get(item.previous_path) if item.previous_path else None
        )
        has_symbols = bool(index.symbols_by_path.get(path))
        whole_file = item.binary or changed_lines is None or not changed_lines.lines or not has_symbols
        if not whole_file:
            for line in sorted(changed_lines.lines):
                symbol = index.innermost_symbol(path, line)
                if symbol is None:
                    whole_file = True
                else:
                    seeds.append((symbol.node_id, 0, "full"))
        if whole_file:
            seeds.append((file_id, 0, "full"))
        changed_symbols = sorted({node_id for node_id, _depth, _mode in seeds if node_id != file_id})
        if changed_symbols:
            seeds.append((file_id, 0, "wiring"))
            if PurePosixPath(path).suffix.lower() in _CPP_HEADER_SUFFIXES:
                # `#include` is textual: an edit inside a header's class or
                # function reaches every translation unit that includes it.
                seeds.append((file_id, 0, "include"))
            for symbol_id in changed_symbols:
                for owner in index.owners(index.nodes[symbol_id]):
                    seeds.append((owner.node_id, 1, "owner"))
        if file_id in index.nodes or changed_symbols:
            _structural(seeds, path, index, selection)

        # (b) cochange (the old path's history for a file renamed in this diff)
        partners = list(cochange.get(path, [])) or (list(cochange.get(item.previous_path, [])) if item.previous_path else [])
        for partner, weight, _edge in partners:
            if weight >= COCHANGE_TEST_THRESHOLD and is_runnable_test(partner) and partner in known_paths:
                selection.add(
                    partner, "cochange", float(weight),
                    f"changed together with {path} (weight {float(weight):.2f})", for_path=path,
                )

        # (c) filename pairing
        if not is_test_path(path):
            if tests_by_base is None:
                tests_by_base = _runnable_tests_by_base(known_paths)
            for candidate in tests_by_base.get(test_base_name(path)[0], ()):
                if pairs_with_test(path, candidate):
                    selection.add(candidate, "pair", PAIR_SCORE, f"test file name pairs with {path}", for_path=path)

    deleted_tests = {item.path for item in changed if item.status == "D"}
    for test in list(selection.hits):
        if test in deleted_tests:
            del selection.hits[test]
    tests_to_run = selection.items()
    tests_by_changed_file = {path: sorted(tests) for path, tests in sorted(selection.tests_for.items())}
    uncovered_changed_files = sorted(path for path in reviewable if path not in selection.covered)

    reason = ""
    build_changes = sorted(item.path for item in changed if is_build_config(item.path))
    if index_status != "available":
        reason = f"Cortex index is {index_status}; impacted tests cannot be traced"
    elif build_changes:
        reason = f"build/config file changed: {build_changes[0]}"
    elif not any(is_runnable_test(path) for path in indexed_paths):
        reason = "no test files are indexed"
    elif deleted:
        reason = f"deleted file {deleted[0]}: its former dependents cannot be traced from the current index"
    elif unindexed:
        reason = f"changed file is not indexed: {unindexed[0]}"
    else:
        uncovered = [path for path in reviewable if path not in selection.covered]
        if uncovered:
            reason = f"no test reaches changed file {uncovered[0]}"
    return tests_to_run, bool(reason), reason, tests_by_changed_file, uncovered_changed_files


__all__ = [
    "BUILD_CONFIG_NAMES",
    "BUILD_CONFIG_SUFFIXES",
    "COCHANGE_TEST_THRESHOLD",
    "STRUCTURAL_MAX_DEPTH",
    "STRUCTURAL_RELATIONS",
    "ChangedLines",
    "is_build_config",
    "is_documentation",
    "is_runnable_test",
    "parse_changed_lines",
    "select_tests",
    "select_tests_with_coverage",
]
