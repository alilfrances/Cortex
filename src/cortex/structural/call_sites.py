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
resolver joins the two.  Qt's ``Q_D(Foo)`` declares ``d`` as a ``FooPrivate`` and ``Q_Q(Foo)`` declares ``q`` as a
``Foo``, both local to the function (``CppCalls.declare_pimpl``).
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
    locals: set[str] = field(default_factory=set)  # Ruby: names that are variables, not bare calls
    # Go: the method receiver's name (`s` in `func (s *Svc) M()`).  Stored on the
    # frame itself -- an `id(frame)`-keyed side table is unsound because CPython
    # reuses ids of collected frames, which leaked a method's receiver name into
    # later plain functions (seen on gorilla/mux: `r *http.Request` became `this`).
    receiver_name: str | None = None


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
# `qobject_cast<T*>(x)` and friends: the value has the type of the template argument.
_CPP_CASTS = frozenset({
    "qobject_cast", "dynamic_cast", "static_cast", "reinterpret_cast", "const_cast",
    "dynamic_pointer_cast", "static_pointer_cast", "const_pointer_cast",
    "qSharedPointerCast", "qSharedPointerDynamicCast", "qSharedPointerObjectCast", "qSharedPointerConstCast",
})
# `Q_D(Foo)` / `Q_D(const Foo)` / `Q_D(ns::Foo)`: Qt's pimpl macro declares `FooPrivate *const d = d_func();`;
# `Q_Q(Foo)` (in the private class) declares `Foo *const q = q_func();`.
_QT_PIMPL_MACROS = {"Q_D": "d", "Q_Q": "q"}  # macro -> the variable it declares
_QT_PIMPL_ARGUMENT = re.compile(r"^\s*\(\s*(?:const\s+)?(?:\w+\s*::\s*)*(\w+)\s*\)\s*$")
_CPP_TYPE_ID = {"type_identifier", "namespace_identifier"}
_CPP_DECLARATOR_NAMES = {"identifier", "field_identifier"}
_CPP_DEF_NODES = {"function_definition"}
_CPP_CLASS_NODES = {"class_specifier", "struct_specifier"}
# A branch of a preprocessor conditional holds declarations of its own (`#ifdef X`, `#elif`, `#else` inside a class body).
_CPP_PREPROC_BRANCHES = {"preproc_ifdef", "preproc_if", "preproc_else", "preproc_elif", "preproc_elifdef"}


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
            if name in _CPP_CASTS:
                args = fn.child_by_field_name("arguments")
                first = next(iter(args.named_children), None) if args is not None else None
                return self.type_name(first)
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

    def is_cast(self, value: Any) -> bool:
        fn = value.child_by_field_name("function")
        if fn is None or fn.type != "template_function":
            return False
        name = fn.child_by_field_name("name")
        return name is not None and self.text(name) in _CPP_CASTS

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
        """Record the fields of a class body, also those in the branches of `#ifdef` / `#if` (all of them: which
        branch compiles is not known here)."""
        for child in body.named_children:
            if child.type == "field_declaration":
                self.add_declaration(child, Frame("", "", None, into))
            elif child.type in _CPP_PREPROC_BRANCHES:
                self.class_fields(child, into)

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
        if kind == "lambda_expression" and frame.in_function:
            # `[=](Filter* f) { f->run(); }` -- lambda parameters are declared
            # parameters; captured locals stay visible, the lambda's own
            # declarations do not leak out (a copied var map).
            inner = Frame(frame.symbol_id, frame.owner_label, frame.class_name, dict(frame.vars), True)
            declarator = node.child_by_field_name("declarator")
            params = declarator.child_by_field_name("parameters") if declarator is not None else None
            for param in params.named_children if params is not None else ():
                if param.type == "parameter_declaration":
                    self.add_declaration(param, inner)
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
        if kind == "function_declarator" and frame.in_function:
            # `Q_D(const Foo);` is not an expression, so the parser reads it as an ERROR holding a declarator
            name = node.child_by_field_name("declarator")
            params = node.child_by_field_name("parameters")
            if name is not None and params is not None and self.text(name) in _QT_PIMPL_MACROS:
                self.declare_pimpl(self.text(name), self.text(params), frame)
            return
        if kind != "call_expression":
            return
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if frame.in_function and fn.type == "identifier" and self.text(fn) in _QT_PIMPL_MACROS:
            arguments = node.child_by_field_name("arguments")
            if arguments is not None:
                self.declare_pimpl(self.text(fn), self.text(arguments), frame)
            return
        if self.follows_emit_keyword(node):
            return
        self.call_site(node, fn, frame)

    def declare_pimpl(self, macro: str, arguments: str, frame: Frame) -> None:
        """`Q_D(Foo)` declares `d` as a `FooPrivate` in this function (the private class, last name segment);
        `Q_Q(Foo)` declares `q` as a `Foo`."""
        match = _QT_PIMPL_ARGUMENT.match(arguments)
        if match:
            frame.vars[_QT_PIMPL_MACROS[macro]] = match.group(1) + ("Private" if macro == "Q_D" else "")

    def follows_emit_keyword(self, node: Any) -> bool:
        """`if (x) Q_EMIT sig();` -- an unbraced control-flow body parses as
        ``ERROR(emit) expression_statement(call_expression)``; that site is an
        ``emits`` edge (regex Qt pass), never also a ``calls`` edge."""
        parent = node.parent
        if parent is None or parent.type != "expression_statement":
            return False
        previous = parent.prev_sibling
        return previous is not None and previous.type == "ERROR" and self.text(previous).strip() in {"emit", "Q_EMIT"}

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
        if akind == "call_expression":
            # `qobject_cast<Widget*>(o)->refresh()`: typed by the cast, like a local initialised by it
            cast = self.constructed_type(arg) if self.is_cast(arg) else None
            if cast:
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=cast, extra=None)
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


# --------------------------------------------------------------------------- #
# JavaScript / TypeScript / TSX
# --------------------------------------------------------------------------- #

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_JS_CLASS_NODES = {"class_declaration", "class", "abstract_class_declaration", "class_expression"}
_JS_METHOD_NODES = {"method_definition", "method_signature", "abstract_method_signature"}
_JS_FUNCTION_NODES = {"function_declaration", "generator_function_declaration"}
_JS_INNER_FUNCTIONS = {"arrow_function", "function_expression", "function", "generator_function"}
_JS_SKIP_CALLEES = frozenset({"require", "super", "import"})
_NULLISH = {"null", "undefined", "void", "never"}


@dataclass(slots=True)
class Binding:
    kind: str  # "named" | "default" | "namespace"
    spec: str
    file: str | None
    imported: str  # exported name (named) or local name


def resolve_js_spec(importer: str, spec: str, known: set[str]) -> str | None:
    """Repo file a relative import specifier names (extension/index probing); `None` for
    bare specifiers (packages) and anything that does not match a known file."""
    if not spec.startswith("."):
        return None
    import posixpath

    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    stem, ext = posixpath.splitext(base)
    candidates = [base]
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        candidates += [stem + e for e in (".ts", ".tsx")]
    candidates += [base + e for e in _JS_EXTS]
    candidates += [posixpath.join(base, "index" + e) for e in _JS_EXTS]
    for candidate in candidates:
        if candidate in known:
            return candidate
    return None


