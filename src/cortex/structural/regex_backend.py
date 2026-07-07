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


_C_SUFFIXES = (".c",)
_CPP_SUFFIXES = (".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")
_C_IMPORT_PATTERNS = [
    re.compile(r'^\s*#\s*include\s+[<"](?P<target>[^>"]+)[>"]', re.MULTILINE),
]
_C_DEF_PATTERNS = [
    _Pattern(re.compile(r"^\s*(?:struct|enum|union)\s+(?P<name>[A-Za-z_]\w*)\s*\{", re.MULTILINE), "class"),
    _Pattern(
        re.compile(
            r"^\s*(?:[A-Za-z_][\w\s*]+\s+)+(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{",
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
    _Pattern(re.compile(r"^\s*signal\s+(?P<name>[A-Za-z_]\w*)\s*\(", re.MULTILINE), "func"),
    _Pattern(re.compile(r"^\s*(?P<name>[A-Z][A-Za-z0-9_]*)\s*\{", re.MULTILINE), "class"),
]
_QT_SECTION_RE = re.compile(r"^\s*(?:(?:public|private|protected)\s+)?(?P<section>signals|slots|Q_SIGNALS|Q_SLOTS)\s*:?\s*$")
_QT_ACCESS_RE = re.compile(r"^\s*(?:public|private|protected|signals|slots|Q_SIGNALS|Q_SLOTS)\s*:")
_QT_CLASS_RE = re.compile(r"^\s*(?:class|struct)\s+(?P<name>[A-Za-z_]\w*)\b[^{;]*\{")
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


def _signature(content: str, start: int) -> str:
    line = content[start:content.find("\n", start)]
    if not line:
        line = content[start:]
    return line.strip()


def _symbol_node(
    path: str,
    name: str,
    kind: str,
    signature: str,
    line: int,
    metadata: dict[str, str | int] | None = None,
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
        span_end=line,
        metadata=node_metadata,
    )


def _symbol_ref(path: str, name: str, symbol_ids: set[str]) -> str:
    symbol_id = f"symbol:{path}:{name}"
    return symbol_id if symbol_id in symbol_ids else _target_id(name)


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
            symbol_ids = {node.node_id for node in nodes}
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:emits:{lineno}:{name}",
                    source=file_node_id,
                    target=_symbol_ref(path, name, symbol_ids),
                    relation="emits",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": lineno, "source_file": path},
                )
            )

        for index, match in enumerate(_QT_CONNECT_POINTER_RE.finditer(line), start=1):
            symbol_ids = {node.node_id for node in nodes}
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:connects:{lineno}:{index}:{match.group('signal')}:{match.group('slot')}",
                    source=_symbol_ref(path, match.group("signal"), symbol_ids),
                    target=_symbol_ref(path, match.group("slot"), symbol_ids),
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
                    },
                )
            )
        for index, match in enumerate(_QT_CONNECT_MACRO_RE.finditer(line), start=1):
            symbol_ids = {node.node_id for node in nodes}
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:connects:{lineno}:macro:{index}:{match.group('signal')}:{match.group('slot')}",
                    source=_symbol_ref(path, match.group("signal"), symbol_ids),
                    target=_symbol_ref(path, match.group("slot"), symbol_ids),
                    relation="connects",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={
                        "lineno": lineno,
                        "source_file": path,
                        "sender": match.group("sender").strip(),
                        "receiver": match.group("receiver").strip(),
                    },
                )
            )

        brace_depth += line.count("{") - line.count("}")
        if current_class and brace_depth < class_depth:
            current_class = ""
            section = ""


def _extract_qml_handlers(path: str, content: str, file_node_id: str, nodes: list[GraphNode], edges: list[GraphEdge]) -> None:
    symbol_ids = {node.node_id for node in nodes}
    for lineno, line in enumerate(content.splitlines(), start=1):
        match = _QML_HANDLER_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        edges.append(
            GraphEdge(
                edge_id=f"regex:{path}:handles:{lineno}:{name}",
                source=file_node_id,
                target=_symbol_ref(path, name, symbol_ids),
                relation="handles",
                layer="STRUCTURAL",
                confidence="LOW",
                weight=1.0,
                metadata={"lineno": lineno, "source_file": path},
            )
        )


def extract_regex_edges(
    path: str,
    content: str,
    known_paths: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    del known_paths
    suffix = PurePosixPath(path).suffix.lower()
    file_node_id = f"file:{path}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    for pattern in _IMPORT_PATTERNS.get(suffix, []):
        for index, match in enumerate(pattern.finditer(content), start=1):
            line = _line_number(content, match.start())
            target = match.group("target")
            edges.append(
                GraphEdge(
                    edge_id=f"regex:{path}:import:{line}:{index}:{target}",
                    source=file_node_id,
                    target=_target_id(target),
                    relation="imports",
                    layer="STRUCTURAL",
                    confidence="LOW",
                    weight=1.0,
                    metadata={"lineno": line, "source_file": path},
                )
            )

    seen: set[str] = set()
    for pattern in _DEF_PATTERNS.get(suffix, []):
        for match in pattern.regex.finditer(content):
            name = match.group(pattern.name_group)
            if name in seen:
                continue
            seen.add(name)
            line = _line_number(content, match.start())
            node = _symbol_node(path, name, pattern.kind, _signature(content, match.start()), line)
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
        _extract_qt_cpp_edges(path, content, file_node_id, nodes, edges, seen)
    if suffix == ".qml":
        _extract_qml_handlers(path, content, file_node_id, nodes, edges)

    return nodes, edges
