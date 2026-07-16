from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from ..models import GraphEdge, GraphNode


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    kind: str
    name_group: str = "name"
    # Extra symbol-node metadata to merge in for every match of this pattern.
    # Used to tag QML `signal` declarations `qt: signal` (P0-4) the same way
    # C++ signals/slots are tagged, so both feed the same cross-file
    # resolution index in graph.py.
    metadata: dict[str, str] | None = None


_C_SUFFIXES = (".c",)
_CPP_SUFFIXES = (".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")
_C_IMPORT_PATTERNS = [
    re.compile(r'^\s*#\s*include\s+[<"](?P<target>[^>"]+)[>"]', re.MULTILINE),
]
_C_DEF_PATTERNS = [
    _Pattern(re.compile(r"^\s*(?:struct|enum|union)\s+(?P<name>[A-Za-z_]\w*)\s*\{", re.MULTILINE), "class"),
    _Pattern(
        re.compile(
            r"^[ \t]*(?:[A-Za-z_]\w*[ \t*]+)+(?P<name>[A-Za-z_]\w*)[ \t]*\([^;{}\n]*\)[ \t]*\{",
            re.MULTILINE,
        ),
        "func",
    ),
]
_CPP_DEF_PATTERNS = [
    _Pattern(re.compile(r"^\s*namespace\s+(?P<name>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\{", re.MULTILINE), "class"),
    _Pattern(
        re.compile(r"^\s*(?:class|struct|enum(?:\s+class)?|union)\s+(?P<name>[A-Za-z_]\w*)[^{;]*\{", re.MULTILINE),
        "class",
    ),
    _Pattern(
        re.compile(
            r"^\s*(?:template\s*<[^>]+>\s*)?(?:[\w:<>,~*&\s]+\s+)(?:[A-Za-z_]\w*::)*(?P<name>~?[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{",
            re.MULTILINE,
        ),
        "func",
    ),
]
_QML_IMPORT_PATTERNS = [
    re.compile(r'^\s*import\s+(?P<target>[A-Za-z_][\w.]*|"[^"]+")', re.MULTILINE),
]
_QML_DEF_PATTERNS = [
    _Pattern(re.compile(r"^\s*function\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE), "func"),
    _Pattern(
        re.compile(r"^\s*signal\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE),
        "func",
        metadata={"qt": "signal"},
    ),
]
_QML_OBJECT_RE = re.compile(r"^\s*(?P<name>[A-Z][A-Za-z0-9_]*)\s*\{", re.MULTILINE)
_QT_SECTION_RE = re.compile(r"^\s*(?:(?:public|private|protected)\s+)?(?P<section>signals|slots|Q_SIGNALS|Q_SLOTS)\s*:?\s*$")
_QT_ACCESS_RE = re.compile(r"^\s*(?:public|private|protected|signals|slots|Q_SIGNALS|Q_SLOTS)\s*:")
_QT_CLASS_RE = re.compile(
    r"^\s*(?:class|struct)\s+(?P<name>[A-Za-z_]\w*)\b(?:\s*:\s*(?P<bases>[^{;]+))?\s*\{",
    re.MULTILINE,
)
_QT_MEMBER_RE = re.compile(
    r"^\s*(?:virtual\s+)?(?:[\w:<>,~*&\s]+\s+)?(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:=\s*0\s*)?;"
)
_QT_EMIT_RE = re.compile(r"^\s*(?:Q_EMIT|emit)\s+(?P<name>[A-Za-z_]\w*)\s*\(")
_QT_CONNECT_POINTER_RE = re.compile(
    r"connect\s*\(\s*(?P<sender>[^,]+),\s*&(?P<sender_class>[A-Za-z_]\w*)::(?P<signal>[A-Za-z_]\w*)\s*,\s*"
    r"(?P<receiver>[^,]+),\s*&(?P<receiver_class>[A-Za-z_]\w*)::(?P<slot>[A-Za-z_]\w*)"
)
_QT_CONNECT_MACRO_RE = re.compile(
    r"connect\s*\(\s*(?P<sender>[^,]+),\s*SIGNAL\s*\(\s*(?P<signal>[A-Za-z_]\w*)\s*\([^)]*\)\s*\)\s*,\s*"
    r"(?P<receiver>[^,]+),\s*SLOT\s*\(\s*(?P<slot>[A-Za-z_]\w*)\s*\([^)]*\)\s*\)"
)
_QML_HANDLER_RE = re.compile(r"^\s*(?P<name>on[A-Z][A-Za-z0-9_]*)\s*:")


_IMPORT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    ".js": [
        re.compile(r"^\s*import(?:\s+[\w*{}\s,]+\s+from)?\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
        re.compile(r"^\s*(?:const|let|var)\s+.*?require\(['\"](?P<target>[^'\"]+)['\"]\)", re.MULTILINE),
    ],
    ".jsx": [
        re.compile(r"^\s*import(?:\s+[\w*{}\s,]+\s+from)?\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
        re.compile(r"^\s*(?:const|let|var)\s+.*?require\(['\"](?P<target>[^'\"]+)['\"]\)", re.MULTILINE),
    ],
    ".ts": [
        re.compile(r"^\s*import(?:\s+type)?(?:\s+[\w*{}\s,]+\s+from)?\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
    ],
    ".tsx": [
        re.compile(r"^\s*import(?:\s+type)?(?:\s+[\w*{}\s,]+\s+from)?\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
    ],
    ".go": [re.compile(r"^\s*import\s+(?:\(\s*)?[`\"](?P<target>[^`\"]+)[`\"]", re.MULTILINE)],
    ".rs": [re.compile(r"^\s*use\s+(?P<target>[^;]+);", re.MULTILINE)],
    ".swift": [re.compile(r"^\s*import\s+(?P<target>[A-Za-z_][\w.]*)", re.MULTILINE)],
    ".java": [re.compile(r"^\s*import\s+(?:static\s+)?(?P<target>[\w.*]+);", re.MULTILINE)],
    ".rb": [
        re.compile(r"^\s*require(?:_relative)?\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
        re.compile(r"^\s*load\s+['\"](?P<target>[^'\"]+)['\"]", re.MULTILINE),
    ],
    **{suffix: _C_IMPORT_PATTERNS for suffix in _C_SUFFIXES},
    **{suffix: _C_IMPORT_PATTERNS for suffix in _CPP_SUFFIXES},
    ".qml": _QML_IMPORT_PATTERNS,
}

_DEF_PATTERNS: dict[str, list[_Pattern]] = {
    ".js": [
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
    ],
    ".jsx": [
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
    ],
    ".ts": [
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
    ],
    ".tsx": [
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
        _Pattern(re.compile(r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_$][\w$]*)", re.MULTILINE), "class"),
    ],
    ".go": [
        _Pattern(re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*type\s+(?P<name>[A-Za-z_]\w*)\s+(?:struct|interface)\b", re.MULTILINE), "class"),
    ],
    ".rs": [
        _Pattern(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(?P<name>[A-Za-z_]\w*)", re.MULTILINE), "class"),
    ],
    ".swift": [
        _Pattern(re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+)?func\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:public\s+|private\s+|internal\s+|open\s+)?(?:class|struct|enum|protocol)\s+(?P<name>[A-Za-z_]\w*)", re.MULTILINE), "class"),
    ],
    ".java": [
        _Pattern(re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)+[\w<>\[\], ?]+\s+(?P<name>[a-z_]\w*)\s*\(", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|abstract\s+|final\s+)?(?:class|interface|enum|record)\s+(?P<name>[A-Za-z_]\w*)", re.MULTILINE), "class"),
    ],
    ".rb": [
        _Pattern(re.compile(r"^\s*def\s+(?P<name>[A-Za-z_]\w*[!?=]?)", re.MULTILINE), "func"),
        _Pattern(re.compile(r"^\s*class\s+(?P<name>[A-Z]\w*(?:::[A-Z]\w*)*)", re.MULTILINE), "class"),
        _Pattern(re.compile(r"^\s*module\s+(?P<name>[A-Z]\w*(?:::[A-Z]\w*)*)", re.MULTILINE), "class"),
    ],
    **{suffix: _C_DEF_PATTERNS for suffix in _C_SUFFIXES},
    **{suffix: _CPP_DEF_PATTERNS for suffix in _CPP_SUFFIXES},
    ".qml": _QML_DEF_PATTERNS,
}


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _target_id(target: str) -> str:
    cleaned = target.strip().strip('"')
    return f"module:{cleaned or 'unknown'}"


def resolve_local_import(target: str, known_paths: set[str]) -> str | None:
    """Resolve an include/import target to a repo-relative path if it matches a known file.

    Tries an exact relative-path match first, then falls back to a unique basename
    match (so `#include "airpod.h"` resolves even when the include omits the
    subdirectory the file actually lives in).
    """
    candidate = target.strip().strip('"').lstrip("./")
    if not candidate:
        return None
    if candidate in known_paths:
        return candidate
    basename = PurePosixPath(candidate).name
    matches = [p for p in known_paths if PurePosixPath(p).name == basename]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_qml_component(name: str, known_paths: set[str]) -> str | None:
    return resolve_local_import(f"{name}.qml", known_paths)


def resolve_qml_cpp_type(name: str, known_paths: set[str]) -> str | None:
    """Return a local C/C++ declaration path for a QML object type.

    QML can instantiate a registered QObject type even though there is no
    ``Type.qml`` file to resolve.  The regex extractor only has the repository
    path set at per-file parse time, so use the conventional header/source
    basename as a conservative signal and let the graph-wide Qt resolution
    pass map the type to the real class node.  Unique-basename matching keeps
    this incremental-safe: an unchanged header is enough to resolve a newly
    parsed QML file, while framework/external types remain unmarked.
    """
    if not name or not name[0].isupper():
        return None
    for suffix in (".hpp", ".h", ".hh", ".hxx", ".cpp", ".cc", ".cxx"):
        resolved = resolve_local_import(f"{name}{suffix}", known_paths)
        if resolved is not None:
            return resolved
    # Also recognize the common snake_case filename spelling without making
    # a repo-wide class guess; the graph pass still requires a real class node
    # with the exact QML type name before it rewrites the endpoint.
    normalised_name = re.sub(r"[^a-z0-9]", "", name.lower())
    candidates = [
        path for path in known_paths
        if PurePosixPath(path).suffix.lower() in _CPP_SUFFIXES
        and re.sub(r"[^a-z0-9]", "", PurePosixPath(path).stem.lower()) == normalised_name
    ]
    return sorted(candidates)[0] if len(candidates) == 1 else None


def _signature(content: str, start: int) -> str:
    end = content.find("\n", start)
    if end == -1:
        end = len(content)
    return content[start:end].strip()


_CPP_RAW_PREFIX_RE = re.compile(r'(?:u8|u|U|L)?R"(?P<delimiter>[^\s()\\]{0,16})\(')


def _mask_comments_and_strings(content: str, *, hash_comments: bool = False) -> str:
    """Replace comments and literals with spaces while preserving newlines.

    The structural regex backend and the hotspot estimator both need a cheap
    line-preserving view of source. Keeping the scanner here avoids having two
    subtly different interpretations of braces/keywords. In addition to
    ordinary quoted strings and comments it understands Python triple-quoted
    literals and C++ raw strings such as ``R"TAG(... )TAG"``.
    """
    chars = list(content)
    n = len(content)
    i = 0

    def blank(start: int, end: int) -> None:
        for index in range(start, min(end, n)):
            if content[index] != "\n":
                chars[index] = " "

    while i < n:
        raw_match = _CPP_RAW_PREFIX_RE.match(content, i)
        if raw_match:
            delimiter = raw_match.group("delimiter")
            terminator = ")" + delimiter + '"'
            close = content.find(terminator, raw_match.end())
            end = n if close == -1 else close + len(terminator)
            blank(i, end)
            i = end
            continue

        if content.startswith("//", i):
            end = content.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
            continue
        if content.startswith("/*", i):
            close = content.find("*/", i + 2)
            end = n if close == -1 else close + 2
            blank(i, end)
            i = end
            continue
        if hash_comments and content[i] == "#":
            end = content.find("\n", i)
            end = n if end == -1 else end
            blank(i, end)
            i = end
            continue

        if content.startswith('"""', i) or content.startswith("'''", i):
            quote = content[i : i + 3]
            end = i + 3
            while end < n:
                if content[end] == "\\":
                    end += 2
                    continue
                if content.startswith(quote, end):
                    end += 3
                    break
                end += 1
            blank(i, end)
            i = end
            continue

        if content[i] in {'"', "'", "`"}:
            quote = content[i]
            end = i + 1
            while end < n:
                if content[end] == "\\":
                    end += 2
                    continue
                if content[end] == quote:
                    end += 1
                    break
                end += 1
            blank(i, end)
            i = end
            continue

        i += 1

    return "".join(chars)


def _matching_brace(content: str, open_idx: int) -> int | None:
    """Return the offset of the '}' that closes the '{' at ``open_idx``.

    Skips braces inside strings, char literals, and line/block comments so a
    body span is not cut short by a ``}`` that merely appears in text. Returns
    ``None`` when no balanced closing brace exists (unbalanced source).
    """
    masked = _mask_comments_and_strings(content[open_idx:])
    depth = 0
    for offset, ch in enumerate(masked):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx + offset
    return None


def _def_span_end(content: str, match_start: int, start_line: int) -> int:
    """Line of the closing brace for a definition, or ``start_line`` if none.

    Finds the body's opening brace (the first ``{`` after the declaration) and
    brace-matches to its close. A ``;`` before any ``{`` means the match is a
    forward declaration or prototype with no body, so the span stays one line.
    """
    open_idx = content.find("{", match_start)
    if open_idx == -1:
        return start_line
    semi_idx = content.find(";", match_start, open_idx)
    if semi_idx != -1:
        return start_line
    close_idx = _matching_brace(content, open_idx)
    if close_idx is None:
        return start_line
    return _line_number(content, close_idx)


def _line_count(content: str) -> int:
    return content.count("\n") + (0 if content.endswith("\n") and content else 1)


def _symbol_node(
    path: str,
    name: str,
    kind: str,
    signature: str,
    line: int,
    metadata: dict[str, str | int] | None = None,
    span_end: int | None = None,
) -> GraphNode:
    node_metadata: dict[str, str | int] = {"lineno": line}
    if metadata:
        node_metadata.update(metadata)
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind=kind,
        label=name.split(".")[-1].split("::")[-1],
        source_ref=path,
        granularity="symbol",
        signature=signature,
        span_start=line,
        span_end=span_end if span_end is not None else line,
        metadata=node_metadata,
    )


def _symbol_ref(path: str, name: str, nodes: list[GraphNode]) -> str:
    """Same-file resolution for an `emit`/`connect()` endpoint name.

    Only matches a node explicitly tagged `qt: signal`/`qt: slot` (i.e. one
    created via `_upsert_qt_symbol` from a `signals:`/`slots:` section) --
    *not* just any node that happens to share the name. A `connect()` call
    references a specific class's member (`&DeviceModel::onDeviceConnected`);
    matching an unrelated same-named plain method (e.g. a different class's
    own `onDeviceConnected` implementation living in the same .cpp) would
    silently produce a wrong-but-resolved edge instead of the correct
    cross-file one the P0-4 resolution pass in graph.py would otherwise find
    (via `sender_class`/`receiver_class` metadata -- see
    `_extract_qt_cpp_edges` below and `graph.py::_resolve_qt_edges`).
    """
    symbol_id = f"symbol:{path}:{name}"
    for node in nodes:
        if node.node_id == symbol_id and node.metadata.get("qt") in ("signal", "slot"):
            return symbol_id
    return _target_id(name)


def _cpp_base_names(base_clause: str) -> list[str]:
    bases: list[str] = []
    for raw_base in base_clause.split(","):
        tokens = [
            token
            for token in re.split(r"\s+", raw_base.strip())
            if token and token not in {"public", "private", "protected", "virtual"}
        ]
        if not tokens:
            continue
        base = tokens[0].strip()
        base = re.sub(r"<.*>$", "", base)
        if re.match(r"^[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*$", base):
            bases.append(base)
    return bases


def _extract_cpp_inheritance_edges(path: str, content: str, edges: list[GraphEdge]) -> None:
    for match in _QT_CLASS_RE.finditer(content):
        base_clause = match.group("bases")
        if not base_clause:
            continue
        class_name = match.group("name")
        class_id = f"symbol:{path}:{class_name}"
        line = _line_number(content, match.start())
        for base in _cpp_base_names(base_clause):
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:inherits:{class_name}:{base}",
                    source=class_id,
                    target=f"name:{base}",
                    relation="inherits",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )


def _upsert_qt_symbol(
    path: str,
    name: str,
    kind: str,
    signature: str,
    line: int,
    qt_kind: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen: set[str],
    file_node_id: str,
) -> None:
    symbol_id = f"symbol:{path}:{name}"
    for node in nodes:
        if node.node_id == symbol_id:
            node.metadata["qt"] = qt_kind
            return
    seen.add(name)
    node = _symbol_node(path, name, kind, signature, line, {"qt": qt_kind})
    nodes.append(node)
    edges.append(
        GraphEdge(
            edge_id=f"regex:{path}:contains:{name}",
            source=file_node_id,
            target=node.node_id,
            relation="contains",
            layer="STRUCTURAL",
            confidence="LOW",
            weight=1.0,
            metadata={"lineno": line, "source_file": path},
        )
    )


def _extract_qt_cpp_edges(
    path: str,
    content: str,
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen: set[str],
) -> None:
    current_class = ""
    class_depth = 0
    section = ""
    brace_depth = 0

    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        class_match = _QT_CLASS_RE.match(line)
        if class_match:
            current_class = class_match.group("name")
            class_depth = brace_depth + line.count("{") - line.count("}")
            section = ""
        if current_class and "Q_OBJECT" in line:
            symbol_id = f"symbol:{path}:{current_class}"
            for node in nodes:
                if node.node_id == symbol_id:
                    node.metadata["qt"] = "qobject"
                    break
        section_match = _QT_SECTION_RE.match(line)
        if section_match:
            section = "signal" if "SIGNAL" in section_match.group("section") or section_match.group("section") == "signals" else "slot"
        elif _QT_ACCESS_RE.match(line):
            section = ""
        elif section:
            member_match = _QT_MEMBER_RE.match(line)
            if member_match:
                _upsert_qt_symbol(
                    path,
                    member_match.group("name"),
                    "func",
                    stripped,
                    lineno,
                    section,
                    nodes,
                    edges,
                    seen,
                    file_node_id,
                )

        emit_match = _QT_EMIT_RE.match(line)
        if emit_match:
            name = emit_match.group("name")
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:emits:{lineno}:{name}",
                    source=file_node_id,
                    target=_symbol_ref(path, name, nodes),
                    relation="emits",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    # signal_name lets the P0-4 cross-file resolution pass
                    # (graph.py::_resolve_qt_edges) recognize a still-unresolved
                    # `module:<name>` target and try harder once the whole
                    # ingest batch (and, for incremental re-ingest, the store's
                    # already-known signals) is available.
                    metadata={"lineno": lineno, "source_file": path, "signal_name": name},
                )
            )

        for index, match in enumerate(_QT_CONNECT_POINTER_RE.finditer(line), start=1):
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:connects:{lineno}:{index}:{match.group('signal')}:{match.group('slot')}",
                    source=_symbol_ref(path, match.group("signal"), nodes),
                    target=_symbol_ref(path, match.group("slot"), nodes),
                    relation="connects",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={
                        "lineno": lineno,
                        "source_file": path,
                        "sender": match.group("sender").strip(),
                        "receiver": match.group("receiver").strip(),
                        "sender_class": match.group("sender_class"),
                        "receiver_class": match.group("receiver_class"),
                        "signal_name": match.group("signal"),
                        "slot_name": match.group("slot"),
                    },
                )
            )
        for index, match in enumerate(_QT_CONNECT_MACRO_RE.finditer(line), start=1):
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:connects:{lineno}:macro:{index}:{match.group('signal')}:{match.group('slot')}",
                    source=_symbol_ref(path, match.group("signal"), nodes),
                    target=_symbol_ref(path, match.group("slot"), nodes),
                    relation="connects",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={
                        "lineno": lineno,
                        "source_file": path,
                        "sender": match.group("sender").strip(),
                        "receiver": match.group("receiver").strip(),
                        "signal_name": match.group("signal"),
                        "slot_name": match.group("slot"),
                    },
                )
            )

        brace_depth += line.count("{") - line.count("}")
        if current_class and brace_depth < class_depth:
            current_class = ""
            section = ""


