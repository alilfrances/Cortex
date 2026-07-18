"""Repository-local QML module/build metadata extraction."""
from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

from ..models import GraphEdge, GraphNode


def _line(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _node(path: str, node_id: str, kind: str, label: str, line: int, metadata: dict[str, Any] | None = None) -> GraphNode:
    data = {"lineno": line, "qml_kind": kind}
    if metadata:
        data.update(metadata)
    return GraphNode(node_id=node_id, kind=kind if kind in {"module", "property", "func", "enum", "enum_member", "class"} else "module", label=label, source_ref=path, granularity="symbol", span_start=line, span_end=line, metadata=data)


def _edge(path: str, source: str, target: str, relation: str, line: int, **metadata: Any) -> GraphEdge:
    return GraphEdge(edge_id=f"qmlmeta:{path}:{relation}:{source}:{target}:{line}", source=source, target=target, relation=relation, layer="STRUCTURAL", confidence="EXTRACTED", weight=1.0, metadata={"lineno": line, "source_file": path, **metadata})


def parse_qmldir(path: str, content: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Parse Qt 5/6 qmldir declarations without executing plugins."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    module_uri = ""
    module_version = ""
    exports: list[tuple[str, str, str, str, dict[str, Any], int]] = []
    for line, raw in enumerate(content.splitlines(keepends=True), start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split()
        key = parts[0].lower()
        if key == "module" and len(parts) >= 2:
            module_uri = parts[1]
            module_version = ""
            continue
        if key in {"plugin", "classname", "optional", "prefer"}:
            # Plugin loading is intentionally metadata only.
            continue
        if key in {"depends", "import", "dependency"} and len(parts) >= 2:
            dep = parts[1]
            exports.append(("dependency", dep, "", "", {"dependency": True}, line))
            continue
        if key in {"typeinfo", "designersupported"}:
            if len(parts) >= 2:
                exports.append(("typeinfo", parts[1], "", "", {}, line))
            continue
        singleton = False
        internal = False
        if key == "singleton":
            singleton = True
            parts = parts[1:]
        elif key == "internal":
            internal = True
            parts = parts[1:]
        if len(parts) >= 2 and re.match(r"^[A-Za-z_][\w.]*$", parts[0]):
            type_name, version = parts[0], parts[1]
            file_name = parts[2] if len(parts) >= 3 else ""
            exports.append(("export", type_name, version, file_name, {"singleton": singleton, "internal": internal}, line))
    if not module_uri:
        # A directory URI is still useful for same-module resolution.
        module_uri = PurePosixPath(path).parent.as_posix().replace("/", ".")
    module_id = f"module:{module_uri}"
    nodes.append(_node(path, f"symbol:{path}:{module_uri}", "module", module_uri, 1, {"uri": module_uri, "version": module_version, "qml_kind": "module", "metadata_file": "qmldir"}))
    for kind, name, version, file_name, metadata, line in exports:
        if kind in {"dependency", "typeinfo"}:
            target = f"module:{name}"
            edges.append(_edge(path, module_id, target, "imports", line, uri=name, unverified=True, **metadata))
            continue
        export_id = f"export:{module_uri}:{name}:{version}"
        data = {"uri": module_uri, "version": version, "exported_name": name, "qml_kind": "export", **metadata}
        if name:
            nodes.append(_node(path, export_id, "class", name, line, data))
            edges.append(_edge(path, module_id, export_id, "exports", line, uri=module_uri, version=version))
            if file_name:
                normalized = posixpath.normpath(posixpath.join(posixpath.dirname(path), file_name))
                edges.append(_edge(path, export_id, f"file:{normalized}", "exports", line, uri=module_uri, version=version, exported_name=name, singleton=metadata.get("singleton", False), internal=metadata.get("internal", False)))
    return nodes, edges


def _qmltype_component_blocks(content: str):
    # qmltypes is declarative JSON-ish text. Balanced braces preserve multiline
    # components while avoiding a dependency on a JSON parser for comments and
    # unquoted Qt metadata values.
    for match in re.finditer(r"\bComponent\s*\{", content):
        start = match.start()
        depth = 0
        quote = ""
        for index in range(match.end(), len(content)):
            char = content[index]
            if quote:
                if char == quote and content[index - 1] != "\\":
                    quote = ""
                continue
            if char in "'\"":
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                if depth == 0:
                    yield start, content[start:index + 1]
                    break
                depth -= 1


def parse_qmltypes(path: str, content: str) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for start, block in _qmltype_component_blocks(content):
        line = _line(content, start)
        name_match = re.search(r"(?:name|exports)\s*:\s*['\"]?([A-Za-z_][\w.]*)", block)
        name = name_match.group(1).split("/")[-1] if name_match else "Component"
        comp_id = f"symbol:{path}:{name}"
        nodes.append(_node(path, comp_id, "class", name, line, {"qml_kind": "qmltypes_component", "type_name": name, "metadata_file": ".qmltypes"}))
        for prop in re.finditer(r"property\s*:\s*\{[^}]*?name\s*:\s*['\"]([^'\"]+).*?type\s*:\s*['\"]([^'\"]+)", block, re.DOTALL):
            prop_id = f"symbol:{path}:{name}.{prop.group(1)}"
            nodes.append(_node(path, prop_id, "property", prop.group(1), line, {"qml_kind": "property", "qml_owner": name, "type": prop.group(2), "qmltypes": True}))
            edges.append(_edge(path, comp_id, prop_id, "contains", line))
        for key, qml_kind in (("method", "method"), ("signal", "signal")):
            for match in re.finditer(rf"{key}\s*:\s*\{{[^}}]*?name\s*:\s*['\"]([^'\"]+)", block, re.DOTALL):
                member_id = f"symbol:{path}:{name}.{match.group(1)}"
                nodes.append(_node(path, member_id, "func", match.group(1), line, {"qml_kind": qml_kind, "qml_owner": name, "qmltypes": True, "qt": "signal" if qml_kind == "signal" else "method"}))
                edges.append(_edge(path, comp_id, member_id, "contains", line))
        for enum_match in re.finditer(r"enumeration\s*:\s*\{[^}]*?name\s*:\s*['\"]([^'\"]+)", block, re.DOTALL):
            enum_id = f"symbol:{path}:{name}.{enum_match.group(1)}"
            nodes.append(_node(path, enum_id, "enum", enum_match.group(1), line, {"qml_kind": "enum", "qml_owner": name, "qmltypes": True}))
            edges.append(_edge(path, comp_id, enum_id, "contains", line))
        for member in re.finditer(r"\b(Property|Method|Signal|Enumeration)\s*\{(?P<body>[^{}]*)\}", block, re.I):
            body = member.group("body")
            name_match = re.search(r"\bname\s*:\s*['\"]([^'\"]+)", body, re.I)
            if not name_match:
                continue
            member_name = name_match.group(1)
            kind_name = member.group(1).lower()
            member_kind = "property" if kind_name == "property" else "enum" if kind_name == "enumeration" else "func"
            member_id = f"symbol:{path}:{name}.{member_name}"
            if any(node.node_id == member_id for node in nodes):
                continue
            metadata = {"qml_kind": "enum" if kind_name == "enumeration" else ("signal" if kind_name == "signal" else kind_name), "qml_owner": name, "qmltypes": True}
            if kind_name == "signal":
                metadata["qt"] = "signal"
            type_match = re.search(r"\btype\s*:\s*['\"]([^'\"]+)", body, re.I)
            if type_match:
                metadata["type"] = type_match.group(1)
            nodes.append(_node(path, member_id, member_kind, member_name, line, metadata))
            edges.append(_edge(path, comp_id, member_id, "contains", line))
        for export in re.findall(r"exports\s*:\s*\[?\s*['\"]([^'\"]+)", block):
            edges.append(_edge(path, comp_id, f"module:{export}", "exports", line, exported_name=name, unverified=True))
    return nodes, edges


def _balanced_calls(content: str):
    for match in re.finditer(r"\b(qt_add_qml_module|qt6_add_qml_module|qt_target_qml_sources|qt6_target_qml_sources|qt_add_resources|target_sources)\s*\(", content, re.I):
        depth, quote, index = 1, "", match.end()
        while index < len(content) and depth:
            char = content[index]
            if quote:
                if char == quote and content[index - 1] != "\\": quote = ""
            elif char in "'\"": quote = char
            elif char == "(": depth += 1
            elif char == ")": depth -= 1
            index += 1
        yield match.group(1), match.start(), content[match.end():index - 1]


def parse_cmake_qml_metadata(path: str, content: str, known_paths: set[str] | None = None) -> tuple[list[GraphNode], list[GraphEdge]]:
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    known_paths = known_paths or set()
    for command, start, body in _balanced_calls(content):
        if command.lower() not in {"qt_add_qml_module", "qt6_add_qml_module", "qt_target_qml_sources", "qt6_target_qml_sources"}:
            continue
        line = _line(content, start)
        tokens = shlex.split(body, comments=True, posix=True)
        if not tokens:
            continue
        target = tokens[0]
        target_id = f"target:{target}"
        if not any(n.node_id == target_id for n in nodes):
            nodes.append(GraphNode(target_id, "target", target, path, "symbol", span_start=line, span_end=line, metadata={"lineno": line, "qml_kind": "cmake_target"}))
        upper = {value.upper(): index for index, value in enumerate(tokens)}
        uri = tokens[upper["URI"] + 1] if "URI" in upper and upper["URI"] + 1 < len(tokens) else ""
        version = tokens[upper["VERSION"] + 1] if "VERSION" in upper and upper["VERSION"] + 1 < len(tokens) else ""
        if uri:
            edges.append(_edge(path, target_id, f"module:{uri}", "exports", line, uri=uri, version=version, cmake_command=command))
        markers = ["QML_FILES", "SOURCES", "RESOURCES", "IMPORTS", "DEPENDENCIES"]
        for marker in markers:
            if marker not in upper:
                continue
            end = min((upper[m] for m in markers if m in upper and upper[m] > upper[marker]), default=len(tokens))
            for raw in tokens[upper[marker] + 1:end]:
                if raw.upper() in {"PRIVATE", "PUBLIC", "NO_RESOURCE_TARGET_PATH", "RESOURCE_PREFIX"} or raw.startswith("${"):
                    continue
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), raw))
                if marker in {"QML_FILES", "SOURCES", "RESOURCES"} and resolved in known_paths:
                    edges.append(_edge(path, target_id, f"file:{resolved}", "registers", line, category=marker.lower(), uri=uri, version=version))
                elif marker in {"IMPORTS", "DEPENDENCIES"}:
                    edges.append(_edge(path, target_id, f"module:{raw}", "imports", line, dependency=marker == "DEPENDENCIES", unverified=True))
    return nodes, edges


