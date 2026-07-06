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
}


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _target_id(target: str) -> str:
    cleaned = target.strip()
    return f"module:{cleaned or 'unknown'}"


def _signature(content: str, start: int) -> str:
    line = content[start:content.find("\n", start)]
    if not line:
        line = content[start:]
    return line.strip()


def _symbol_node(path: str, name: str, kind: str, signature: str, line: int) -> GraphNode:
    return GraphNode(
        node_id=f"symbol:{path}:{name}",
        kind=kind,
        label=name.split(".")[-1].split("::")[-1],
        source_ref=path,
        granularity="symbol",
        signature=signature,
        span_start=line,
        span_end=line,
        metadata={"lineno": line},
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

    return nodes, edges
