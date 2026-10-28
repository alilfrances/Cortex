"""Which `inherits` bases and which `calls` leave the repository.

The resolver (`callresolve`) never rewrites an out-of-repo target: it only records *why* an edge stayed
`name:`/`module:` as ``metadata.external = <rule>``, so the status gates can leave those edges out of the
denominator (``symbol / (symbol + name + module)``).  Everything here is a pure function of strings: no
graph access, no I/O.  The rule names are the vocabulary of ``metadata.external``:

    qt           a ``Q[A-Z]...`` class (``QObject``, ``QThread``)
    std          written under ``std::``
    prefix       written under another known library prefix (``testing::``, ``boost::``, ``Qt::``, ``QtQuick``)
    gtest        a bare ``Test``/``TestWithParam`` in a file that includes a gtest/gmock header
    qml_builtin  a Qt Quick / Controls / Layouts type no repository component is named after
    qml_import   a QML type written under an import alias, or any QML type in a file that imports a module
                 outside the repository

and, for `calls` (C/C++ only; ``classify_call``), ``qt``, ``std`` and ``prefix`` as above plus

    qt_macro     a free ``qWarning`` / ``qobject_cast`` / ``Q_ASSERT``
    libc         a free call of a C library or ``std`` function name (``printf``, ``memcpy``, ``move``)
    inherited    a member call (or an implicit-``this`` call) on a repository class that neither it nor its
                 repository bases has the member of, when a class on that path derives from something outside
                 the repository (``QWidget::update()``); set by ``callresolve``, not by ``classify_call``
"""

from __future__ import annotations

import re
from typing import Iterable

# Scope prefixes of libraries that are never part of the repository (checked on the base as written).
EXTERNAL_PREFIXES = ("std::", "testing::", "boost::", "Qt::", "QtQuick", "QtQml")
# The Qt class naming convention: `QObject`, `QThread`, `QAbstractListModel`.
QT_CLASS_RE = re.compile(r"^Q[A-Z][A-Za-z0-9]*$")
# gtest fixtures are bare `Test` / `TestWithParam<...>` under `using namespace testing`: external only
# when the file includes one of these headers (a repository class named `Test` is a candidate first anyway).
GTEST_BASES = frozenset({"Test", "TestWithParam"})
GTEST_HEADER_MARKERS = ("gtest", "gmock")
# Qt Quick / Controls / Layouts types a QML root commonly derives from.  They only count when no repository
# QML component has the label, which is the case `classify_base` is called for.
QML_BUILTINS = frozenset((
    "ApplicationWindow", "Button", "Column", "ColumnLayout", "Control", "Dialog", "Flickable", "Flow",
    "Grid", "GridLayout", "GridView", "Image", "Item", "Label", "ListView", "Loader", "MouseArea", "Page",
    "Pane", "Popup", "QtObject", "Rectangle", "Repeater", "Row", "RowLayout", "StackLayout", "Text",
    "TextField", "WebEngineView", "Window",
))
# The families `callresolve.language_family` names for C/C++ and QML sources.
CPP_FAMILY = "c"
QML_FAMILY = ".qml"

# Free C library / `std` function names (also reached unqualified through `using namespace std`).  A call
# only counts when no function, class or out-of-line member of the repository carries the name: this is
# a list of names, not of declarations, and `min` or `close` are common names for repository functions too.
LIBC_STD_NAMES = frozenset((
    "abort", "abs", "assert", "atoi", "atol", "calloc", "ceil", "clock", "close", "exit", "fabs", "fclose",
    "fflush", "floor", "fopen", "forward", "fprintf", "fread", "free", "fwrite", "get", "getenv",
    "make_shared", "make_unique", "malloc", "max", "memcmp", "memcpy", "memmove", "memset", "min", "move",
    "open", "poll", "pow", "printf", "read", "realloc", "select", "sleep", "snprintf", "sprintf", "sqrt",
    "strcat", "strchr", "strcmp", "strcpy", "strlen", "strncmp", "strncpy", "strstr", "strtod", "strtol",
    "strtoul", "swap", "tie", "time", "usleep", "write",
))
# `qWarning`, `qMax` / `Q_ASSERT`, `Q_UNUSED`, and the two lower-case cast templates `qobject_cast`,
# `qgraphicsitem_cast`.
QT_MACRO_RE = re.compile(r"^(?:q[A-Z]\w*|q(?:object|graphicsitem)_cast|Q_[A-Z_]+)$")

