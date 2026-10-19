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

## Qt C++ call edges (0.10.0)

C++ `calls` edges sit next to the Qt wiring and never replace it:

- `emit sig()` / `Q_EMIT sig()` stays an `emits` edge and is never also a
  `calls` edge (including an unbraced `if (x) Q_EMIT sig();`).
- `connect(...)`, `SIGNAL(...)`, `SLOT(...)` and `Q_*` macros are never calls;
  `connect` stays a `connects` edge.
- A slot invoked directly (`onDone()`, `this->onDone()`) is an ordinary call
  and resolves like any other member call.
- Calls inside regions the parser swallows into an `ERROR` node after
  `Q_OBJECT` are ignored rather than guessed.

So a QML handler can be traced to C++ helpers as
`handles` -> `connects` -> `calls` with `cortex_path`. With the regex fallback,
C++ calls are name-only (`confidence: LOW`, never resolved to a symbol).

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
