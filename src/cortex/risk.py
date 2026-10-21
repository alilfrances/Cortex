"""Deterministic, local-only diff risk analysis.

The risk command deliberately keeps git parsing separate from graph analysis.  It
runs ``git`` with an argument list (never through a shell), reads only the
requested diff and the local Cortex index, and never contacts a remote.  The
normalised components and weights below are part of the public contract so a
risk result can be reproduced in a hook or CI job.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import posixpath
from pathlib import Path
import re
import subprocess
from collections.abc import Callable
from typing import Any, Iterable, Mapping, Sequence

from .deadline import check_deadline
from .gitutils import discover_repo_root
from .impacted_tests import select_tests_with_coverage
from .store import CortexStore, default_db_path
from .testpatterns import is_test_path, pairs_with_test, test_name_info
from .tokenizer import count_text_tokens

# A direct COCHANGE edge is already normalised to [0, 1] by cochange.py.  A
# partner above this floor is actionable rather than a weak historical
# association.  Keep this value fixed: changing it changes CI advice.
COCHANGE_THRESHOLD = 0.50

# Fixed, documented score policy.  Every component is clamped to [0, 1], then
# multiplied by its weight and scaled to 0..10.  ``directives`` includes only
# actionable missing-test/Qt/build-reference advice (missing cochange has its
# own component), not analysis errors or unindexed notices.
RISK_WEIGHTS: dict[str, float] = {
    "diff": 0.30,
    "hotspot": 0.20,
    "fan_in": 0.15,
    "cochange": 0.15,
    "directives": 0.20,
}
DIFF_CHURN_SCALE = 100
HOTSPOT_SCALE = 1000
FAN_IN_SCALE = 10
DIRECTIVE_SCALE = 3

_CPP_IMPL_SUFFIXES = {".c", ".cpp", ".cc", ".cxx"}
_CPP_HEADER_SUFFIXES = {".h", ".hpp", ".hh", ".hxx"}
_QML_SUFFIX = ".qml"
_CONFIG_NAMES = {"cmakelists.txt"}
_CONFIG_SUFFIXES = {".qrc", ".cmake"}
_SOURCE_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".java", ".rb", ".go", ".rs",
    ".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".qml",
}

_SIGNAL_DECL_RE = re.compile(
    r"\b(?:signal\s+)?(?P<name>[A-Za-z_]\w*)\s*\([^;{}\n]*\)\s*(?:const\s*)?;"
)
_QML_SIGNAL_DECL_RE = re.compile(r"^\s*signal\s+(?P<name>[A-Za-z_]\w*)\s*\(")
class RiskAnalysisError(ValueError):
    """An expected, user-facing failure while obtaining a diff."""

    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DiffFile:
    """One changed path as represented by git's name/status and numstat data."""

    path: str
    status: str
    additions: int = 0
    deletions: int = 0
    previous_path: str | None = None
    rename_score: str | None = None
    binary: bool = False
    added_lines: tuple[str, ...] = ()
    removed_lines: tuple[str, ...] = ()

    @property
    def churn(self) -> int:
        return self.additions + self.deletions

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "status": self.status,
            "additions": self.additions,
            "deletions": self.deletions,
            "churn": self.churn,
            "binary": self.binary,
        }
        if self.previous_path is not None:
            payload["previous_path"] = self.previous_path
        if self.rename_score is not None:
            payload["rename_score"] = self.rename_score
        return payload


@dataclass
class _DiffData:
    files: list[DiffFile] = field(default_factory=list)
    raw_zero_context: str = ""


