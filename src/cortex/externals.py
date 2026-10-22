"""Which `inherits` bases (and, from Task 3.2 on, which calls) leave the repository.

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
