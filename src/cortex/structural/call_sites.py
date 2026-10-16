"""Tree-sitter call-site extraction (the `calls` edge contract, see callresolve.py).

One walker per language family.  Each walker only *describes* a call site --
callee, receiver text/kind, scope, statically declared receiver type, enclosing
class -- and never resolves it: resolution is the repo-wide pass in
``callresolve``.  Emitting calls never changes the symbol nodes or the
non-call edges produced by ``treesitter_backend`` (additive-only).

Declared types are flow-insensitive per function: parameters, then locals in
source order, then members of the enclosing class when the class body is in the
same file.  A receiver that is not typed locally but may be a member of the
enclosing class (an out-of-line ``Foo::bar()`` in a ``.cpp`` using a member
declared in ``Foo.hpp``) carries ``receiver_member``; the class node of the
declaring file records ``member_types`` (see ``cpp_member_types``) and the
resolver joins the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import GraphEdge

_MAX_RECEIVER = 80


@dataclass(slots=True)
class CallContext:
    path: str
    source: bytes
    file_node_id: str
    # (start_byte, end_byte) of a definition node -> symbol id it was recorded under
    def_symbols: dict[tuple[int, int], str]
    # (start_byte, end_byte) of a definition node -> class qualifier (`Foo` of `Foo::bar`)
    def_qualifiers: dict[tuple[int, int], str]
    name_of: Callable[[Any, bytes], str]
    member_of: Callable[[Any, bytes], tuple[str | None, str]]
    known_paths: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Frame:
    symbol_id: str
    owner_label: str
    class_name: str | None
    vars: dict[str, str]
    in_function: bool = False
    # Inside a tree-sitter ERROR / bitfield_clause outside any function: the
    # classic `Q_OBJECT` + `public:` mis-parse turns the next declaration into a
    # bogus call expression, so nothing there is a call site.
    suppressed: bool = False


def _text(node: Any, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _squash(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_RECEIVER else text[:_MAX_RECEIVER]


class CallWalker:
    """Iterative pre-order walk carrying a scope `Frame`; subclasses fill the hooks."""

    language = ""

    def __init__(self, ctx: CallContext) -> None:
        self.ctx = ctx
        self.edges: list[GraphEdge] = []
        self._ids: set[str] = set()

    # -- hooks ------------------------------------------------------------
    def enter(self, node: Any, frame: Frame) -> Frame:
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        raise NotImplementedError

    # -- driver -----------------------------------------------------------
    def run(self, root: Any) -> list[GraphEdge]:
        stack: list[tuple[Any, Frame]] = [(root, Frame(self.ctx.file_node_id, "<file>", None, {}))]
        while stack:
            node, frame = stack.pop()
            frame = self.enter(node, frame)
            self.visit(node, frame)
            for child in reversed(node.children):
                stack.append((child, frame))
        return self.edges

    def text(self, node: Any) -> str:
        return _text(node, self.ctx.source)

    def emit(
        self,
        node: Any,
        frame: Frame,
        callee: str,
        *,
        receiver: str | None = None,
        kind: str = "none",
        scope: str | None = None,
        receiver_type: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not callee:
            return
        lineno = node.start_point[0] + 1
        receiver = _squash(receiver) if receiver else None
        site = f"{receiver}.{callee}" if receiver else callee
        edge_id = f"treesitter:{self.ctx.path}:calls:{frame.owner_label}:{site}:{lineno}"
        if edge_id in self._ids:
            return
        self._ids.add(edge_id)
        metadata: dict[str, Any] = {
            "lineno": lineno,
            "source_file": self.ctx.path,
            "callee": callee,
            "receiver": receiver,
            "receiver_kind": kind,
            "scope": scope,
            "receiver_type": receiver_type,
            "enclosing_class": frame.class_name,
        }
        if extra:
            metadata.update(extra)
        self.edges.append(
            GraphEdge(
                edge_id=edge_id,
                source=frame.symbol_id,
                target=f"name:{callee}",
                relation="calls",
                layer="STRUCTURAL",
                confidence="EXTRACTED",
                weight=1.0,
                metadata=metadata,
            )
        )

    def definition_frame(self, node: Any, frame: Frame, *, class_name: str | None, label: str) -> Frame:
        key = (node.start_byte, node.end_byte)
        symbol_id = self.ctx.def_symbols.get(key)
        if symbol_id is None:
            symbol_id = self.ctx.file_node_id
            owner = "<file>"
        else:
            owner = symbol_id.split(":", 2)[-1]
        return Frame(symbol_id, owner or label, class_name, dict(frame.vars), True)


# --------------------------------------------------------------------------- #
# C / C++
# --------------------------------------------------------------------------- #

_C_KEYWORDS = frozenset({
    "if", "else", "for", "while", "do", "switch", "case", "return", "sizeof", "alignof", "alignas",
    "decltype", "typeid", "typeof", "noexcept", "throw", "catch", "try", "defined", "static_assert",
    "_Static_assert", "__attribute__", "__declspec", "offsetof", "new", "delete", "operator",
    "co_await", "co_yield", "co_return", "requires", "asm", "__asm__",
})
_QT_NOT_CALLS = frozenset({"SIGNAL", "SLOT", "connect", "emit", "disconnect_never_a_call"})
_SMART_POINTERS = frozenset({
    "shared_ptr", "unique_ptr", "weak_ptr", "QPointer", "QScopedPointer", "QSharedPointer",
    "QWeakPointer", "scoped_ptr", "intrusive_ptr", "ComPtr", "optional", "Ref", "RefPtr",
})
_FACTORIES = frozenset({"create", "make", "instance", "newInstance", "make_shared", "make_unique"})
_CPP_TYPE_ID = {"type_identifier", "namespace_identifier"}
_CPP_DECLARATOR_NAMES = {"identifier", "field_identifier"}
_CPP_DEF_NODES = {"function_definition"}
_CPP_CLASS_NODES = {"class_specifier", "struct_specifier"}


class CppCalls(CallWalker):
    language = "cpp"

    # -- skip rules ---------------------------------------------------------
    @staticmethod
    def skip(callee: str) -> bool:
        return (
            callee in _C_KEYWORDS
            or callee in _QT_NOT_CALLS
            or callee.endswith("_cast")
            or re.fullmatch(r"Q_[A-Z_]+", callee) is not None
        )

    # -- types --------------------------------------------------------------
    def type_name(self, node: Any | None) -> str | None:
        """Class name a declared type refers to (pointers/refs/smart pointers stripped)."""
        if node is None:
            return None
        kind = node.type
        if kind == "type_identifier":
            return self.text(node)
        if kind in {"qualified_identifier", "scoped_type_identifier"}:
            inner = node.child_by_field_name("name")
            return self.type_name(inner)
        if kind == "template_type":
            name = node.child_by_field_name("name")
            label = self.type_name(name)
            if label in _SMART_POINTERS:
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for child in args.named_children:
                        if child.type == "type_descriptor":
                            return self.type_name(child.child_by_field_name("type"))
                        return self.type_name(child)
                return None
            return label
        if kind in {"struct_specifier", "class_specifier", "union_specifier", "enum_specifier"}:
            return self.type_name(node.child_by_field_name("name"))
        if kind == "type_descriptor":
            return self.type_name(node.child_by_field_name("type"))
        return None

    def declarator_name(self, node: Any | None) -> str | None:
        while node is not None:
            if node.type in _CPP_DECLARATOR_NAMES:
                return self.text(node)
            if node.type == "init_declarator":
                node = node.child_by_field_name("declarator")
                continue
            if node.type in {
                "pointer_declarator", "reference_declarator", "array_declarator",
                "parenthesized_declarator", "function_declarator", "attributed_declarator",
            }:
                inner = node.child_by_field_name("declarator")
                if inner is None:
                    inner = next((c for c in node.named_children if c.type != "type_qualifier"), None)
                node = inner
                continue
            return None
        return None

    def constructed_type(self, value: Any | None) -> str | None:
        """Type of `Type(...)`, `new Type(...)`, `make_shared<Type>(...)`, `Type::create(...)`."""
        if value is None:
            return None
        if value.type == "new_expression":
            return self.type_name(value.child_by_field_name("type"))
        if value.type == "parenthesized_expression":
            inner = next(iter(value.named_children), None)
            return self.constructed_type(inner)
        if value.type != "call_expression":
            return None
        fn = value.child_by_field_name("function")
        if fn is None:
            return None
        if fn.type == "identifier":
            return self.text(fn)
        if fn.type == "template_function":
            name = self.text(fn.child_by_field_name("name")) if fn.child_by_field_name("name") is not None else ""
            if name in {"make_shared", "make_unique", "make_optional"}:
                args = fn.child_by_field_name("arguments")
                first = next(iter(args.named_children), None) if args is not None else None
                return self.type_name(first)
            return name or None
        if fn.type == "qualified_identifier":
            name = fn.child_by_field_name("name")
            scope = fn.child_by_field_name("scope")
            if name is not None and scope is not None:
                if self.text(name) in _FACTORIES:
                    return self.text(scope).split("::")[-1]
                if name.type == "template_function":
                    inner = name.child_by_field_name("name")
                    if inner is not None and self.text(inner) in {"make_shared", "make_unique"}:
                        args = name.child_by_field_name("arguments")
                        first = next(iter(args.named_children), None) if args is not None else None
                        return self.type_name(first)
        return None

    def add_declaration(self, node: Any, frame: Frame) -> None:
        type_node = node.child_by_field_name("type")
        if type_node is not None and self.text(type_node) in {"emit", "Q_EMIT"}:
            return  # `emit sig(args);` parses as a declaration; it is an `emits` edge
        declared = self.type_name(type_node)
        if declared is not None and re.fullmatch(r"Q_[A-Z_]+|[A-Z][A-Z0-9_]{2,}", declared):
            return  # `Q_OBJECT`/`Q_INVOKABLE` swallowed into a declaration by the parser
        auto = type_node is not None and type_node.type == "placeholder_type_specifier"
        for child in node.children_by_field_name("declarator"):
            name = self.declarator_name(child)
            if not name or re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", name):
                continue  # macro-looking names (QML_NAMED_ELEMENT, Q_PROPERTY...) are not variables
            resolved = declared
            if (auto or declared is None) and child.type == "init_declarator":
                resolved = self.constructed_type(child.child_by_field_name("value")) or resolved
            if resolved:
                frame.vars[name] = resolved
            else:
                frame.vars.pop(name, None)

    def class_fields(self, body: Any, into: dict[str, str]) -> None:
        for child in body.named_children:
            if child.type == "field_declaration":
                self.add_declaration(child, Frame("", "", None, into))

    def add_parameters(self, declarator: Any | None, frame: Frame) -> None:
        while declarator is not None and declarator.type != "function_declarator":
            declarator = declarator.child_by_field_name("declarator")
        if declarator is None:
            return
        params = declarator.child_by_field_name("parameters")
        if params is None:
            return
        for param in params.named_children:
            if param.type == "parameter_declaration":
                self.add_declaration(param, frame)

    # -- scope --------------------------------------------------------------
    def enter(self, node: Any, frame: Frame) -> Frame:
        kind = node.type
        if kind in {"ERROR", "bitfield_clause"} and not frame.in_function and not frame.suppressed:
            return Frame(frame.symbol_id, frame.owner_label, frame.class_name, frame.vars, False, True)
        if kind in _CPP_CLASS_NODES:
            body = node.child_by_field_name("body")
            if body is None:
                return frame
            name = self.ctx.name_of(node, self.ctx.source) or None
            members: dict[str, str] = {}
            self.class_fields(body, members)
            return Frame(frame.symbol_id, frame.owner_label, name, members, False)
        if kind == "function_definition":
            key = (node.start_byte, node.end_byte)
            qualifier = self.ctx.def_qualifiers.get(key)
            if qualifier is None:
                qualifier, _ = self.ctx.member_of(node, self.ctx.source)
            class_name = qualifier or frame.class_name
            inner = self.definition_frame(node, frame, class_name=class_name, label="")
            self.add_parameters(node.child_by_field_name("declarator"), inner)
            return inner
        return frame

    # -- sites --------------------------------------------------------------
    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if frame.suppressed:
            return
        if kind == "declaration" and frame.in_function:
            self.add_declaration(node, frame)
            return
        if kind == "for_range_loop" and frame.in_function:
            self.add_declaration(node, frame)
            return
        if kind == "new_expression":
            name = self.type_name(node.child_by_field_name("type"))
            if name:
                self.emit(node, frame, name, kind="new")
            return
        if kind != "call_expression":
            return
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        self.call_site(node, fn, frame)

    def call_site(self, node: Any, fn: Any, frame: Frame) -> None:
        kind = fn.type
        if kind == "template_function":
            inner = fn.child_by_field_name("name")
            if inner is None:
                return
            fn, kind = inner, inner.type
        if kind == "identifier":
            callee = self.text(fn)
            if not self.skip(callee):
                self.emit(node, frame, callee)
            return
        if kind == "qualified_identifier":
            segments: list[str] = []
            cursor: Any = fn
            callee = ""
            while cursor is not None and cursor.type == "qualified_identifier":
                scope = cursor.child_by_field_name("scope")
                if scope is not None:
                    segments.append(self.text(scope).strip(":"))
                cursor = cursor.child_by_field_name("name")
            if cursor is not None:
                if cursor.type == "template_function":
                    cursor = cursor.child_by_field_name("name")
                callee = self.text(cursor) if cursor is not None else ""
            if not callee or callee.startswith("~") or self.skip(callee):
                return
            segments = [s for s in segments if s]
            if not segments:
                self.emit(node, frame, callee)  # ::global()
                return
            scope_label = segments[-1].split("<", 1)[0].split("::")[-1]
            self.emit(node, frame, callee, receiver="::".join(segments), kind="scoped", scope=scope_label)
            return
        if kind == "field_expression":
            self.member_call(node, fn, frame)

    def member_call(self, node: Any, fn: Any, frame: Frame) -> None:
        field_node = fn.child_by_field_name("field")
        arg = fn.child_by_field_name("argument")
        if field_node is None or arg is None:
            return
        if field_node.type == "template_method":
            inner = field_node.child_by_field_name("name")
            field_node = inner if inner is not None else field_node
        callee = self.text(field_node)
        if not callee or self.skip(callee) or callee.startswith("~"):
            return
        receiver = self.text(arg)
        akind = arg.type
        if akind == "this" or (akind == "pointer_expression" and receiver.replace(" ", "") == "*this"):
            self.emit(node, frame, callee, receiver="this", kind="this")
            return
        if akind == "parenthesized_expression":
            inner = next(iter(arg.named_children), None)
            if inner is not None and self.text(inner).replace(" ", "") == "*this":
                self.emit(node, frame, callee, receiver="this", kind="this")
                return
        if akind == "identifier":
            known = frame.vars.get(receiver)
            self.emit(
                node, frame, callee, receiver=receiver, kind="var", receiver_type=known,
                extra=None if known else {"receiver_member": receiver},
            )
            return
        if akind == "field_expression":
            base = arg.child_by_field_name("argument")
            member = arg.child_by_field_name("field")
            if base is not None and member is not None and base.type == "this":
                known = frame.vars.get(self.text(member))
                self.emit(
                    node, frame, callee, receiver=receiver, kind="var", receiver_type=known,
                    extra=None if known else {"receiver_member": self.text(member)},
                )
                return
        self.emit(node, frame, callee, receiver=receiver, kind="chain")


def cpp_member_types(node: Any, source: bytes) -> dict[str, str]:
    """`{member: type}` for the typed fields of a C/C++ class/struct body (sorted)."""
    body = node.child_by_field_name("body")
    if body is None:
        return {}
    walker = CppCalls(CallContext("", source, "", {}, {}, lambda n, s: "", lambda n, s: (None, "")))
    members: dict[str, str] = {}
    walker.class_fields(body, members)
    return dict(sorted(members.items()))


_WALKERS: dict[str, type[CallWalker]] = {}
for _suffix in (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"):
    _WALKERS[_suffix] = CppCalls


def supports_calls(suffix: str) -> bool:
    return suffix in _WALKERS


def extract_calls(suffix: str, root: Any, ctx: CallContext) -> list[GraphEdge]:
    walker_cls = _WALKERS.get(suffix)
    if walker_cls is None:
        return []
    return walker_cls(ctx).run(root)