def _qml_handler_signal_name(handler_name: str) -> str:
    """`onDeviceConnected` -> `deviceConnected`, `onClicked` -> `clicked`."""
    return handler_name[2].lower() + handler_name[3:]


def _extract_qml_signal_symbols(
    path: str,
    content: str,
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen: set[str],
) -> None:
    """QML `signal foo(...)` declarations as `qt: signal`-tagged symbol nodes.

    The regex backend's generic `_DEF_PATTERNS` pass already does this (the
    `signal` pattern in `_QML_DEF_PATTERNS` carries `metadata={"qt": "signal"}`).
    This standalone entry point exists so the tree-sitter backend -- whose QML
    grammar node types aren't mapped to a "signal declaration" kind -- can
    still produce the same qt-tagged symbol via the always-available regex
    path, exactly like it already reuses `_extract_qt_cpp_edges` for C++
    signals/slots. Cross-file handler/emit resolution (graph.py) depends on
    these being tagged identically regardless of which backend ran.
    """
    pattern = next(p for p in _QML_DEF_PATTERNS if p.metadata and p.metadata.get("qt") == "signal")
    for match in pattern.regex.finditer(content):
        name = match.group(pattern.name_group)
        if name in seen:
            continue
        seen.add(name)
        name_start = match.start(pattern.name_group)
        line_start = content.rfind("\n", 0, name_start) + 1
        line = _line_number(content, line_start)
        # A QML `signal foo(...)` declaration is a single statement with no
        # `{...}` body of its own -- unlike `_def_span_end`'s brace-matching,
        # which (lacking a `;` terminator to stop at) would otherwise walk
        # forward to the next unrelated `{` in the file, e.g. a sibling
        # `MouseArea { ... }` block, and wrongly claim its lines as part of
        # the signal's span (P1-6 Qt-parity fix). No span_end passed here ->
        # _symbol_node defaults it to `line` (single-line span).
        node = _symbol_node(
            path, name, pattern.kind, _signature(content, line_start), line, metadata=pattern.metadata
        )
        nodes.append(node)
        edges.append(
            GraphEdge(
                edge_id=f"regex:{path}:contains:{name}",
                source=file_node_id,
                target=node.node_id,
                relation="contains",
                layer="STRUCTURAL",
                confidence="LOW",
                weight=1.0,
                metadata={"lineno": line, "source_file": path},
            )
        )