def _git(repo_root: Path, args: Sequence[str]) -> bytes:
    """Run one local git command without invoking a shell or a network."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RiskAnalysisError("git_error", f"Could not run git: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RiskAnalysisError("diff_error", detail or "git diff failed", detail=detail)
    return completed.stdout


def _validate_range_spec(range_spec: str | None) -> None:
    if range_spec is None:
        return
    if not range_spec or range_spec.startswith("-") or "\0" in range_spec or len(range_spec) > 4096:
        raise RiskAnalysisError(
            "invalid_range",
            "Git range must be a non-option revision or revision range",
        )


def _diff_args(range_spec: str | None, staged: bool, *options: str) -> list[str]:
    # Disable configured external diff/textconv drivers: risk is a local,
    # deterministic parser, not an invitation to execute repository hooks.
    args = [*options, "--no-ext-diff", "--no-textconv", "--find-renames"]
    if staged:
        args.append("--cached")
    if range_spec:
        args.append(range_spec)
    # The delimiter is important for paths and protects the parser from
    # accidentally treating a user-provided value as a pathspec.  Range values
    # are still passed as one argv element; no shell expansion is possible.
    args.append("--")
    return args


def _ensure_diffable(repo_root: Path, range_spec: str | None, staged: bool) -> None:
    try:
        _git(repo_root, ["rev-parse", "--show-toplevel"])
    except RiskAnalysisError as exc:
        raise RiskAnalysisError("not_git", f"{repo_root} is not inside a git repository") from exc

    # A staged diff can be meaningful in an unborn repository.  A revision
    # range cannot; give a useful no-commit error instead of exposing git's
    # implementation-specific wording.
    if range_spec or not staged:
        try:
            _git(repo_root, ["rev-parse", "--verify", "HEAD"])
        except RiskAnalysisError as exc:
            raise RiskAnalysisError("no_commits", "The repository has no commit to diff") from exc

    if range_spec and ".." in range_spec:
        # Detect the common shallow-clone failure distinctly.  Do not reject
        # valid single-revision specs; git remains the authority on syntax.
        try:
            shallow = _git(repo_root, ["rev-parse", "--is-shallow-repository"]).decode().strip()
        except RiskAnalysisError:
            shallow = ""
        try:
            _git(repo_root, ["rev-parse", "--verify", range_spec.split("..", 1)[0]])
        except RiskAnalysisError as exc:
            if shallow == "true":
                raise RiskAnalysisError(
                    "shallow_history",
                    f"Cannot resolve {range_spec!r} in this shallow repository; deepen history or use a local commit range",
                ) from exc
            raise RiskAnalysisError(
                "insufficient_history",
                f"Cannot resolve {range_spec!r}; the repository does not contain both range endpoints",
            ) from exc


def _decode_z(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="surrogateescape").split("\0")


def parse_name_status(data: bytes | str) -> list[dict[str, str | None]]:
    """Parse ``git diff --name-status -z --find-renames`` deterministically."""
    raw = data.encode("utf-8", errors="surrogateescape") if isinstance(data, str) else data
    parts = _decode_z(raw)
    records: list[dict[str, str | None]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        code = status[0]
        if code in {"R", "C"} and index + 1 < len(parts):
            previous_path, path = parts[index], parts[index + 1]
            index += 2
            records.append({"status": code, "score": status[1:] or None, "path": path, "previous_path": previous_path})
        elif index < len(parts):
            path = parts[index]
            index += 1
            records.append({"status": code, "score": status[1:] or None, "path": path, "previous_path": None})
    return records


def _parse_numstat(data: bytes | str) -> list[tuple[int, int, bool, str | None, str | None]]:
    """Parse numstat -z rows as (adds, deletes, binary, old, new)."""
    raw = data.encode("utf-8", errors="surrogateescape") if isinstance(data, str) else data
    parts = _decode_z(raw)
    result: list[tuple[int, int, bool, str | None, str | None]] = []
    index = 0
    while index < len(parts):
        row = parts[index]
        index += 1
        if not row:
            continue
        fields = row.split("\t", 2)
        if len(fields) != 3:
            continue
        added_raw, deleted_raw, path_field = fields
        binary = added_raw == "-" or deleted_raw == "-"
        adds = 0 if binary else _safe_int(added_raw)
        deletes = 0 if binary else _safe_int(deleted_raw)
        # A rename has an empty path field followed by old and new path NUL
        # records (Git's numstat format), while a normal row has path inline.
        if not path_field and index + 1 < len(parts):
            old_path, new_path = parts[index], parts[index + 1]
            index += 2
            result.append((adds, deletes, binary, old_path, new_path))
        else:
            result.append((adds, deletes, binary, None, path_field))
    return result


def _safe_int(value: str) -> int:
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def parse_numstat(data: bytes | str) -> list[dict[str, Any]]:
    """Return JSON-friendly rows from ``git diff --numstat -z`` output."""
    return [
        {
            "additions": additions,
            "deletions": deletions,
            "binary": binary,
            "previous_path": previous,
            "path": path,
        }
        for additions, deletions, binary, previous, path in _parse_numstat(data)
    ]


def parse_zero_context_diff(text: bytes | str) -> dict[str, dict[str, list[str]]]:
    """Extract added/removed lines from a zero-context unified diff.

    The result is keyed by the new path where possible.  Rename-only diffs are
    represented with empty line lists.  File headers are deliberately ignored,
    so ``+++``/``---`` cannot be mistaken for source changes.
    """
    raw = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else text
    result: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    old_path: str | None = None
    new_path: str | None = None
    for line in raw.splitlines():
        if line.startswith("diff --git "):
            current = None
            old_path = new_path = None
            continue
        if line.startswith("--- "):
            old_path = _diff_header_path(line[4:])
            continue
        if line.startswith("+++ "):
            new_path = _diff_header_path(line[4:])
            current = new_path or old_path
            if current:
                result.setdefault(current, {"added": [], "removed": []})
            continue
        if line.startswith("Binary files "):
            if current:
                result.setdefault(current, {"added": [], "removed": []})
            continue
        if line.startswith("@@"):
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            result.setdefault(current, {"added": [], "removed": []})["added"].append(line[1:])
        elif current and line.startswith("-") and not line.startswith("---"):
            result.setdefault(current, {"added": [], "removed": []})["removed"].append(line[1:])
    return result


def _diff_header_path(value: str) -> str | None:
    path = value.strip().split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _collect_diff(repo_root: Path, range_spec: str | None, staged: bool) -> _DiffData:
    name_records = parse_name_status(_git(repo_root, _diff_args(range_spec, staged, "diff", "--name-status", "-z", "--no-color")))
    num_records = _parse_numstat(_git(repo_root, _diff_args(range_spec, staged, "diff", "--numstat", "-z", "--no-color")))
    zero_context = _git(
        repo_root,
        _diff_args(range_spec, staged, "diff", "--unified=0", "--no-color", "--no-ext-diff"),
    ).decode("utf-8", errors="replace")
    line_changes = parse_zero_context_diff(zero_context)

    # Name-status is authoritative for order/status.  Numstat is normally in
    # the same order, but matching paths makes this safe around a mix of
    # renames, binary files, and deletions.
    by_path: dict[str, list[tuple[int, int, bool, str | None, str | None]]] = defaultdict(list)
    for record in num_records:
        _adds, _deletes, _binary, old, new = record
        by_path[new or old or ""].append(record)
    files: list[DiffFile] = []
    for record in name_records:
        path = str(record.get("path") or "")
        if not path:
            continue
        status = str(record.get("status") or "M")
        previous = record.get("previous_path")
        rename_score = str(record.get("score")) if record.get("score") is not None else None
        candidates = by_path.get(path, [])
        if not candidates and previous:
            candidates = by_path.get(str(previous), [])
        if candidates:
            adds, deletes, binary, _old, _new = candidates.pop(0)
        else:
            adds = deletes = 0
            binary = False
        changed = line_changes.get(path) or line_changes.get(str(previous or "")) or {"added": [], "removed": []}
        files.append(
            DiffFile(
                path=path,
                status=status,
                additions=adds,
                deletions=deletes,
                previous_path=str(previous) if previous else None,
                rename_score=rename_score,
                binary=binary,
                added_lines=tuple(changed.get("added", [])),
                removed_lines=tuple(changed.get("removed", [])),
            )
        )
    return _DiffData(files=files, raw_zero_context=zero_context)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _normalised_components(
    *, churn: int, hotspot_score: int, fan_in: int, missing_weight: float, directive_count: int
) -> dict[str, float]:
    return {
        "diff": _clamp(churn / DIFF_CHURN_SCALE),
        "hotspot": _clamp(hotspot_score / HOTSPOT_SCALE),
        "fan_in": _clamp(fan_in / FAN_IN_SCALE),
        "cochange": _clamp(missing_weight),
        "directives": _clamp(directive_count / DIRECTIVE_SCALE),
    }


def risk_score(components: Mapping[str, float]) -> float:
    """Apply the documented fixed weights and return a rounded 0..10 score."""
    total = sum(RISK_WEIGHTS[key] * _clamp(float(components.get(key, 0.0))) for key in RISK_WEIGHTS)
    return round(max(0.0, min(10.0, total * 10.0)), 2)


def _graph_maps(nodes: Sequence[Any], edges: Sequence[Any]) -> tuple[dict[str, Any], dict[str, list[tuple[str, float, Any]]], dict[str, int]]:
    file_nodes: dict[str, Any] = {}
    node_by_id: dict[str, Any] = {}
    for node in nodes:
        node_by_id[node.node_id] = node
        if node.kind == "file" and not node.node_id.startswith("commit:"):
            file_nodes[node.source_ref] = node
    cochange: dict[str, list[tuple[str, float, Any]]] = defaultdict(list)
    fan_in: dict[str, int] = defaultdict(int)
    for edge in edges:
        if edge.layer == "COCHANGE" and edge.relation == "cochange":
            source = edge.source.removeprefix("file:")
            target = edge.target.removeprefix("file:")
            if source in file_nodes and target in file_nodes:
                cochange[source].append((target, float(edge.weight), edge))
                cochange[target].append((source, float(edge.weight), edge))
        if edge.layer != "STRUCTURAL":
            continue
        target_node = node_by_id.get(edge.target)
        if target_node is None:
            continue
        target_path = target_node.source_ref
        source_node = node_by_id.get(edge.source)
        if source_node is not None and source_node.source_ref == target_path:
            continue
        if target_path in file_nodes:
            fan_in[target_path] += 1
    for path in cochange:
        cochange[path].sort(key=lambda item: (-item[1], item[0], item[2].edge_id))
    return file_nodes, cochange, fan_in


def _indexed_data(repo_root: Path, db_path: Path | None, nodes: Sequence[Any] | None, edges: Sequence[Any] | None) -> tuple[list[Any], list[Any], set[str], str]:
    if nodes is not None and edges is not None:
        paths = {node.source_ref for node in nodes if node.kind == "file"}
        return list(nodes), list(edges), paths, "available"
    resolved_db = db_path or default_db_path(repo_root)
    if not resolved_db.exists():
        return [], [], set(), "missing"
    try:
        store = CortexStore(resolved_db)
        graph_nodes, graph_edges = store.fetch_graph(repo_root)
        paths = {node.source_ref for node in graph_nodes if node.kind == "file"}
        return graph_nodes, graph_edges, paths, "available"
    except Exception:
        return [], [], set(), "unreadable"


def _repo_files(repo_root: Path) -> set[str]:
    try:
        output = _git(repo_root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"])
    except RiskAnalysisError:
        return set()
    return {item for item in _decode_z(output) if item}


def _changed_paths(files: Sequence[DiffFile]) -> set[str]:
    return {item.path for item in files}


_PAIR_INDEX_CACHE: list[Any] = []


def _pair_index(paths: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
    """stem -> [(path, lowercase suffix)] over `paths`, built once per path set.

    Pairing a header with its implementation used to scan every path for every
    path, which made `cortex_risk` quadratic in file count. Sets are memoised by
    identity and size (holding a strong reference, so a recycled `id()` can never
    serve a stale index).
    """
    memoisable = isinstance(paths, (set, frozenset))
    if memoisable and _PAIR_INDEX_CACHE and _PAIR_INDEX_CACHE[0] is paths and _PAIR_INDEX_CACHE[1] == len(paths):
        return _PAIR_INDEX_CACHE[2]
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for candidate in paths:
        stem, suffix = posixpath.splitext(posixpath.basename(candidate))
        index[stem].append((candidate, suffix.lower()))
    if memoisable:
        _PAIR_INDEX_CACHE[:] = [paths, len(paths), index]
    return index


def _pair_for(path: str, paths: Iterable[str]) -> list[str]:
    suffix = Path(path).suffix.lower()
    if suffix not in _CPP_HEADER_SUFFIXES | _CPP_IMPL_SUFFIXES:
        return []
    stem = Path(path).stem
    allowed = _CPP_IMPL_SUFFIXES if suffix in _CPP_HEADER_SUFFIXES else _CPP_HEADER_SUFFIXES
    return sorted(
        candidate for candidate, candidate_suffix in _pair_index(paths).get(stem, ())
        if candidate != path and candidate_suffix in allowed
    )


def _is_qt_path(path: str, qt_paths: set[str] | None = None) -> bool:
    """Return Qt status only from indexed metadata or resolved edge metadata.

    Source markers are intentionally not sufficient: an unindexed file with a
    coincidental ``Q_OBJECT``/``connect`` token must not create Qt advice.
    """
    return bool(qt_paths and path in qt_paths)


def _pair_directives(changed: Sequence[DiffFile], all_paths: set[str], nodes: Sequence[Any], edges: Sequence[Any]) -> list[dict[str, str]]:
    qt_paths = {
        node.source_ref
        for node in nodes
        if isinstance(getattr(node, "metadata", None), Mapping) and node.metadata.get("qt")
    }
    qt_paths.update(
        str(edge.metadata.get("source_file", ""))
        for edge in edges
        if edge.relation in {"emits", "connects", "handles", "instantiates", "binds", "reads", "writes", "aliases", "exports"}
        and edge.metadata.get("source_file")
    )
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in changed:
        for partner in _pair_for(item.path, all_paths):
            if partner in _changed_paths(changed):
                continue
            if not (_is_qt_path(item.path, qt_paths) or _is_qt_path(partner, qt_paths)):
                continue
            key = (item.path, partner)
            if key in seen:
                continue
            seen.add(key)
            result.append({"changed": item.path, "partner": partner, "reason": "Qt header/implementation partner is untouched"})
    return sorted(result, key=lambda value: (value["changed"], value["partner"]))


def _test_directives(changed: Sequence[DiffFile], all_paths: set[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    changed_paths = _changed_paths(changed)
    tests_by_base: dict[str, list[str]] | None = None
    for item in changed:
        check_deadline()
        if Path(item.path).suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if is_test_path(item.path):
            continue
        if tests_by_base is None:
            tests_by_base = defaultdict(list)
            for candidate in all_paths:
                info = test_name_info(candidate)
                if info.is_test:
                    tests_by_base[info.base].append(candidate)
        candidates = sorted(
            candidate for candidate in tests_by_base.get(test_name_info(item.path).base, ())
            if candidate not in changed_paths and pairs_with_test(item.path, candidate)
        )
        for candidate in candidates:
            result.append({"source": item.path, "test": candidate, "reason": "paired test is untouched"})
            break
    return result


def _qt_instantiation_directives(
    changed: Sequence[DiffFile], nodes: Sequence[Any], edges: Sequence[Any], all_paths: set[str]
) -> list[dict[str, str]]:
    changed_paths = _changed_paths(changed)
    nodes_by_id = {node.node_id: node for node in nodes}
    pair_map: dict[str, set[str]] = defaultdict(set)
    for path in all_paths:
        for partner in _pair_for(path, all_paths):
            pair_map[path].add(partner)
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, edge in enumerate(edges):
        if index % 5000 == 0:
            check_deadline()
        if edge.layer != "STRUCTURAL" or edge.relation != "instantiates":
            continue
        source_node = nodes_by_id.get(edge.source)
        target_node = nodes_by_id.get(edge.target)
        if source_node is None or target_node is None:
            # Unresolved module:<Type> edges are intentionally ignored.  A
            # filename/type guess here would fabricate a Qt relationship.
            continue
        qml_path = source_node.source_ref
        target_path = target_node.source_ref
        if not qml_path.lower().endswith(_QML_SUFFIX):
            continue
        related_changed = target_path in changed_paths or bool(pair_map.get(target_path, set()) & changed_paths)
        if not related_changed or qml_path in changed_paths:
            continue
        key = (target_path, qml_path)
        if key in seen:
            continue
        seen.add(key)
        result.append({"changed": target_path, "qml": qml_path, "reason": "resolved QML instantiation site is untouched"})
    return sorted(result, key=lambda value: (value["changed"], value["qml"]))


def _signal_names(item: DiffFile) -> set[str]:
    names: set[str] = set()
    for line in (*item.added_lines, *item.removed_lines):
        qml_match = _QML_SIGNAL_DECL_RE.match(line)
        if qml_match:
            names.add(qml_match.group("name"))
        match = _SIGNAL_DECL_RE.search(line)
        if match and "connect" not in line and "SLOT" not in line and "SIGNAL" not in line:
            names.add(match.group("name"))
    return names


def _qt_signal_site_directives(changed: Sequence[DiffFile], nodes: Sequence[Any], edges: Sequence[Any]) -> list[dict[str, str]]:
    changed_paths = _changed_paths(changed)
    changed_names_by_path = {item.path: _signal_names(item) for item in changed}
    signal_ids: dict[str, set[str]] = defaultdict(set)
    node_by_id = {node.node_id: node for node in nodes}
    for node in nodes:
        if node.metadata.get("qt") == "signal":
            signal_ids[node.label].add(node.node_id)
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in changed:
        names = changed_names_by_path.get(item.path, set())
        if not names:
            continue
        # Declaration changes are meaningful only when the changed path is a
        # file with an indexed Qt signal of that name, or when the diff itself
        # has a recognisable signal declaration.  The edge still must resolve.
        for edge in edges:
            if edge.layer != "STRUCTURAL" or edge.relation not in {"connects", "handles"}:
                continue
            signal_name = str(edge.metadata.get("signal_name", ""))
            signal_endpoint = edge.source if edge.relation == "connects" else edge.target
            endpoint_node = node_by_id.get(signal_endpoint)
            resolved_name = endpoint_node.label if endpoint_node is not None and endpoint_node.metadata.get("qt") == "signal" else ""
            if signal_name not in names and resolved_name not in names:
                continue
            if endpoint_node is None or endpoint_node.metadata.get("qt") != "signal":
                # Explicitly do not turn a module placeholder into a guessed
                # relationship.  P0-4's resolved graph is the authority.
                continue
            site = str(edge.metadata.get("source_file", ""))
            if not site:
                source_node = node_by_id.get(edge.source)
                site = source_node.source_ref if source_node is not None else ""
            if not site or site in changed_paths:
                continue
            key = (item.path, site, edge.relation)
            if key in seen:
                continue
            seen.add(key)
            result.append({"signal": resolved_name or signal_name, "site": site, "relation": edge.relation, "reason": "resolved Qt site is untouched"})
    return sorted(result, key=lambda value: (value["signal"], value["site"], value["relation"]))


def _build_reference_miss_directives(repo_root: Path, changed: Sequence[DiffFile], repo_files: set[str]) -> list[dict[str, str]]:
    config_paths = sorted(
        path for path in repo_files
        if Path(path).name.lower() in _CONFIG_NAMES or Path(path).suffix.lower() in _CONFIG_SUFFIXES
    )
    texts: dict[str, str] = {}
    for path in config_paths:
        try:
            texts[path] = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    result: list[dict[str, str]] = []
    for item in changed:
        if item.status not in {"A", "C", "R"} or not item.path.lower().endswith(_QML_SUFFIX):
            continue
        path = item.path.replace("\\", "/")
        basename = Path(path).name
        if any(path in text or basename in text for text in texts.values()):
            continue
        result.append({"qml": path, "reason": "new QML file is absent from current CMakeLists/qrc references"})
    return sorted(result, key=lambda value: value["qml"])


def _directive_strings(
    missing_cochange: Sequence[dict[str, Any]],
    missing_tests: Sequence[dict[str, Any]],
    qt_pairs: Sequence[dict[str, Any]],
    qt_instantiations: Sequence[dict[str, Any]],
    qt_signal_sites: Sequence[dict[str, Any]],
    build_system_misses: Sequence[dict[str, Any]],
    tests_to_run: Sequence[dict[str, Any]] = (),
) -> list[str]:
    directives: list[str] = []
    for item in missing_cochange:
        directives.append(f"missing_cochange: {item['path']} ({float(item['weight']):.2f}) for {item['for']}")
    for item in missing_tests:
        directives.append(f"missing_tests: {item['test']} for {item['source']}")
    for item in qt_pairs:
        directives.append(f"missing_qt_pair: {item['partner']} for {item['changed']}")
    for item in qt_instantiations:
        directives.append(f"missing_qt_instantiation: {item['qml']} for {item['changed']}")
    for item in qt_signal_sites:
        directives.append(f"missing_qt_site: {item['site']} ({item['relation']} {item['signal']})")
    for item in build_system_misses:
        directives.append(f"build_system_miss: {item['qml']}")
    for item in tests_to_run:
        directives.append(f"tests_to_run: {item['test']} ({','.join(item['via'])})")
    return sorted(directives)


def analyze_risk(
    repo_path: Path | str,
    range_spec: str | None = None,
    *,
    staged: bool = False,
    db_path: Path | None = None,
    nodes: Sequence[Any] | None = None,
    edges: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Analyze an explicit git range, or ``HEAD~1..HEAD`` by default.

    The returned mapping is intentionally JSON-ready.  Expected git/index
    problems are represented by ``status``/``error`` fields instead of being
    raised, which lets both CLI and MCP remain useful in shallow, new, or
    unindexed repositories.
    """
    try:
        repo_root = discover_repo_root(Path(repo_path))
    except (ValueError, OSError) as exc:
        return {"status": "error", "error": "not_git", "message": str(exc), "files": [], "directives": []}
    requested_range = range_spec or (None if staged else "HEAD~1..HEAD")
    try:
        _validate_range_spec(requested_range)
        _ensure_diffable(repo_root, requested_range, staged)
        diff = _collect_diff(repo_root, requested_range, staged)
    except RiskAnalysisError as exc:
        return {
            "status": "error",
            "error": exc.code,
            "message": str(exc),
            "range": requested_range or "INDEX",
            "staged": staged,
            "files": [],
            "directives": [],
        }

    graph_nodes, graph_edges, indexed_paths, index_status = _indexed_data(repo_root, db_path, nodes, edges)
    file_nodes, cochange, fan_in = _graph_maps(graph_nodes, graph_edges)
    repo_files = _repo_files(repo_root)
    all_paths = set(repo_files) | indexed_paths
    changed_paths = _changed_paths(diff.files)

    missing_cochange: list[dict[str, Any]] = []
    file_payloads: list[dict[str, Any]] = []
    for item in diff.files:
        node = file_nodes.get(item.path)
        if node is None and item.previous_path:
            node = file_nodes.get(item.previous_path)
        raw_hotspot = node.metadata.get("hotspot", {}) if node is not None else {}
        hotspot = raw_hotspot if isinstance(raw_hotspot, Mapping) else {}
        hotspot_values = {
            "churn": int(hotspot.get("churn", 0) or 0),
            "complexity": int(hotspot.get("complexity", 0) or 0),
            "score": int(hotspot.get("score", 0) or 0),
        }
        missing_for_file: list[dict[str, Any]] = []
        # A file renamed in this diff has no history under its new path until
        # the next ingest; fall back to the co-change partners of its old path.
        partners = cochange.get(item.path) or (cochange.get(item.previous_path, []) if item.previous_path else [])
        for partner, weight, _edge in partners:
            if weight <= COCHANGE_THRESHOLD or partner in changed_paths:
                continue
            record = {"path": partner, "weight": round(weight, 3), "for": item.path}
            missing_cochange.append(record)
            missing_for_file.append(record)
        components = _normalised_components(
            churn=item.churn,
            hotspot_score=hotspot_values["score"],
            fan_in=fan_in.get(item.path, 0),
            missing_weight=max((float(record["weight"]) for record in missing_for_file), default=0.0),
            directive_count=0,
        )
        score = risk_score(components)
        file_payloads.append(
            {
                **item.to_dict(),
                "hotspot": hotspot_values,
                "fan_in": fan_in.get(item.path, 0),
                "structural_fan_in": fan_in.get(item.path, 0),
                "missing_cochange": sorted(missing_for_file, key=lambda value: (-value["weight"], value["path"])),
                "components": components,
                "risk_score": score,
                "risk": score,
                "score": score,
                "indexed": item.path in indexed_paths or bool(item.previous_path and item.previous_path in indexed_paths),
            }
        )

    check_deadline()
    missing_tests = _test_directives(diff.files, all_paths)
    check_deadline()
    qt_pairs = _pair_directives(diff.files, all_paths, graph_nodes, graph_edges)
    check_deadline()
    qt_instantiations = _qt_instantiation_directives(diff.files, graph_nodes, graph_edges, all_paths)
    check_deadline()
    qt_signal_sites = _qt_signal_site_directives(diff.files, graph_nodes, graph_edges)
    check_deadline()
    build_system_misses = _build_reference_miss_directives(repo_root, diff.files, all_paths)
    check_deadline()

    actionable_by_file: dict[str, int] = defaultdict(int)
    for record in missing_tests:
        actionable_by_file[record["source"]] += 1
    for record in qt_pairs:
        actionable_by_file[record["changed"]] += 1
    for record in qt_instantiations:
        actionable_by_file[record["changed"]] += 1
    for record in qt_signal_sites:
        # A declaration file is not in the site record, so this directive is
        # credited to every changed file with the signal name below only when
        # it is otherwise the source of the declaration.  It remains in the
        # global directives regardless.
        for item in diff.files:
            if item.path in changed_paths and record.get("signal") in _signal_names(item):
                actionable_by_file[item.path] += 1
    for record in build_system_misses:
        actionable_by_file[record["qml"]] += 1

    for payload in file_payloads:
        path = payload["path"]
        components = _normalised_components(
            churn=int(payload["churn"]),
            hotspot_score=int(payload["hotspot"]["score"]),
            fan_in=int(payload["fan_in"]),
            missing_weight=max((float(value["weight"]) for value in payload["missing_cochange"]), default=0.0),
            directive_count=actionable_by_file.get(path, 0),
        )
        payload["components"] = components
        payload["risk_score"] = risk_score(components)
        payload["risk"] = payload["risk_score"]
        payload["score"] = payload["risk_score"]

    file_payloads.sort(key=lambda value: (-float(value["risk_score"]), str(value["path"]), str(value.get("previous_path", ""))))
    missing_cochange.sort(key=lambda value: (value["for"], -float(value["weight"]), value["path"]))
    tests_to_run, tests_run_all, tests_run_all_reason, tests_by_changed_file, uncovered_changed_files = select_tests_with_coverage(
        diff.files,
        diff.raw_zero_context,
        graph_nodes,
        graph_edges,
        all_paths=all_paths,
        indexed_paths=indexed_paths,
        cochange=cochange,
        index_status=index_status,
    )
    directives = _directive_strings(
        missing_cochange, missing_tests, qt_pairs, qt_instantiations, qt_signal_sites, build_system_misses, tests_to_run
    )

    unindexed_files = sorted(path for path in changed_paths if path not in indexed_paths and not (next((f.previous_path for f in diff.files if f.path == path), None) in indexed_paths))
    status = "ok" if index_status == "available" and not unindexed_files else "partial"
    result: dict[str, Any] = {
        "status": status,
        "range": requested_range or "INDEX",
        "staged": staged,
        "repo_path": str(repo_root),
        "index_status": index_status,
        "cochange_threshold": COCHANGE_THRESHOLD,
        "score_formula": {
            "weights": RISK_WEIGHTS,
            "normalization": {
                "diff": f"min(1, churn/{DIFF_CHURN_SCALE})",
                "hotspot": f"min(1, stored_hotspot_score/{HOTSPOT_SCALE})",
                "fan_in": f"min(1, fan_in/{FAN_IN_SCALE})",
                "cochange": "min(1, strongest missing partner weight)",
                "directives": f"min(1, actionable_directives/{DIRECTIVE_SCALE})",
            },
            "score": "round(10 * sum(weight * component), 2)",
        },
        "files": file_payloads,
        "missing_cochange": missing_cochange,
        "missing_cochanges": missing_cochange,
        "missing_tests": sorted(missing_tests, key=lambda value: (value["source"], value["test"])),
        "missing_qt_pairs": qt_pairs,
        "missing_qt_instantiations": qt_instantiations,
        "missing_qt_sites": qt_signal_sites,
        "build_system_misses": build_system_misses,
        "directives": directives,
        "unindexed": bool(unindexed_files or index_status != "available"),
        "unindexed_files": unindexed_files,
        "analysis_errors": (["Cortex index is unavailable; graph/hotspot analysis was skipped."] if index_status != "available" else []),
        "tests_to_run": tests_to_run,
        "tests_run_all": tests_run_all,
        "tests_run_all_reason": tests_run_all_reason,
        "tests_by_changed_file": tests_by_changed_file,
        "uncovered_changed_files": uncovered_changed_files,
    }
    return result


