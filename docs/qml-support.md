# QML support

Cortex 0.8 indexes Qt 5 and Qt 6 QML locally. The pinned `qmljs` Tree-sitter
grammar supplies declaration boundaries; Cortex does not run Qt, `qmllint`, a
QML compiler, or a language server.

## Indexed declarations

Pragmas, URI/version/aliased imports, file components and base types, nested
objects, IDs, properties and modifiers (`default`, `final`, `override`,
`readonly`, `required`, and `virtual`), aliases, signals (including the
parenthesis-free form), methods/generators, typed parameters and returns,
enums/members, inline components, ordinary/grouped/attached/object/array/script
bindings, behaviors, annotations, and embedded JavaScript reads/writes/calls
are represented as searchable graph nodes. Ordinary properties also expose an
implicit `<property>Changed` signal marked `implicit=true`.

Relations include `binds`, `reads`, `writes`, `references`, `aliases`, and
`exports`, alongside the existing `contains`, `imports`, `inherits`,
`instantiates`, and `handles` relations. Dynamic properties and unresolved
framework types remain explicitly `unverified`; Cortex never guesses a target
from an ambiguous basename.

## Modules and registration

Checked-in `qmldir` and `.qmltypes` files, QML JavaScript imports, qrc aliases,
`qt_add_qml_module`/`qt_target_qml_sources` metadata, and Qt 5/6 C++ QML
registration macros/APIs are indexed. Local modules resolve by URI, version,
directory, export, and scope. Framework and third-party module internals are
external placeholders unless their declarations are in the repository.

## Capability boundary

A managed, hash-verified runtime provides Tree-sitter grammars after first
setup. If setup is unavailable or `CORTEX_RUNTIME_NETWORK=0` is used without a
pre-seeded bundle, Cortex reports degraded capability and uses the visible
regex fallback. Ingest, search, context, references, risk, dead-code, export,
and report remain functional. Parser setup never uploads repository paths,
source, or graph data.