def _extract_qml_handlers(
    path: str,
    content: str,
    known_paths: set[str],
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
) -> None:
    """QML `onFoo:` handlers (P0-4).

    Each handler becomes a real symbol node (kind `func`, metadata
    `qt: handler`) in addition to its `handles` edge, so it's addressable by
    `cortex_read_symbol`/dead-code/context like any other symbol. The
    `handles` edge target is derived from the on-name (`onDeviceConnected` ->
    `deviceConnected`); when the handler sits inside a block that instantiates
    a *locally known* QML component (tracked via a nesting stack, since a
    handler can sit several objects deep -- e.g. a `MouseArea` inside the
    instantiated component), the edge is tagged with that component's file
    path (`component_path`) so the cross-file resolution pass in graph.py can
    point it at the real signal symbol once that file's signals are known
    (same batch, or an earlier one via the store -- see QtSymbolIndex). An
    enclosing type that isn't a locally known component (a Qt Quick framework
    item such as MouseArea, or no enclosing object at all) keeps the
    `module:` placeholder rather than guessing.
    """
    stack: list[tuple[str, int]] = []
    brace_depth = 0
    seen_qualnames: set[str] = set()

    for lineno, line in enumerate(content.splitlines(), start=1):
        obj_match = _QML_OBJECT_RE.match(line)
        if obj_match:
            open_depth = brace_depth + line.count("{") - line.count("}")
            stack.append((obj_match.group("name"), open_depth))

        handler_match = _QML_HANDLER_RE.match(line)
        if handler_match:
            handler_name = handler_match.group("name")
            enclosing = stack[-1][0] if stack else None
            component_path = resolve_qml_component(enclosing, known_paths) if enclosing else None
            signal_name = _qml_handler_signal_name(handler_name)

            qualname = f"{enclosing}.{handler_name}" if enclosing else handler_name
            if qualname in seen_qualnames:
                qualname = f"{qualname}:{lineno}"
            seen_qualnames.add(qualname)

            handler_node = _symbol_node(
                path,
                qualname,
                "func",
                line.strip(),
                lineno,
                metadata={"qt": "handler", "handler_name": handler_name, "signal_name": signal_name},
            )
            nodes.append(handler_node)
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:contains:{qualname}",
                    source=file_node_id,
                    target=handler_node.node_id,
                    relation="contains",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": lineno, "source_file": path},
                )
            )
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:handles:{lineno}:{handler_name}",
                    source=file_node_id,
                    target=_target_id(signal_name),
                    relation="handles",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={
                        "lineno": lineno,
                        "source_file": path,
                        "handler_name": handler_name,
                        "signal_name": signal_name,
                        "component_path": component_path or "",
                    },
                )
            )

        brace_depth += line.count("{") - line.count("}")
        while stack and brace_depth < stack[-1][1]:
            stack.pop()