_SCHEMES = ("name:", "module:")


def written_base(written: str) -> str:
    """The base as the extractor wrote it, without the ``name:``/``module:`` scheme and a leading ``::``."""
    text = written.strip()
    for scheme in _SCHEMES:
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    return text.removeprefix("::")


def has_external_prefix(text: str) -> str | None:
    """``"std"`` / ``"prefix"`` when `text` (a base without scheme) starts with a known external prefix."""
    if text.startswith("std::"):
        return "std"
    if any(text.startswith(prefix) for prefix in EXTERNAL_PREFIXES):
        return "prefix"
    return None


def classify_base(label: str, written: str, file_imports: Iterable[str], family: str) -> str | None:
    """The rule that makes an `inherits` base external, or ``None``.

    `label` is the class label the edge names (the last scope segment, template arguments dropped),
    `written` the base as the extractor wrote it (``name:::testing::Test``), `file_imports` what the
    edge's file imports from outside the repository (``module:`` targets, header or module names; for QML
    only the modules that are not a repository directory) and `family` the language family of the file
    (``callresolve.language_family``).  Only C/C++ and QML sources are classified; the caller has already
    established that no class in that family carries `label`.
    """
    if family not in (CPP_FAMILY, QML_FAMILY):
        return None
    imports = list(file_imports)
    text = written_base(written)
    prefixed = has_external_prefix(text)
    if prefixed:
        return prefixed
    if family == QML_FAMILY:
        if "." in text.split("<", 1)[0]:
            return "qml_import"  # `Controls.TextField`: written under an import alias
        if label in QML_BUILTINS:
            return "qml_builtin"
        return "qml_import" if bool(imports) else None
    if QT_CLASS_RE.match(label):
        return "qt"
    if label in GTEST_BASES and any(
        marker in item.lower() for item in imports for marker in GTEST_HEADER_MARKERS
    ):
        return "gtest"
    return None


def _type_rule(text: str) -> str | None:
    """The rule for a scope / receiver type written as `text` (``QString``, ``std``, ``boost::asio``,
    ``QList<int>``), or ``None``."""
    base = text.split("<", 1)[0].strip().removeprefix("::")
    if not base:
        return None
    prefixed = has_external_prefix(base if "::" in base else base + "::")
    if prefixed == "std":
        return "std"
    if prefixed:
        return "qt" if base.startswith("Qt") else "prefix"
    return "qt" if QT_CLASS_RE.match(base.rsplit("::", 1)[-1]) else None


def classify_call(md: dict, family: str) -> str | None:
    """The rule that makes a `calls` edge's callee external, or ``None``.

    `md` is the edge's call-site metadata (``callee``, ``receiver_kind``, ``scope``, ``receiver``,
    ``receiver_type``); `family` the caller's language family.  Only C/C++ is classified: the other
    families are counted per language by the status report.  The rules read the call site alone: the
    caller (`callresolve`) has established that nothing in the repository resolves it, that the callee
    is defined nowhere in the repository (free-name rules) and that the scope / receiver type is not a
    repository class (type rules).
    """
    if family != CPP_FAMILY:
        return None
    callee = str(md.get("callee") or "")
    kind = md.get("receiver_kind") or "none"
    if kind == "scoped" or md.get("scope"):
        # `receiver` keeps the whole qualifier (`boost::asio`), `scope` only its last segment.
        for text in (md.get("receiver") if kind == "scoped" else None, md.get("scope")):
            rule = _type_rule(str(text)) if text else None
            if rule:
                return rule
        return None
    receiver_type = md.get("receiver_type")
    if receiver_type:
        return _type_rule(str(receiver_type))
    if kind == "new" or kind == "none":
        if QT_CLASS_RE.match(callee):
            return "qt"
        if kind == "none":
            if QT_MACRO_RE.match(callee):
                return "qt_macro"
            if callee in LIBC_STD_NAMES:
                return "libc"
    return None