class JsCalls(CallWalker):
    language = "js"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.bindings: dict[str, Binding] = {}

    # -- imports ------------------------------------------------------------
    def _strip(self, node: Any) -> str:
        return self.text(node).strip("\"'`")

    def collect_imports(self, root: Any) -> None:
        for child in root.children:
            if child.type == "import_statement":
                source = child.child_by_field_name("source")
                if source is None:
                    continue
                spec = self._strip(source)
                file = resolve_js_spec(self.ctx.path, spec, self.ctx.known_paths)
                for clause in child.children:
                    if clause.type != "import_clause":
                        continue
                    for part in clause.children:
                        if part.type == "identifier":
                            name = self.text(part)
                            self.bindings[name] = Binding("default", spec, file, name)
                        elif part.type == "namespace_import":
                            ident = next((c for c in part.children if c.type == "identifier"), None)
                            if ident is not None:
                                name = self.text(ident)
                                self.bindings[name] = Binding("namespace", spec, file, name)
                        elif part.type == "named_imports":
                            for spec_node in part.children:
                                if spec_node.type != "import_specifier":
                                    continue
                                name_node = spec_node.child_by_field_name("name")
                                alias = spec_node.child_by_field_name("alias")
                                if name_node is None:
                                    continue
                                imported = self.text(name_node)
                                local = self.text(alias) if alias is not None else imported
                                self.bindings[local] = Binding("named", spec, file, imported)
            elif child.type in {"lexical_declaration", "variable_declaration"}:
                for decl in child.children:
                    if decl.type == "variable_declarator":
                        self._collect_require(decl)

    def _collect_require(self, decl: Any) -> None:
        value = decl.child_by_field_name("value")
        target = decl.child_by_field_name("name")
        if value is None or target is None or value.type != "call_expression":
            return
        fn = value.child_by_field_name("function")
        args = value.child_by_field_name("arguments")
        if fn is None or args is None or self.text(fn) != "require":
            return
        first = next(iter(args.named_children), None)
        if first is None or first.type not in {"string", "template_string"}:
            return
        spec = self._strip(first)
        file = resolve_js_spec(self.ctx.path, spec, self.ctx.known_paths)
        if target.type == "identifier":
            name = self.text(target)
            self.bindings[name] = Binding("namespace", spec, file, name)
        elif target.type == "object_pattern":
            for item in target.named_children:
                if item.type == "shorthand_property_identifier_pattern":
                    name = self.text(item)
                    self.bindings[name] = Binding("named", spec, file, name)
                elif item.type == "pair_pattern":
                    key, val = item.child_by_field_name("key"), item.child_by_field_name("value")
                    if key is not None and val is not None and val.type == "identifier":
                        self.bindings[self.text(val)] = Binding("named", spec, file, self.text(key))

    def run(self, root: Any) -> list[GraphEdge]:
        self.collect_imports(root)
        self.run_prescan(root)
        return super().run(root)

    # -- types --------------------------------------------------------------
    def type_name(self, node: Any | None) -> str | None:
        if node is None:
            return None
        kind = node.type
        if kind == "type_annotation":
            return self.type_name(next(iter(node.named_children), None))
        if kind == "type_identifier":
            return self.text(node)
        if kind == "nested_type_identifier":
            return self.type_name(node.child_by_field_name("name"))
        if kind == "generic_type":
            return self.type_name(node.child_by_field_name("name"))
        if kind == "parenthesized_type":
            return self.type_name(next(iter(node.named_children), None))
        if kind == "union_type":
            found = {
                name
                for child in node.named_children
                if self.text(child) not in _NULLISH and (name := self.type_name(child))
            }
            return next(iter(found)) if len(found) == 1 else None
        return None

    def constructed(self, value: Any | None) -> str | None:
        while value is not None and value.type in {"await_expression", "parenthesized_expression"}:
            value = next(iter(value.named_children), None)
        if value is not None and value.type == "new_expression":
            ctor = value.child_by_field_name("constructor")
            if ctor is not None and ctor.type in {"identifier", "type_identifier"}:
                return self.text(ctor)
        return None

    def add_var(self, name_node: Any | None, type_node: Any | None, value: Any | None, vars_: dict[str, str]) -> None:
        if name_node is None or name_node.type != "identifier":
            return
        name = self.text(name_node)
        resolved = self.type_name(type_node) or self.constructed(value)
        if resolved:
            vars_[name] = resolved
        else:
            vars_.pop(name, None)

    def add_parameters(self, params: Any | None, vars_: dict[str, str]) -> None:
        if params is None:
            return
        for param in params.named_children:
            if param.type in {"required_parameter", "optional_parameter"}:
                self.add_var(param.child_by_field_name("pattern"), param.child_by_field_name("type"), None, vars_)
            elif param.type == "identifier":
                vars_.pop(self.text(param), None)

    def class_fields(self, body: Any) -> dict[str, str]:
        fields: dict[str, str] = {}
        for member in body.named_children:
            if member.type in {"public_field_definition", "field_definition"}:
                name = member.child_by_field_name("name")
                if name is None:
                    name = member.child_by_field_name("property")
                if name is None:
                    continue
                kind = self.type_name(member.child_by_field_name("type")) or self.constructed(
                    member.child_by_field_name("value")
                )
                if kind:
                    fields[self.text(name)] = kind
            elif member.type == "method_definition":
                name = member.child_by_field_name("name")
                if name is not None and self.text(name) == "constructor":
                    params = member.child_by_field_name("parameters")
                    for param in (params.named_children if params is not None else []):
                        # TS parameter properties: constructor(private repo: Repo)
                        if param.type in {"required_parameter", "optional_parameter"} and any(
                            c.type in {"accessibility_modifier", "readonly"} for c in param.children
                        ):
                            self.add_var(
                                param.child_by_field_name("pattern"), param.child_by_field_name("type"), None, fields
                            )
        return fields

    # -- scope --------------------------------------------------------------
    def enter(self, node: Any, frame: Frame) -> Frame:
        kind = node.type
        if kind in _JS_CLASS_NODES:
            body = node.child_by_field_name("body")
            if body is None:
                return frame
            name = self.ctx.name_of(node, self.ctx.source) or None
            return Frame(frame.symbol_id, frame.owner_label, name, {}, False)
        if kind in _JS_METHOD_NODES and node.type == "method_definition":
            inner = self.definition_frame(node, frame, class_name=frame.class_name, label="")
            inner.vars = dict(self.fields_of.get(frame.class_name or "", {}))
            self.add_parameters(node.child_by_field_name("parameters"), inner.vars)
            return inner
        if kind in _JS_FUNCTION_NODES:
            inner = self.definition_frame(node, frame, class_name=None, label="")
            inner.vars = {}
            self.add_parameters(node.child_by_field_name("parameters"), inner.vars)
            return inner
        if kind in _JS_INNER_FUNCTIONS:
            class_name = frame.class_name if kind == "arrow_function" else None
            inner = Frame(frame.symbol_id, frame.owner_label, class_name, dict(frame.vars), frame.in_function)
            self.add_parameters(node.child_by_field_name("parameters"), inner.vars)
            return inner
        return frame

    fields_of: dict[str, dict[str, str]]

    def run_prescan(self, root: Any) -> None:
        """Class field types by class name, so methods see fields declared after them."""
        self.fields_of = {}
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in _JS_CLASS_NODES:
                body = node.child_by_field_name("body")
                name = self.ctx.name_of(node, self.ctx.source)
                if body is not None and name:
                    self.fields_of.setdefault(name, {}).update(self.class_fields(body))
            stack.extend(node.children)

    # -- sites --------------------------------------------------------------
    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if kind == "variable_declarator":
            self.add_var(node.child_by_field_name("name"), node.child_by_field_name("type"), node.child_by_field_name("value"), frame.vars)
            return
        if kind == "new_expression":
            ctor = node.child_by_field_name("constructor")
            if ctor is not None and ctor.type in {"identifier", "type_identifier"}:
                self.free_site(node, frame, self.text(ctor), "new")
            elif ctor is not None and ctor.type == "member_expression":
                self.member_site(node, ctor, frame, kind_override="new")
            return
        if kind != "call_expression":
            return
        fn = node.child_by_field_name("function")
        while fn is not None and fn.type in {"parenthesized_expression", "non_null_expression"}:
            fn = next(iter(fn.named_children), None)
        if fn is None:
            return
        if fn.type == "identifier":
            callee = self.text(fn)
            if callee not in _JS_SKIP_CALLEES:
                self.free_site(node, frame, callee, "none")
        elif fn.type == "member_expression":
            self.member_site(node, fn, frame)

    def free_site(self, node: Any, frame: Frame, name: str, kind: str) -> None:
        extra: dict[str, Any] = {"implicit_receiver": False} if kind == "none" else {}
        callee = name
        binding = self.bindings.get(name)
        if binding is not None:
            callee = binding.imported if binding.kind == "named" else name
            extra.update({"import_spec": binding.spec, "import_file": binding.file})
            if binding.kind == "default":
                extra["import_default"] = True
        self.emit(node, frame, callee, kind=kind, extra=extra or None)

    def type_file(self, type_name: str | None) -> dict[str, Any] | None:
        """Where an imported type name lives, so duplicate class names pick the imported one."""
        binding = self.bindings.get(type_name or "")
        if binding is not None and binding.file and binding.kind in {"named", "default"}:
            return {"receiver_type_file": binding.file}
        return None

    def unwrap(self, node: Any) -> Any:
        while node is not None and node.type in {"parenthesized_expression", "non_null_expression", "await_expression"}:
            node = next(iter(node.named_children), None)
        return node

    def member_site(self, node: Any, fn: Any, frame: Frame, kind_override: str | None = None) -> None:
        prop = fn.child_by_field_name("property")
        obj = self.unwrap(fn.child_by_field_name("object"))
        if prop is None or obj is None:
            return
        callee = self.text(prop)
        receiver = self.text(obj)
        otype = obj.type
        if otype == "this":
            self.emit(node, frame, callee, receiver="this", kind="this")
            return
        if otype == "super":
            self.emit(node, frame, callee, receiver="super", kind="this", extra={"super": True})
            return
        if otype == "identifier":
            binding = self.bindings.get(receiver)
            if binding is not None and receiver not in frame.vars:
                self.emit(
                    node, frame, callee, receiver=receiver, kind="scoped", scope=receiver,
                    extra={
                        "import_spec": binding.spec, "scope_file": binding.file,
                        "scope_namespace": binding.kind == "namespace",
                    },
                )
                return
            rtype = frame.vars.get(receiver)
            self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
            return
        if otype == "member_expression":
            base = self.unwrap(obj.child_by_field_name("object"))
            member = obj.child_by_field_name("property")
            if base is not None and base.type == "this" and member is not None:
                rtype = self.fields_of.get(frame.class_name or "", {}).get(self.text(member))
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
                return
        if otype == "new_expression":
            ctor = obj.child_by_field_name("constructor")
            rtype = self.text(ctor) if ctor is not None and ctor.type in {"identifier", "type_identifier"} else None
            self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
            return
        if otype in {"as_expression", "type_assertion"}:
            type_node = obj.child_by_field_name("type")
            if type_node is None and obj.named_children:
                type_node = obj.named_children[-1]
            self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=self.type_name(type_node))
            return
        self.emit(node, frame, callee, receiver=receiver, kind="chain")