def _extract_qml_component_definition(
    path: str,
    content: str,
    file_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    seen: set[str],
) -> None:
    stem = PurePosixPath(path).stem
    if not stem or not stem[0].isupper():
        return

    root_match = _QML_OBJECT_RE.search(content)
    if root_match:
        name_start = root_match.start("name")
        line_start = content.rfind("\n", 0, name_start) + 1
        line = _line_number(content, line_start)
        signature = _signature(content, line_start)
        span_end = _def_span_end(content, root_match.end("name"), line)
    else:
        line = 1
        signature = _signature(content, 0)
        span_end = _line_count(content)

    seen.add(stem)
    node = _symbol_node(path, stem, "class", signature, line, span_end=span_end)
    nodes.append(node)
    edges.append(
        GraphEdge(
            edge_id=f"regex:{path}:contains:{stem}",
            source=file_node_id,
            target=node.node_id,
            relation="contains",
            layer="STRUCTURAL",
            confidence="LOW",
            weight=1.0,
            metadata={"lineno": line, "source_file": path},
        )
    )


def _extract_qml_instantiates(path: str, content: str, known_paths: set[str], file_node_id: str, edges: list[GraphEdge]) -> None:
    for index, match in enumerate(_QML_OBJECT_RE.finditer(content), start=1):
        name = match.group("name")
        qml_path = resolve_qml_component(name, known_paths)
        if qml_path is not None:
            if qml_path == path:
                continue
            line = _line_number(content, match.start("name"))
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:instantiates:{line}:{index}:{name}",
                    source=file_node_id,
                    target=f"file:{qml_path}",
                    relation="instantiates",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path, "type_name": name, "component_kind": "qml"},
                )
            )
            continue

        # A local QML file is not the only thing a scene can instantiate:
        # registered QObject types are commonly declared in a sibling C++
        # header.  Emit a placeholder only when a matching local C++
        # declaration path exists; graph.py resolves it to the real class
        # symbol after all files (or the stored incremental Qt index) are
        # available.  External Qt Quick controls therefore stay out of the
        # graph instead of becoming fabricated symbols.
        cpp_path = resolve_qml_cpp_type(name, known_paths)
        if cpp_path is None:
            continue
        line = _line_number(content, match.start("name"))
        edges.append(
            GraphEdge(
                edge_id=f"regex:{path}:instantiates:{line}:{index}:{name}",
                source=file_node_id,
                target=f"module:{name}",
                relation="instantiates",
                layer="STRUCTURAL",
                confidence="LOW",
                weight=1.0,
                metadata={
                    "lineno": line,
                    "source_file": path,
                    "type_name": name,
                    "component_path": cpp_path,
                    "component_kind": "cpp",
                },
            )
        )