def extract_module_edges(path: str, content: str, known_paths: set[str] | None = None) -> tuple[list[GraphNode], list[GraphEdge]]:
    lower = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    if lower == "qmldir":
        return parse_qmldir(path, content)
    if suffix == ".qmltypes":
        return parse_qmltypes(path, content)
    if lower == "cmakelists.txt" or suffix == ".cmake":
        return parse_cmake_qml_metadata(path, content, known_paths)
    if suffix == ".qrc":
        known_paths = known_paths or set()
        edges: list[GraphEdge] = []
        prefix_match = re.search(r"<qresource\b[^>]*\bprefix\s*=\s*['\"]([^'\"]*)", content, re.I)
        prefix = prefix_match.group(1) if prefix_match else ""
        for match in re.finditer(r"<file(?P<attrs>[^>]*)>(?P<name>[^<]+)</file>", content, re.I):
            raw = match.group("name").strip()
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), raw))
            if resolved not in known_paths:
                continue
            alias_match = re.search(r"\balias\s*=\s*['\"]([^'\"]+)", match.group("attrs"), re.I)
            alias = alias_match.group(1) if alias_match else PurePosixPath(raw).name
            edges.append(_edge(path, f"file:{path}", f"file:{resolved}", "registers", _line(content, match.start()), prefix=prefix, alias=alias, resource_path=posixpath.join(prefix, alias)))
        return [], edges
    return [], []