# Secondary buckets, in the order truncation gives them up once `files` is empty.
# `tests_by_changed_file` goes last: it is the per-file explanation of `tests_to_run`.
_RISK_DROP_ORDER = (
    "score_formula",
    "missing_cochanges",
    "missing_cochange",
    "missing_tests",
    "missing_qt_pairs",
    "missing_qt_instantiations",
    "missing_qt_sites",
    "build_system_misses",
    "analysis_errors",
    "unindexed_files",
    "tests_by_changed_file",
)
_TESTS_TRUNCATED_REASON = "tests_to_run was truncated to fit the response budget"


def _risk_tokens(output: Mapping[str, Any]) -> int:
    return count_text_tokens(json.dumps(output, sort_keys=True))


def _longest_fitting_prefix(count: int, fits: Callable[[int], bool]) -> int:
    """Largest n in [0, count] with fits(n), assuming size only grows with n (0 if none fits)."""
    lo, hi, best = 0, count, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid == 0 or fits(mid):
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    return best


def _compact_tests_by_changed_file(output: dict[str, Any]) -> dict[str, list[int]] | None:
    """`tests_by_changed_file` with each test replaced by its index in `tests_to_run`,
    or None when some test is not in that list (then the full form cannot be encoded)."""
    position = {item.get("test"): index for index, item in enumerate(output.get("tests_to_run") or [])}
    compact: dict[str, list[int]] = {}
    for path, tests in (output.get("tests_by_changed_file") or {}).items():
        if any(test not in position for test in tests):
            return None
        compact[path] = sorted(position[test] for test in tests)
    return compact