def extract_regex_edges(
    path: str,
    content: str,
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    suffix = PurePosixPath(path).suffix.lower()
    file_node_id = f"file:{path}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for pattern in _IMPORT_PATTERNS.get(suffix, []):
        for index, match in enumerate(pattern.finditer(content), start=1):
            line = _line_number(content, match.start())
            target = match.group("target")
            resolved = resolve_local_import(target, known_paths)
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:import:{line}:{index}:{target}",
                    source=file_node_id,
                    target=f"file:{resolved}" if resolved else _target_id(target),
                    relation="imports",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )

    seen: set[str] = set()
    if suffix == ".qml":
        _extract_qml_component_definition(path, content, file_node_id, nodes, edges, seen)

    for pattern in _DEF_PATTERNS.get(suffix, []):
        for match in pattern.regex.finditer(content):
            name = match.group(pattern.name_group)
            if name in seen:
                continue
            seen.add(name)
            # Anchor on the name, not match.start(): leading `^\s*` and greedy
            # return-type groups can pull the match onto a blank line or a
            # preceding line (e.g. the Q_OBJECT line above a method), which
            # would otherwise skew the line number, signature, and span.
            name_start = match.start(pattern.name_group)
            line_start = content.rfind("\n", 0, name_start) + 1
            line = _line_number(content, line_start)
            is_qml_signal = suffix == ".qml" and pattern.metadata and pattern.metadata.get("qt") == "signal"
            # QML `signal foo(...)` has no `{...}` body -- see the matching
            # comment in _extract_qml_signal_symbols for why _def_span_end
            # must not run for it (P1-6 Qt-parity fix).
            span_end = line if is_qml_signal else _def_span_end(content, match.end(pattern.name_group), line)
            node = _symbol_node(
                path, name, pattern.kind, _signature(content, line_start), line, metadata=pattern.metadata, span_end=span_end
            )
            nodes.append(node)
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:contains:{name}",
                    source=file_node_id,
                    target=node.node_id,
                    relation="contains",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )

    if suffix in _CPP_SUFFIXES:
        _extract_cpp_inheritance_edges(path, content, edges)
        _extract_qt_cpp_edges(path, content, file_node_id, nodes, edges, seen)
    if suffix == ".qml":
        _extract_qml_instantiates(path, content, known_paths, file_node_id, edges)
        _extract_qml_handlers(path, content, known_paths, file_node_id, nodes, edges)

    return nodes, edges