# --------------------------------------------------------------------------- #
# Go / Rust / Swift / Java / Ruby
# --------------------------------------------------------------------------- #

import posixpath

_CACHE: dict[str, tuple[set[str], int, Any]] = {}


def _memo(name: str, known: set[str], build: Callable[[set[str]], Any]) -> Any:
    """Per-batch index over `known_paths` (identity+size keyed) so per-file walkers stay O(1).

    The cache holds a strong reference to ``known``: keying on ``id(known)`` alone
    is unsound because CPython reuses the id of a collected set, so a later batch
    (another repo ingested in the same process, as the eval harness does) could be
    served the previous batch's index."""
    hit = _CACHE.get(name)
    if hit is not None and hit[0] is known and hit[1] == len(known):
        return hit[2]
    value = build(known)
    _CACHE[name] = (known, len(known), value)
    return value


def _suffix_index(known: set[str], ext: str) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in sorted(known):
        if path.endswith(ext):
            parts = path[: -len(ext)].split("/")
            for i in range(len(parts)):
                index.setdefault("/".join(parts[i:]), []).append(path)
    return index


def _is_type_name(name: str) -> bool:
    return bool(name) and name[0].isupper()


def _field(node: Any | None, *names: str) -> Any | None:
    if node is None:
        return None
    for name in names:
        child = node.child_by_field_name(name)
        if child is not None:
            return child
    return None


def _first_named(node: Any | None) -> Any | None:
    return next(iter(node.named_children), None) if node is not None else None


def method_qualifier(suffix: str, node: Any, source: bytes) -> str | None:
    """Owning type of a method that is *not* contained by its type's span (Go receivers, Rust
    `impl` blocks, Swift `extension`s): recorded as the node's `qualifier` so the resolver can
    find members without a class node."""
    def text(n: Any) -> str:
        return _text(n, source)

    if suffix == ".go" and node.type == "method_declaration":
        recv = node.child_by_field_name("receiver")
        param = _first_named(recv)
        typ = param.child_by_field_name("type") if param is not None else None
        while typ is not None and typ.type in {"pointer_type", "generic_type", "parenthesized_type"}:
            typ = typ.child_by_field_name("type") if typ.type == "generic_type" else _first_named(typ)
        return text(typ) if typ is not None and typ.type == "type_identifier" else None
    if suffix == ".rs" and node.type == "function_item":
        cursor = node.parent
        while cursor is not None and cursor.type != "impl_item":
            if cursor.type in {"function_item", "source_file"}:
                return None
            cursor = cursor.parent
        typ = cursor.child_by_field_name("type") if cursor is not None else None
        while typ is not None and typ.type in {"generic_type", "reference_type"}:
            typ = typ.child_by_field_name("type")
        return text(typ).split("::")[-1] if typ is not None and typ.type in {"type_identifier", "scoped_type_identifier"} else None
    if suffix == ".swift" and node.type == "function_declaration":
        cursor = node.parent
        while cursor is not None and cursor.type != "class_declaration":
            cursor = cursor.parent
        if cursor is not None and any(c.type == "extension" or text(c) == "extension" for c in cursor.children if not c.is_named):
            name = cursor.child_by_field_name("name")
            inner = name
            while inner is not None and inner.type != "type_identifier":
                inner = _first_named(inner)
            return text(inner) if inner is not None else None
    return None