def _pruned_compact_indices(compact: Mapping[str, list[int]], size: int) -> dict[str, list[int]]:
    """Compact indices that still point into a `tests_to_run` shortened to `size`."""
    pruned = {path: [i for i in indices if i < size] for path, indices in compact.items()}
    return {path: indices for path, indices in pruned.items() if indices}


def truncate_risk_result(result: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Deterministically keep high-risk files and directives within a budget.

    Give-up order as the budget shrinks: lower-ranked `files`, then the secondary
    buckets in `_RISK_DROP_ORDER` (`tests_by_changed_file` last, first re-encoded as
    indices into `tests_to_run`), then `directives`, then `tests_to_run` (which
    switches `tests_run_all` on). Every fully removed key is named in
    `omitted_fields`. Prefix lengths are found by binary search: size only grows
    with the prefix.
    """
    output = json.loads(json.dumps(result, sort_keys=True))
    limit = max(0, int(budget))
    output["budget"] = limit
    output["truncated"] = False
    omitted: list[str] = []

    def over() -> bool:
        return _risk_tokens(output) > limit

    def shorten(key: str, probe: Callable[[list[Any], int], None] | None = None) -> None:
        items = output.get(key)
        if not items:
            return

        def fits(count: int) -> bool:
            output[key] = items[:count]
            if probe is not None:
                probe(items, count)
            return not over()

        best = _longest_fitting_prefix(len(items), fits)
        output[key] = items[:best]
        if probe is not None:
            probe(items, best)
        if best < len(items):
            output["truncated"] = True

    def drop(key: str) -> None:
        output.pop(key, None)
        if key == "tests_by_changed_file":
            output.pop("tests_by_changed_file_encoding", None)
        omitted.append(key)
        output["omitted_fields"] = list(omitted)
        output["truncated"] = True

    # Always preserve error/status and the concise directive list: lower-ranked
    # file detail goes first.
    if over():
        shorten("files")
    for key in _RISK_DROP_ORDER:
        if not over():
            break
        if key not in output:
            continue
        if key == "tests_by_changed_file":
            compact = _compact_tests_by_changed_file(output)
            if compact is not None:
                full = output[key]
                output[key] = compact
                output["tests_by_changed_file_encoding"] = "tests_to_run_index"
                output["truncated"] = True
                if not over():
                    break
                output[key] = full
                output.pop("tests_by_changed_file_encoding", None)
        drop(key)
    if over():
        # Keep directives in stable order but drop from the end; the full
        # directive arrays remain available in an unbudgeted CLI analysis.
        shorten("directives")
    if over() and output.get("tests_to_run"):
        # The impacted-test list goes last, lowest score first.  A shortened
        # list can no longer be vouched for, so it switches to run-all;
        # ``tests_run_all``/``tests_run_all_reason`` themselves are never dropped.
        was_run_all = bool(output.get("tests_run_all"))
        original_reason = output.get("tests_run_all_reason")
        compact = (
            dict(output["tests_by_changed_file"])
            if output.get("tests_by_changed_file_encoding") == "tests_to_run_index"
            else None
        )

        def tests_probe(items: list[Any], count: int) -> None:
            if count < len(items) and not was_run_all:
                output["tests_run_all"] = True
                output["tests_run_all_reason"] = _TESTS_TRUNCATED_REASON
            else:
                output["tests_run_all"] = was_run_all
                if "tests_run_all_reason" in result:
                    output["tests_run_all_reason"] = original_reason
            if compact is not None:
                output["tests_by_changed_file"] = _pruned_compact_indices(compact, count)

        shorten("tests_to_run", tests_probe)
    output["returned_count"] = len(output.get("files", []))
    output["budget_feasible"] = not over()  # measured before the flag itself is added
    return output


# Friendly aliases for callers that prefer a verb matching the CLI command.
run_risk = analyze_risk
compute_risk = analyze_risk

__all__ = [
    "COCHANGE_THRESHOLD",
    "DiffFile",
    "RISK_WEIGHTS",
    "RiskAnalysisError",
    "analyze_risk",
    "compute_risk",
    "parse_name_status",
    "parse_numstat",
    "parse_zero_context_diff",
    "risk_score",
    "run_risk",
    "truncate_risk_result",
]