def class_bases(suffix: str, node: Any, source: bytes) -> list[str]:
    """Declared base type names of a JS/TS/Java/Swift class (recorded as `bases` metadata:
    those extractors emit no `inherits` edges)."""
    names: list[str] = []

    def collect(n: Any) -> None:
        if n.type in {"type_identifier", "identifier"}:
            names.append(_text(n, source))
            return
        for child in n.named_children:
            collect(child)

    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        for child in node.children:
            if child.type == "class_heritage":
                for clause in child.children:
                    if clause.type in {"extends_clause"}:
                        for value in clause.named_children:
                            if value.type in {"identifier", "type_identifier"}:
                                names.append(_text(value, source))
                            elif value.type == "member_expression":
                                names.append(_text(value, source).split(".")[-1])
                    elif clause.type in {"identifier", "type_identifier"}:
                        names.append(_text(clause, source))  # JS: `class A extends B`
    elif suffix == ".java":
        for field_name in ("superclass", "interfaces"):
            child = node.child_by_field_name(field_name)
            if child is not None:
                collect(child)
    elif suffix == ".swift":
        for child in node.children:
            if child.type == "inheritance_specifier":
                collect(child)
    elif suffix == ".rb":
        def const(n: Any) -> str:
            text = _text(n, source)
            return text.rsplit("::", 1)[-1]

        parent = node.child_by_field_name("superclass")
        if parent is not None:
            for child in parent.named_children:
                if child.type in {"constant", "scope_resolution"}:
                    names.append(const(child))
        body = node.child_by_field_name("body")
        for stmt in (body.named_children if body is not None else []):
            if stmt.type == "call" and stmt.child_by_field_name("receiver") is None:
                meth = stmt.child_by_field_name("method")
                args = stmt.child_by_field_name("arguments")
                if meth is not None and _text(meth, source) in {"include", "prepend"} and args is not None:
                    names.extend(const(a) for a in args.named_children if a.type in {"constant", "scope_resolution"})
    seen: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


# -- Go ----------------------------------------------------------------------

_GO_BUILTINS = frozenset({
    "len", "cap", "make", "new", "append", "copy", "delete", "panic", "recover", "print", "println",
    "close", "complex", "real", "imag", "min", "max", "clear", "string", "int", "int8", "int16", "int32",
    "int64", "uint", "uint8", "uint16", "uint32", "uint64", "float32", "float64", "bool", "byte", "rune",
    "error", "any",
})


def _go_dirs(known: set[str]) -> list[str]:
    dirs = {posixpath.dirname(p) for p in known if p.endswith(".go")}
    return sorted(dirs, key=lambda d: (-len(d), d))


class GoCalls(CallWalker):
    language = "go"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.imports: dict[str, str | None] = {}  # alias -> repo dir (None: external)
        self.fields_of: dict[str, dict[str, str]] = {}

    def go_type(self, node: Any | None) -> str | None:
        while node is not None and node.type in {"pointer_type", "parenthesized_type", "slice_type", "array_type"}:
            node = node.child_by_field_name("element") if node.type in {"slice_type", "array_type"} else _first_named(node)
        if node is not None and node.type in {"type_identifier"}:
            return self.text(node)
        if node is not None and node.type == "generic_type":
            return self.go_type(node.child_by_field_name("type"))
        return None

    def collect(self, root: Any) -> None:
        dirs = _memo("go_dirs", self.ctx.known_paths, _go_dirs)
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "import_spec":
                path_node = node.child_by_field_name("path")
                name_node = node.child_by_field_name("name")
                if path_node is not None:
                    spec = self.text(path_node).strip('"`')
                    alias = self.text(name_node) if name_node is not None else spec.rstrip("/").split("/")[-1]
                    if re.fullmatch(r"v\d+", alias) and "/" in spec:
                        alias = spec.split("/")[-2]
                    if alias not in {"_", "."}:
                        self.imports[alias] = next((d for d in dirs if spec == d or spec.endswith("/" + d)), None) if dirs else None
                        if self.imports[alias] == "":
                            self.imports[alias] = None
            elif node.type == "type_spec":
                name = node.child_by_field_name("name")
                typ = node.child_by_field_name("type")
                if name is not None and typ is not None and typ.type == "struct_type":
                    fields: dict[str, str] = {}
                    for decl in typ.named_children:
                        for field in decl.named_children:
                            if field.type == "field_declaration":
                                ftype = self.go_type(field.child_by_field_name("type"))
                                for fname in field.children_by_field_name("name"):
                                    if ftype:
                                        fields[self.text(fname)] = ftype
                    self.fields_of[self.text(name)] = fields
            stack.extend(node.children)

    def run(self, root: Any) -> list[GraphEdge]:
        self.collect(root)
        return super().run(root)

    def add_params(self, params: Any | None, vars_: dict[str, str]) -> None:
        if params is None:
            return
        for decl in params.named_children:
            if decl.type in {"parameter_declaration", "variadic_parameter_declaration"}:
                typ = self.go_type(decl.child_by_field_name("type"))
                for name in decl.children_by_field_name("name"):
                    if typ:
                        vars_[self.text(name)] = typ
                    else:
                        vars_.pop(self.text(name), None)

    def enter(self, node: Any, frame: Frame) -> Frame:
        if node.type == "method_declaration":
            key = (node.start_byte, node.end_byte)
            cls = self.ctx.def_qualifiers.get(key) or method_qualifier(".go", node, self.ctx.source)
            inner = self.definition_frame(node, frame, class_name=cls, label="")
            inner.vars = {}
            recv = node.child_by_field_name("receiver")
            param = _first_named(recv)
            if param is not None and cls:
                for name in param.children_by_field_name("name"):
                    inner.vars[self.text(name)] = cls
                    inner.receiver_name = self.text(name)
            self.add_params(node.child_by_field_name("parameters"), inner.vars)
            return inner
        if node.type == "function_declaration":
            inner = self.definition_frame(node, frame, class_name=None, label="")
            inner.vars = {}
            self.add_params(node.child_by_field_name("parameters"), inner.vars)
            return inner
        if node.type == "func_literal":
            inner = Frame(frame.symbol_id, frame.owner_label, frame.class_name, dict(frame.vars), frame.in_function)
            inner.receiver_name = frame.receiver_name
            self.add_params(node.child_by_field_name("parameters"), inner.vars)
            return inner
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if kind == "var_spec":
            typ = self.go_type(node.child_by_field_name("type"))
            for name in node.children_by_field_name("name"):
                if typ:
                    frame.vars[self.text(name)] = typ
            return
        if kind == "short_var_declaration":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and right is not None:
                for lhs, rhs in zip(left.named_children, right.named_children):
                    lit = rhs
                    if lit.type == "unary_expression":
                        lit = _field(lit, "operand") if _field(lit, "operand") is not None else lit
                    typ = self.go_type(lit.child_by_field_name("type")) if lit.type == "composite_literal" else None
                    if lhs.type == "identifier":
                        if typ:
                            frame.vars[self.text(lhs)] = typ
                        else:
                            frame.vars.pop(self.text(lhs), None)
            return
        if kind != "call_expression":
            return
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name = self.text(fn)
            if name not in _GO_BUILTINS:
                self.emit(node, frame, name, extra={"implicit_receiver": False, "package_scoped": True})
            return
        if fn.type != "selector_expression":
            return
        operand, field_node = fn.child_by_field_name("operand"), fn.child_by_field_name("field")
        if operand is None or field_node is None:
            return
        callee, receiver = self.text(field_node), self.text(operand)
        if operand.type == "identifier":
            if receiver in self.imports and receiver not in frame.vars:
                self.emit(
                    node, frame, callee, receiver=receiver, kind="scoped", scope=receiver,
                    extra={"scope_dir": self.imports[receiver], "scope_namespace": True, "scope_file": None}
                    if self.imports[receiver] else {"scope_dir": None, "scope_namespace": True, "scope_file": None},
                )
            elif frame.receiver_name == receiver:
                self.emit(node, frame, callee, receiver=receiver, kind="this")
            else:
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=frame.vars.get(receiver))
            return
        if operand.type == "selector_expression":
            base, member = operand.child_by_field_name("operand"), operand.child_by_field_name("field")
            if base is not None and member is not None and base.type == "identifier":
                owner = frame.vars.get(self.text(base))
                rtype = self.fields_of.get(owner or "", {}).get(self.text(member))
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype)
                return
        self.emit(node, frame, callee, receiver=receiver, kind="chain")


# -- Rust --------------------------------------------------------------------

_RUST_WRAPPERS = frozenset({"Box", "Rc", "Arc", "RefCell", "Mutex", "Option", "RwLock", "Cell"})
_RUST_FACTORIES = frozenset({"new", "default", "create", "build", "from", "open", "with_capacity"})
_RUST_SKIP = frozenset({"Some", "Ok", "Err", "None", "drop", "Box"})


def _rust_modules(known: set[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for path in sorted(known):
        if path.endswith("/mod.rs"):
            index.setdefault(posixpath.basename(posixpath.dirname(path)), []).append(path)
        elif path.endswith(".rs"):
            index.setdefault(posixpath.basename(path)[:-3], []).append(path)
    return index


class RustCalls(CallWalker):
    language = "rust"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.uses: dict[str, tuple[str, str]] = {}  # local -> (module segment, item)
        self.fields_of: dict[str, dict[str, str]] = {}

    def module_file(self, name: str) -> str | None:
        paths = _memo("rust_modules", self.ctx.known_paths, _rust_modules).get(name, [])
        if len(paths) > 1:
            here = posixpath.dirname(self.ctx.path)
            near = [p for p in paths if posixpath.dirname(p) == here or posixpath.dirname(posixpath.dirname(p)) == here]
            paths = near or paths
        return paths[0] if len(paths) == 1 else None

    def rust_type(self, node: Any | None) -> str | None:
        while node is not None and node.type in {"reference_type", "pointer_type", "array_type", "slice_type"}:
            node = _field(node, "type", "element")
        if node is None:
            return None
        if node.type == "type_identifier":
            return self.text(node)
        if node.type == "scoped_type_identifier":
            return self.text(node).split("::")[-1]
        if node.type == "generic_type":
            base = self.rust_type(node.child_by_field_name("type"))
            if base in _RUST_WRAPPERS:
                args = node.child_by_field_name("type_arguments")
                return self.rust_type(_first_named(args))
            return base
        return None

    def collect_use(self, node: Any, prefix: list[str]) -> None:
        if node.type == "scoped_identifier":
            parts = prefix + self.text(node).split("::")
            if len(parts) >= 2:
                self.uses[parts[-1]] = (parts[-2], parts[-1])
        elif node.type == "use_as_clause":
            path, alias = node.child_by_field_name("path"), node.child_by_field_name("alias")
            if path is not None and alias is not None:
                parts = prefix + self.text(path).split("::")
                if len(parts) >= 2:
                    self.uses[self.text(alias)] = (parts[-2], parts[-1])
        elif node.type == "scoped_use_list":
            path = node.child_by_field_name("path")
            lst = node.child_by_field_name("list")
            base = prefix + (self.text(path).split("::") if path is not None else [])
            for item in (lst.named_children if lst is not None else []):
                if item.type == "identifier" and len(base) >= 1:
                    self.uses[self.text(item)] = (base[-1], self.text(item))
                else:
                    self.collect_use(item, base)

    def run(self, root: Any) -> list[GraphEdge]:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "use_declaration":
                arg = node.child_by_field_name("argument")
                if arg is not None:
                    self.collect_use(arg, [])
            elif node.type == "struct_item":
                name, body = node.child_by_field_name("name"), node.child_by_field_name("body")
                if name is not None and body is not None:
                    fields: dict[str, str] = {}
                    for fld in body.named_children:
                        if fld.type == "field_declaration":
                            ftype = self.rust_type(fld.child_by_field_name("type"))
                            fname = fld.child_by_field_name("name")
                            if ftype and fname is not None:
                                fields[self.text(fname)] = ftype
                    self.fields_of[self.text(name)] = fields
            stack.extend(node.children)
        return super().run(root)

    def add_params(self, params: Any | None, vars_: dict[str, str]) -> None:
        for param in (params.named_children if params is not None else []):
            if param.type == "parameter":
                pat = param.child_by_field_name("pattern")
                typ = self.rust_type(param.child_by_field_name("type"))
                if pat is not None and pat.type == "identifier":
                    if typ:
                        vars_[self.text(pat)] = typ
                    else:
                        vars_.pop(self.text(pat), None)

    def macro_calls(self, tree: Any, frame: Frame) -> None:
        """Call sites inside a macro invocation's token tree (`assert!(req.matches(&v))`).

        tree-sitter-rust leaves macro arguments as flat token trees, so the dominant
        test idiom (`assert!`, `assert_eq!`, `println!`...) hid every call it wrapped
        (dtolnay/semver's `assert!(req.matches(&parsed))`).  Only the unambiguous
        token shapes are read: `ident(...)`, `recv.ident(...)`, `self.ident(...)`,
        `Path::ident(...)`; `name!(...)` nested macros are skipped.  Attribute and
        `macro_rules!` bodies are not code and are never scanned.
        """
        if not frame.in_function:
            return
        cursor = tree.parent
        while cursor is not None and cursor.type == "token_tree":
            cursor = cursor.parent
        if cursor is None or cursor.type != "macro_invocation":
            return
        tokens = tree.children
        for index, token in enumerate(tokens):
            if token.type != "token_tree" or index == 0 or not self.text(token).startswith("("):
                continue
            name_tok = tokens[index - 1]
            if name_tok.type != "identifier":
                continue
            callee = self.text(name_tok)
            sep = tokens[index - 2] if index >= 2 else None
            base = tokens[index - 3] if index >= 3 else None
            sep_text = self.text(sep) if sep is not None else ""
            if sep_text == "." and base is not None:
                # Only a bare receiver token; `a.b.c(...)` (base preceded by `.`) is a chain.
                chained = index >= 4 and self.text(tokens[index - 4]) in {".", "::"}
                if base.type == "self" and not chained:
                    self.emit(name_tok, frame, callee, receiver="self", kind="self", extra={"in_macro": True})
                elif base.type == "identifier" and not chained:
                    receiver = self.text(base)
                    self.emit(name_tok, frame, callee, receiver=receiver, kind="var",
                              receiver_type=frame.vars.get(receiver), extra={"in_macro": True})
                else:
                    self.emit(name_tok, frame, callee, receiver=self.text(base), kind="chain", extra={"in_macro": True})
            elif sep_text == "::" and base is not None and base.type == "identifier":
                scope = self.text(base)
                if index >= 4 and self.text(tokens[index - 4]) == "::":
                    continue  # `a::B::f(...)` -- leave multi-segment paths alone
                if scope in {"crate", "self", "super"} or callee in _RUST_SKIP:
                    self.emit(name_tok, frame, callee, extra={"implicit_receiver": False, "in_macro": True})
                elif scope == "Self" and frame.class_name:
                    self.emit(name_tok, frame, callee, receiver="Self", kind="this", extra={"in_macro": True})
                elif _is_type_name(scope):
                    self.emit(name_tok, frame, callee, receiver=scope, kind="scoped", scope=scope, extra={"in_macro": True})
                else:
                    file = self.module_file(scope)
                    self.emit(
                        name_tok, frame, callee, receiver=scope, kind="scoped", scope=scope,
                        extra={"scope_file": file, "scope_namespace": True, "in_macro": True}
                        if file else {"in_macro": True},
                    )
            elif sep_text not in {".", "::", "!"} and callee not in _RUST_SKIP:
                extra: dict[str, Any] = {"implicit_receiver": False, "in_macro": True}
                use = self.uses.get(callee)
                if use is not None:
                    extra.update({"import_spec": "::".join(use), "import_file": self.module_file(use[0])})
                    callee = use[1]
                self.emit(name_tok, frame, callee, extra=extra)

    def enter(self, node: Any, frame: Frame) -> Frame:
        if node.type == "impl_item":
            typ = node.child_by_field_name("type")
            name = self.rust_type(typ)
            return Frame(frame.symbol_id, frame.owner_label, name, {}, False)
        if node.type == "function_item":
            inner = self.definition_frame(node, frame, class_name=frame.class_name, label="")
            inner.vars = {}
            self.add_params(node.child_by_field_name("parameters"), inner.vars)
            return inner
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if kind == "let_declaration":
            pat = node.child_by_field_name("pattern")
            typ = self.rust_type(node.child_by_field_name("type")) or self.constructed(node.child_by_field_name("value"))
            if pat is not None and pat.type == "identifier":
                if typ:
                    frame.vars[self.text(pat)] = typ
                else:
                    frame.vars.pop(self.text(pat), None)
            return
        if kind == "token_tree":
            self.macro_calls(node, frame)
            return
        if kind != "call_expression":
            return
        fn = node.child_by_field_name("function")
        if fn is None:
            return
        if fn.type == "identifier":
            name = self.text(fn)
            if name in _RUST_SKIP:
                return
            extra: dict[str, Any] = {"implicit_receiver": False}
            use = self.uses.get(name)
            if use is not None:
                extra.update({"import_spec": "::".join(use), "import_file": self.module_file(use[0])})
                name = use[1]
            self.emit(node, frame, name, extra=extra)
        elif fn.type == "scoped_identifier":
            path, name_node = fn.child_by_field_name("path"), fn.child_by_field_name("name")
            if path is None or name_node is None:
                return
            callee, scope = self.text(name_node), self.text(path).split("::")[-1]
            if scope in {"crate", "self", "super"} or callee in _RUST_SKIP:
                self.emit(node, frame, callee, extra={"implicit_receiver": False})
            elif scope == "Self" and frame.class_name:
                self.emit(node, frame, callee, receiver="Self", kind="this")
            elif _is_type_name(scope):
                self.emit(node, frame, callee, receiver=self.text(path), kind="scoped", scope=scope)
            else:
                file = self.module_file(scope)
                self.emit(
                    node, frame, callee, receiver=self.text(path), kind="scoped", scope=scope,
                    extra={"scope_file": file, "scope_namespace": True} if file else None,
                )
        elif fn.type == "field_expression":
            value, field_node = fn.child_by_field_name("value"), fn.child_by_field_name("field")
            if value is None or field_node is None:
                return
            callee, receiver = self.text(field_node), self.text(value)
            if value.type == "self":
                self.emit(node, frame, callee, receiver="self", kind="self")
            elif value.type == "identifier":
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=frame.vars.get(receiver))
            elif value.type == "field_expression":
                base, member = value.child_by_field_name("value"), value.child_by_field_name("field")
                if base is not None and base.type == "self" and member is not None:
                    rtype = self.fields_of.get(frame.class_name or "", {}).get(self.text(member))
                    self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype)
                else:
                    self.emit(node, frame, callee, receiver=receiver, kind="chain")
            else:
                self.emit(node, frame, callee, receiver=receiver, kind="chain")

    def constructed(self, value: Any | None) -> str | None:
        if value is not None and value.type == "call_expression":
            fn = value.child_by_field_name("function")
            if fn is not None and fn.type == "scoped_identifier":
                path, name = fn.child_by_field_name("path"), fn.child_by_field_name("name")
                if path is not None and name is not None and self.text(name) in _RUST_FACTORIES:
                    scope = self.text(path).split("::")[-1]
                    return scope if _is_type_name(scope) else None
        return None


# -- Swift -------------------------------------------------------------------


class SwiftCalls(CallWalker):
    language = "swift"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.fields_of: dict[str, dict[str, str]] = {}

    def swift_type(self, node: Any | None) -> str | None:
        while node is not None and node.type in {"type_annotation", "optional_type", "parenthesized_type", "user_type"}:
            if node.type == "user_type":
                ident = next((c for c in node.named_children if c.type == "type_identifier"), None)
                return self.text(ident) if ident is not None else None
            node = _field(node, "name") if _field(node, "name") is not None else _first_named(node)
        if node is not None and node.type == "type_identifier":
            return self.text(node)
        return None

    def class_name(self, node: Any) -> str | None:
        name = node.child_by_field_name("name")
        while name is not None and name.type != "type_identifier":
            name = _first_named(name)
        return self.text(name) if name is not None else None

    def declared(self, node: Any, vars_: dict[str, str]) -> None:
        pat = node.child_by_field_name("name")
        ident = None
        if pat is not None:
            ident = _field(pat, "bound_identifier") if _field(pat, "bound_identifier") is not None else _first_named(pat)
        if ident is None or ident.type != "simple_identifier":
            return
        typ = None
        for child in node.children:
            if child.type == "type_annotation":
                typ = self.swift_type(child)
        value = node.child_by_field_name("value")
        if typ is None and value is not None and value.type == "call_expression":
            head = _first_named(value)
            if head is not None and head.type == "simple_identifier" and _is_type_name(self.text(head)):
                typ = self.text(head)
        if typ:
            vars_[self.text(ident)] = typ
        else:
            vars_.pop(self.text(ident), None)

    def run(self, root: Any) -> list[GraphEdge]:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "class_declaration":
                name = self.class_name(node)
                body = node.child_by_field_name("body")
                if name and body is not None:
                    fields = self.fields_of.setdefault(name, {})
                    for member in body.named_children:
                        if member.type == "property_declaration":
                            self.declared(member, fields)
            stack.extend(node.children)
        return super().run(root)

    def enter(self, node: Any, frame: Frame) -> Frame:
        kind = node.type
        if kind == "class_declaration":
            name = self.class_name(node)
            sym = self.ctx.def_symbols.get((node.start_byte, node.end_byte), frame.symbol_id)
            return Frame(sym, frame.owner_label if sym == frame.symbol_id else sym.split(":", 2)[-1], name, {}, False)
        if kind in {"function_declaration", "init_declaration", "deinit_declaration"}:
            if kind == "function_declaration":
                cls = self.ctx.def_qualifiers.get((node.start_byte, node.end_byte)) or frame.class_name
                inner = self.definition_frame(node, frame, class_name=cls, label="")
            else:
                inner = Frame(frame.symbol_id, frame.owner_label, frame.class_name, {}, True)
            inner.vars = dict(self.fields_of.get(inner.class_name or "", {}))
            for param in node.children:
                if param.type == "parameter":
                    names = list(param.children_by_field_name("name"))
                    typ = self.swift_type(names[-1]) if len(names) > 1 else None
                    ident = next((n for n in names if n.type == "simple_identifier"), None)
                    if ident is not None:
                        if typ:
                            inner.vars[self.text(ident)] = typ
                        else:
                            inner.vars.pop(self.text(ident), None)
            return inner
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        if node.type == "property_declaration" and frame.in_function:
            self.declared(node, frame.vars)
            return
        if node.type != "call_expression":
            return
        head = _first_named(node)
        if head is None:
            return
        if head.type == "simple_identifier":
            self.emit(node, frame, self.text(head))
            return
        if head.type != "navigation_expression":
            return
        target = head.child_by_field_name("target")
        suffix = head.child_by_field_name("suffix")
        callee_node = suffix.child_by_field_name("suffix") if suffix is not None else None
        if target is None or callee_node is None:
            return
        callee, receiver = self.text(callee_node), self.text(target)
        if target.type == "self_expression":
            self.emit(node, frame, callee, receiver="self", kind="self")
        elif target.type == "super_expression":
            self.emit(node, frame, callee, receiver="super", kind="self", extra={"super": True})
        elif target.type == "simple_identifier":
            if receiver in frame.vars:
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=frame.vars[receiver])
            elif _is_type_name(receiver):
                self.emit(node, frame, callee, receiver=receiver, kind="scoped", scope=receiver)
            else:
                self.emit(node, frame, callee, receiver=receiver, kind="var")
        else:
            self.emit(node, frame, callee, receiver=receiver, kind="chain")


# -- Java --------------------------------------------------------------------

_JAVA_CLASSES = {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}


class JavaCalls(CallWalker):
    language = "java"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.fields_of: dict[str, dict[str, str]] = {}
        self.imports: dict[str, str | None] = {}

    def java_type(self, node: Any | None) -> str | None:
        if node is None:
            return None
        if node.type in {"type_identifier"}:
            name = self.text(node)
            return None if name == "var" else name
        if node.type == "generic_type":
            return self.java_type(_first_named(node))
        if node.type == "scoped_type_identifier":
            return self.text(node).split(".")[-1]
        return None

    def run(self, root: Any) -> list[GraphEdge]:
        index = _memo("java_suffixes", self.ctx.known_paths, lambda k: _suffix_index(k, ".java"))
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type == "import_declaration":
                text = self.text(node).replace("import", "", 1).replace("static", "", 1).strip().rstrip(";").strip()
                if not text.endswith("*"):
                    dotted = text.replace(".", "/")
                    paths = index.get(dotted, [])
                    self.imports[text.split(".")[-1]] = paths[0] if len(paths) == 1 else None
            elif node.type in _JAVA_CLASSES:
                name, body = node.child_by_field_name("name"), node.child_by_field_name("body")
                if name is not None and body is not None:
                    fields = self.fields_of.setdefault(self.text(name), {})
                    for member in body.named_children:
                        if member.type == "field_declaration":
                            typ = self.java_type(member.child_by_field_name("type"))
                            for decl in member.children_by_field_name("declarator"):
                                ident = decl.child_by_field_name("name")
                                if typ and ident is not None:
                                    fields[self.text(ident)] = typ
            stack.extend(node.children)
        return super().run(root)

    def type_file(self, name: str | None) -> dict[str, Any] | None:
        file = self.imports.get(name or "")
        return {"receiver_type_file": file} if file else None

    def enter(self, node: Any, frame: Frame) -> Frame:
        kind = node.type
        if kind in _JAVA_CLASSES:
            name = self.ctx.name_of(node, self.ctx.source) or None
            sym = self.ctx.def_symbols.get((node.start_byte, node.end_byte), frame.symbol_id)
            return Frame(sym, frame.owner_label if sym == frame.symbol_id else sym.split(":", 2)[-1], name, {}, False)
        if kind in {"method_declaration", "constructor_declaration", "lambda_expression", "static_initializer"}:
            if kind == "method_declaration":
                inner = self.definition_frame(node, frame, class_name=frame.class_name, label="")
            else:
                inner = Frame(frame.symbol_id, frame.owner_label, frame.class_name, {}, True)
            inner.vars = dict(self.fields_of.get(inner.class_name or "", {})) if kind != "lambda_expression" else dict(frame.vars)
            params = node.child_by_field_name("parameters")
            for param in (params.named_children if params is not None else []):
                if param.type in {"formal_parameter", "spread_parameter"}:
                    typ = self.java_type(param.child_by_field_name("type"))
                    name = param.child_by_field_name("name")
                    if name is not None and typ:
                        inner.vars[self.text(name)] = typ
            return inner
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if kind == "local_variable_declaration":
            typ = self.java_type(node.child_by_field_name("type"))
            for decl in node.children_by_field_name("declarator"):
                ident, value = decl.child_by_field_name("name"), decl.child_by_field_name("value")
                resolved = typ
                if resolved is None and value is not None and value.type == "object_creation_expression":
                    resolved = self.java_type(value.child_by_field_name("type"))
                if ident is not None:
                    if resolved:
                        frame.vars[self.text(ident)] = resolved
                    else:
                        frame.vars.pop(self.text(ident), None)
            return
        if kind == "object_creation_expression":
            name = self.java_type(node.child_by_field_name("type"))
            if name:
                file = self.imports.get(name)
                self.emit(node, frame, name, kind="new", extra={"import_spec": name, "import_file": file} if file else None)
            return
        if kind != "method_invocation":
            return
        name_node, obj = node.child_by_field_name("name"), node.child_by_field_name("object")
        if name_node is None:
            return
        callee = self.text(name_node)
        if obj is None:
            self.emit(node, frame, callee)
            return
        receiver = self.text(obj)
        if obj.type == "this":
            self.emit(node, frame, callee, receiver="this", kind="this")
        elif obj.type == "super":
            self.emit(node, frame, callee, receiver="super", kind="this", extra={"super": True})
        elif obj.type == "identifier":
            if receiver in frame.vars:
                rtype = frame.vars[receiver]
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
            elif _is_type_name(receiver):
                file = self.imports.get(receiver)
                self.emit(
                    node, frame, callee, receiver=receiver, kind="scoped", scope=receiver,
                    extra={"scope_file": file, "scope_namespace": False} if file else None,
                )
            else:
                self.emit(node, frame, callee, receiver=receiver, kind="var")
        elif obj.type == "field_access":
            base, member = obj.child_by_field_name("object"), obj.child_by_field_name("field")
            if base is not None and base.type == "this" and member is not None:
                rtype = self.fields_of.get(frame.class_name or "", {}).get(self.text(member))
                self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
            else:
                self.emit(node, frame, callee, receiver=receiver, kind="chain")
        elif obj.type == "object_creation_expression":
            rtype = self.java_type(obj.child_by_field_name("type"))
            self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=rtype, extra=self.type_file(rtype))
        else:
            self.emit(node, frame, callee, receiver=receiver, kind="chain")


# -- Ruby --------------------------------------------------------------------

_RUBY_SKIP = frozenset({
    "require", "require_relative", "load", "attr_accessor", "attr_reader", "attr_writer", "private", "public",
    "protected", "include", "extend", "prepend", "raise", "puts", "print", "p", "pp", "module_function",
    "private_constant", "alias_method", "lambda", "proc", "block_given?", "loop",
})


class RubyCalls(CallWalker):
    """Ruby call sites.  `def`/`def self.x` are symbols (see `_extract_ruby_method_symbols`), a
    method's calls are attributed to it, `@x = T.new` types instance variables per class, and
    `include`d modules / superclasses are `bases` metadata on the class node."""

    language = "ruby"

    def __init__(self, ctx: CallContext) -> None:
        super().__init__(ctx)
        self.fields_of: dict[str, dict[str, str]] = {}

    def run(self, root: Any) -> list[GraphEdge]:
        stack = [root]
        while stack:
            node = stack.pop()
            if node.type in {"class", "module"}:
                cname = self.const_name(node.child_by_field_name("name"))
                if cname:
                    fields = self.fields_of.setdefault(cname, {})
                    inner = [node]
                    while inner:
                        cur = inner.pop()
                        if cur.type == "assignment":
                            left, right = cur.child_by_field_name("left"), cur.child_by_field_name("right")
                            if left is not None and left.type == "instance_variable" and right is not None and right.type == "call":
                                meth = right.child_by_field_name("method")
                                if meth is not None and self.text(meth) == "new":
                                    typ = self.const_name(right.child_by_field_name("receiver"))
                                    if typ:
                                        fields[self.text(left)] = typ
                        if cur is node or cur.type not in {"class", "module"}:
                            inner.extend(cur.children)
            stack.extend(node.children)
        return super().run(root)

    def const_name(self, node: Any | None) -> str | None:
        if node is None:
            return None
        if node.type == "constant":
            return self.text(node)
        if node.type == "scope_resolution":
            return self.const_name(node.child_by_field_name("name"))
        return None

    def enter(self, node: Any, frame: Frame) -> Frame:
        kind = node.type
        if kind in {"class", "module"}:
            name = self.const_name(node.child_by_field_name("name"))
            sym = self.ctx.def_symbols.get((node.start_byte, node.end_byte), frame.symbol_id)
            return Frame(sym, frame.owner_label if sym == frame.symbol_id else sym.split(":", 2)[-1], name, {}, False)
        if kind in {"method", "singleton_method"}:
            sym = self.ctx.def_symbols.get((node.start_byte, node.end_byte), frame.symbol_id)
            owner = frame.owner_label if sym == frame.symbol_id else sym.split(":", 2)[-1]
            inner = Frame(sym, owner, frame.class_name, dict(self.fields_of.get(frame.class_name or "", {})), True)
            params = node.child_by_field_name("parameters")
            for param in (params.named_children if params is not None else []):
                if param.type == "identifier":
                    inner.vars.pop(self.text(param), None)
                    inner.locals.add(self.text(param))
            return inner
        return frame

    def visit(self, node: Any, frame: Frame) -> None:
        kind = node.type
        if kind == "assignment":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if left is not None and left.type in {"identifier", "instance_variable"}:
                name = self.text(left)
                frame.locals.add(name)
                typ = None
                if right is not None and right.type == "call":
                    meth, recv = right.child_by_field_name("method"), right.child_by_field_name("receiver")
                    if meth is not None and self.text(meth) == "new":
                        typ = self.const_name(recv)
                if typ:
                    frame.vars[name] = typ
                else:
                    frame.vars.pop(name, None)
            return
        if kind == "identifier" and node.parent is not None and node.parent.type in {"body_statement", "then", "else"}:
            name = self.text(node)
            if name not in frame.locals and name not in _RUBY_SKIP:
                self.emit(node, frame, name)
            return
        if kind != "call":
            return
        meth, recv = node.child_by_field_name("method"), node.child_by_field_name("receiver")
        if meth is None:
            return
        callee = self.text(meth)
        if callee in _RUBY_SKIP:
            return
        if recv is None:
            self.emit(node, frame, callee)
            return
        receiver = self.text(recv)
        if callee == "new" and recv.type in {"constant", "scope_resolution"}:
            name = self.const_name(recv)
            if name:
                self.emit(node, frame, name, receiver=receiver, kind="new")
            return
        if recv.type == "self":
            self.emit(node, frame, callee, receiver="self", kind="self")
        elif recv.type in {"constant", "scope_resolution"}:
            self.emit(node, frame, callee, receiver=receiver, kind="scoped", scope=self.const_name(recv))
        elif recv.type in {"identifier", "instance_variable"}:
            self.emit(node, frame, callee, receiver=receiver, kind="var", receiver_type=frame.vars.get(receiver))
        else:
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
for _suffix in (".js", ".jsx", ".ts", ".tsx"):
    _WALKERS[_suffix] = JsCalls
_WALKERS.update({".go": GoCalls, ".rs": RustCalls, ".swift": SwiftCalls, ".java": JavaCalls, ".rb": RubyCalls})


def supports_calls(suffix: str) -> bool:
    return suffix in _WALKERS


def extract_calls(suffix: str, root: Any, ctx: CallContext) -> list[GraphEdge]:
    walker_cls = _WALKERS.get(suffix)
    if walker_cls is None:
        return []
    return walker_cls(ctx).run(root)
