#!/usr/bin/env python3
"""Generate a synthetic mixed C++/Python git repository for performance work.

The shape mimics the large application repositories in field reports: C++
classes split across ``.hpp``/``.cpp`` with ``#include`` lines that omit the
directory (so they resolve by basename), cross-file member calls, a small set
of base classes with virtual overrides, Python modules with cross-module
imports, paired tests and a commit history for co-change.

    python scripts/perf/synthetic_repo.py /tmp/perf-repo --modules 800 --commits 60

``--modules 800`` gives about 2.2k files, 17k nodes and 80k edges;
``--modules 2400`` about 6.4k files, 50k nodes and 237k edges. Output is
deterministic for a given argument set (fixed seed).

``--field-shape`` adds the statistics of the large private C++/Qt repository from the
0.12.1 field review (see ``FIELD_SHAPE`` for the stable names tests can target).  It is
additive: the files above are still written, and every other profile is byte-identical
without the flag.  Use ``--commits 120`` or more so a 100-commit range exists.

    python scripts/perf/synthetic_repo.py /tmp/field-repo --modules 40 --commits 120 --field-shape

What it writes (all under ``src/field``, ``tests/field``, ``tests/mocks``, ``tests/header-mocks``, ``js``):

* a base class ``CoreDevice`` (``src/field/coredevice.{h,cpp}``) with virtual
  ``isReady``/``reset``/``waitForReady`` and plain accessors (``name``, ``setName``,
  ``isConnected``, ``hasError``, ``id``) plus an intermediate ``ChannelDevice`` that
  overrides ``isReady``;
* five more ``class CoreDevice`` definitions, so the label has six definitions: three
  mock headers (``tests/mocks/mock_{a,b,c}/coredevice.h``), one class nested in an unrelated
  ``.cpp`` (``src/field/unrelated.cpp``) and a same-named JS helper (``js/coredevice.js``);
* derived classes (about a quarter through ``ChannelDevice``, the rest directly from
  ``CoreDevice``) that override the virtuals.  Their files use concatenated lowercase stems
  of 10+ characters (``widgetwaitready.cpp`` declares ``WidgetWaitReadyStep``), enough of
  them that about 55% of all files in the repository have such a stem;
* a central manager ``src/field/devicemanager.cpp`` with hundreds of out-calls, mostly to
  accessors, made through ``CoreDevice *`` parameters/members and through locals initialised
  by ``qobject_cast<Derived*>``.  Its event handler ``handleEvent`` has no task-word name and
  calls the second manager ``src/field/sessionmanager.cpp`` (``SessionManager::refreshSessions``);
* ``inherits`` edges as the extractor naturally writes them: ``name:CoreDevice`` /
  ``name:ChannelDevice`` for every in-repo base (never ``symbol:``), ``name:QObject`` for the
  Qt base, and QML views (``qml/*view.qml``, one ``module:Item`` edge each) so that about 36%
  of all ``inherits`` targets are ``module:``;
* a mock base one directory above the mocks that use it (review 0.13.0 section D): ``StepBase`` in
  ``src/field/stepbase.h`` and in ``tests/header-mocks/stepbase.h``, and ``StepA``/``StepB``/``StepC`` in
  ``tests/header-mocks/steps/step{a,b,c}.h``, each deriving from it with ``#include <stepbase.h>`` (an
  angle include, so the base stays ambiguous by name until the nearest directory decides);
* classes whose only base is outside the repository, so that ``FIELD_EXTERNAL_SHARE`` (25%) of all
  C++ classes derive from an external base alone: header-only ``QObject`` (70%) and ``QThread``
  (10%) subclasses in ``src/field``, and gtest fixtures (20%) in ``tests/field``, half written
  ``::testing::Test`` and half a bare ``Test`` under ``using namespace testing``.  Nothing calls them;
* a ``CMakeLists.txt`` that every ``CMAKE_EVERY``-th history commit touches, so a 100-commit
  range always includes a build file (``tests_run_all_reason`` is then "build/config file
  changed"); history churn never touches ``tests/`` (on a small repository a single shared
  commit would otherwise cover a file through co-change);
* tests (``tests/field/tst_*.cpp``) for one derived class in ``FIELD_TESTED_ONE_IN``, so about
  80% of the files changed over a 100-commit range have no reaching test (83% measured at
  ``--modules 40``, 84% at ``--modules 200``).

Known gap against the field statistics: only the manager's out-calls and the base-class
lookups were shaped to defeat the 0.12.1 resolver (about 80% of the manager's calls stayed
``name:``, 0 of the ``inherits`` edges resolved); the rest of the repository comes from the
default generator, whose calls all resolve, so repository-wide only about 11% of ``calls``
targets are ``name:`` with 0.12.1 (field: 66%).  With 0.12.2 the manager's calls resolve to
about 80% and in-repo bases to 100% (``scripts/perf/audit_unresolved_calls.py`` prints both);
the profile is still what a regression of those fixes looks like.  Test the manager's file,
not the repository-wide ratio.
"""
from __future__ import annotations

import argparse
import os
import random
import subprocess
from pathlib import Path

BASES = 10
METHODS = 12
HOOKS = 6

# --field-shape: stable names for tests that target the profile.
FIELD_SHAPE = {
    "base_class": "CoreDevice",
    "base_header": "src/field/coredevice.h",
    "base_impl": "src/field/coredevice.cpp",
    "mid_class": "ChannelDevice",
    "mid_impl": "src/field/channeldevice.cpp",
    "mock_headers": [f"tests/mocks/mock_{x}/coredevice.h" for x in "abc"],
    "nested_impl": "src/field/unrelated.cpp",
    "js_helper": "js/coredevice.js",
    "manager_class": "DeviceManager",
    "manager_impl": "src/field/devicemanager.cpp",
    "manager_header": "src/field/devicemanager.h",
    "manager_handler": "DeviceManager::handleEvent",
    "second_manager_class": "SessionManager",
    "second_manager_impl": "src/field/sessionmanager.cpp",
    "second_manager_callee": "SessionManager::refreshSessions",
    # the first derived class; ``derived_impls`` in the return value of ``write_field_shape`` lists all
    "first_derived_class": "WidgetWaitReadyStep",
    "first_derived_impl": "src/field/widgetwaitready.cpp",
    "step_base": "src/field/stepbase.h",
    "step_base_mock": "tests/header-mocks/stepbase.h",
    "step_mocks": [f"tests/header-mocks/steps/step{x}.h" for x in "abc"],
    "build_file": "CMakeLists.txt",
}
CMAKE_EVERY = 7  # every n-th history commit touches CMakeLists.txt in --field-shape repos
FIELD_CONCAT_SHARE = 0.55  # share of all files with a concatenated lowercase stem >= 10 chars
FIELD_TESTED_ONE_IN = 4  # one derived class in this many has a test
FIELD_ANON_NAMESPACE_ONE_IN = 3  # one derived .cpp in this many opens with an anonymous namespace
FIELD_EXTERNAL_SHARE = 0.25  # share of the C++ classes that derive only from an external base (QObject, QThread, testing::Test)
FIELD_EXTERNAL_BASES = 2  # CoreDevice and DeviceManager already derive from QObject
FIELD_CPP_FIXED_CLASSES = 15  # CoreDevice x5 (real, 3 mocks, nested), UnrelatedHolder, ChannelDevice, SessionManager, DeviceManager, StepBase x2, StepA/B/C
FIELD_STEP_MOCKS = 3  # mock classes under tests/header-mocks/steps deriving from the mock StepBase
_PART_A = ("widget", "socket", "device", "channel", "stream", "session", "driver", "bridge", "buffer", "router", "sensor", "filter")
_PART_B = ("wait", "reset", "install", "attach", "detach", "connect", "probe", "flush", "bind", "poll", "sync", "drain")
_PART_C = ("ready", "state", "link", "mode", "queue", "check", "frame", "point", "event", "claim", "scope", "phase")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


WIDGET_HEADER = """#pragma once
#include <QObject>
class {export}Widget{k} : public QObject
{{
    Q_OBJECT
    Q_CLASSINFO("Author", "perf")
    Q_PROPERTY(int value READ value WRITE setValue NOTIFY valueChanged)
public:
    explicit Widget{k}(QObject *parent = nullptr);
    int value() const;
    void setValue(int v);
    Q_INVOKABLE void refresh();
signals:
    void valueChanged();
private:
    int m_value = 0;
}};
"""


def _write_qt(root: Path, modules: int, rng: random.Random) -> None:
    """Qt-style widgets (brace on the next line, some with export macros) plus tests."""
    for sub in ("src/qt", "src/app", "tests/qt", "tests/unit", "tests/integration"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    ks = list(range(0, modules, 4))
    cpps = []
    for index, k in enumerate(ks):
        export = "WIDGETS_EXPORT " if index % 3 == 0 else ""
        (root / "src" / "qt" / f"widget{k}.h").write_text(WIDGET_HEADER.format(k=k, export=export))
        dep = rng.choice(ks)
        include_dep = f'#include "widget{dep}.h"\n' if dep != k else ""
        call = f"    Widget{dep} other;\n    other.refresh();\n" if dep != k else ""
        (root / "src" / "qt" / f"widget{k}.cpp").write_text(
            f'#include "widget{k}.h"\n{include_dep}'
            f"Widget{k}::Widget{k}(QObject *parent) : QObject(parent) {{}}\n"
            f"int Widget{k}::value() const {{ return m_value; }}\n"
            f"void Widget{k}::setValue(int v) {{ m_value = v; emit valueChanged(); }}\n"
            f"void Widget{k}::refresh() {{\n{call}    setValue(m_value + 1);\n}}\n"
        )
        cpps.append(f"src/qt/widget{k}.cpp")
        name = f"tst_widget{k}.cpp" if index % 2 == 0 else f"widget{k}test.cpp"
        (root / "tests" / "qt" / name).write_text(
            f'#include "../../src/qt/widget{k}.h"\nint main_{k}() {{ Widget{k} w; w.refresh(); return w.value(); }}\n'
        )
    for rel in ("src/app/main.cpp", "tests/unit/main.cpp", "tests/integration/main.cpp"):
        (root / rel).write_text("int main() { return 0; }\n")
    (root / "CMakeLists.txt").write_text(
        f"add_library(widgets {' '.join(cpps)})\nadd_executable(app src/app/main.cpp)\n"
    )


def _field_names(count: int) -> list[tuple[str, str]]:
    """``(Class, stem)`` pairs such as ``("WidgetWaitReadyStep", "widgetwaitready")``, first one fixed."""
    names = []
    for a in _PART_A:
        for b in _PART_B:
            for c in _PART_C:
                names.append((f"{a.title()}{b.title()}{c.title()}Step", f"{a}{b}{c}"))
    names.sort(key=lambda pair: (pair[1] != "widgetwaitready", hash_free(pair[1]), pair[1]))
    out = names[:count]
    extra = 0
    while len(out) < count:  # beyond 1728 classes: numbered variants keep the stem concatenated
        cls, stem = names[extra % len(names)]
        out.append((f"{cls[:-4]}{extra}Step", f"{stem}{extra}"))
        extra += 1
    return out


def hash_free(text: str) -> int:
    """Stable (PYTHONHASHSEED-independent) shuffle key."""
    value = 2166136261
    for ch in text:
        value = ((value ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return value


CORE_HEADER = """#pragma once
#include <QObject>
class CoreDevice : public QObject
{{
    Q_OBJECT
public:
    explicit CoreDevice(QObject *parent = nullptr);
    virtual bool isReady() const;
    virtual void reset();
    virtual int waitForReady(int ms);
    QString name() const;
    void setName(const QString &name);
    bool isConnected() const;
    bool hasError() const;
    int id() const;
signals:
    void stateChanged();
private:
    QString m_name;
    bool m_connected = false;
    int m_id = 0;
}};
"""

CORE_IMPL = """#include "coredevice.h"
CoreDevice::CoreDevice(QObject *parent) : QObject(parent) {}
bool CoreDevice::isReady() const { return m_connected; }
void CoreDevice::reset() { m_connected = false; emit stateChanged(); }
int CoreDevice::waitForReady(int ms) { return isReady() ? 0 : ms; }
QString CoreDevice::name() const { return m_name; }
void CoreDevice::setName(const QString &name) { m_name = name; }
bool CoreDevice::isConnected() const { return m_connected; }
bool CoreDevice::hasError() const { return false; }
int CoreDevice::id() const { return m_id; }
"""

MID_HEADER = """#pragma once
#include "coredevice.h"
class ChannelDevice : public CoreDevice
{{
    Q_OBJECT
public:
    explicit ChannelDevice(QObject *parent = nullptr);
    bool isReady() const override;
}};
"""

MID_IMPL = """#include "channeldevice.h"
ChannelDevice::ChannelDevice(QObject *parent) : CoreDevice(parent) {}
bool ChannelDevice::isReady() const { return CoreDevice::isReady() && !hasError(); }
"""

DERIVED_HEADER = """#pragma once
#include "{base_header}"
class {cls} : public {base}
{{
    Q_OBJECT
public:
    explicit {cls}(QObject *parent = nullptr);
    bool isReady() const override;
    void reset() override;
    int waitForReady(int ms) override;
private:
    int m_step = 0;
}};
"""

DERIVED_IMPL = """#include "{stem}.h"
{anon}{cls}::{cls}(QObject *parent) : {base}(parent) {{}}
bool {cls}::isReady() const {{ return m_step > 0 && {base}::isReady(); }}
void {cls}::reset() {{ m_step = 0; {base}::reset(); }}
int {cls}::waitForReady(int ms) {{ m_step += 1; return ms - m_step; }}
"""

# An anonymous namespace opening with a Qt-typed helper: the tree-sitter extractor of 0.13.0
# labelled such a namespace after its first identifier (`class QString`).
ANON_NAMESPACE = """namespace {{
QString describe_{stem}(int) {{ return {{}}; }}
}}
"""

# Classes whose only base is outside the repository (about FIELD_EXTERNAL_SHARE of the C++ classes): the Qt
# classes of the field report's `name:` bucket (QObject 406, QThread 36) and its gtest fixtures (57, written
# `testing::Test` or, under `using namespace testing`, a bare `Test`).
EXTERNAL_HEADER = """#pragma once
#include <{include}>
class {cls} : public {base}
{{
    Q_OBJECT
public:
    explicit {cls}({base} *parent = nullptr);
}};
"""

EXTERNAL_FIXTURE = """#include <gtest/gtest.h>
{using}class {cls} : public {base}
{{
protected:
    void SetUp() override {{}}
}};
"""

MANAGER_HEADER = """#pragma once
#include "coredevice.h"
#include <QList>
class DeviceManager : public QObject
{{
    Q_OBJECT
public:
    void handleEvent(int code);
    void onReattached(CoreDevice *dev);
    void pollAll();
{chunk_decls}private:
    CoreDevice *m_primary = nullptr;
    QList<CoreDevice *> m_devices;
}};
"""

SECOND_HEADER = """#pragma once
class SessionManager
{
public:
    static int refreshSessions(int code);
    static int sessionCount();
};
"""

SECOND_IMPL = """#include "sessionmanager.h"
int SessionManager::sessionCount() { return 1; }
int SessionManager::refreshSessions(int code)
{
    int total = sessionCount();
    total += code;
    return total;
}
"""

NESTED_IMPL = """struct UnrelatedHolder
{
    class CoreDevice
    {
    public:
        int ping() const { return 1; }
    };
};
int unrelatedPing() { UnrelatedHolder::CoreDevice d; return d.ping(); }
"""

STEP_BASE_HEADER = """#pragma once
class StepBase
{
public:
    virtual ~StepBase() = default;
    virtual int run() = 0;
};
"""

STEP_MOCK_HEADER = """#pragma once
#include <stepbase.h>
class Step{x} : public StepBase
{{
public:
    int run() override {{ return {value}; }}
}};
"""

MOCK_HEADER = """#pragma once
class CoreDevice
{{
public:
    virtual bool isReady() const {{ return {value}; }}
    virtual void reset() {{}}
    virtual int waitForReady(int) {{ return 0; }}
}};
"""

JS_HELPER = """class CoreDevice {
    isReady() { return true; }
    reset() {}
}
module.exports = { CoreDevice };
"""


def _repo_files(root: Path) -> list[Path]:
    """The working-tree files, never descending into `.git`: git may pack and delete
    loose-object directories in the background while a walk is inside them."""
    found = []
    for directory, subdirs, names in os.walk(root):
        subdirs[:] = [name for name in subdirs if name != ".git"]
        found.extend(Path(directory) / name for name in names)
    return found


def write_field_shape(root: Path, modules: int, rng: random.Random) -> list[str]:
    """Write the ``--field-shape`` files; return the derived classes' ``.cpp`` paths."""
    for sub in ("src/field", "tests/field", "js", "tests/header-mocks/steps", *(f"tests/mocks/mock_{x}" for x in "abc")):
        (root / sub).mkdir(parents=True, exist_ok=True)
    existing = sum(1 for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
    # n derived classes -> 2n+fixed new files, all with concatenated stems except the fixed ones;
    # pick n so that the concatenated share of the whole repository reaches FIELD_CONCAT_SHARE.
    # QML views add `inherits -> module:Item` edges (the extractor's target for QML roots) so that
    # about 36% of all `inherits` targets are `module:`: roughly 0.5625 per in-repo C++ `name:` edge.
    fixed = 22  # 17 + the StepBase pair and the three step mocks
    need = (FIELD_CONCAT_SHARE * (existing + fixed)) / (1 - FIELD_CONCAT_SHARE)
    derived_count = max(12, int((need - 0.5625 * modules) / 2.5625) + 1)
    names = _field_names(derived_count)
    qml_count = int(0.5625 * (modules + derived_count + FIELD_STEP_MOCKS)) + 1
    field = root / "src" / "field"

    (field / "coredevice.h").write_text(CORE_HEADER.format())
    (field / "coredevice.cpp").write_text(CORE_IMPL)
    (field / "channeldevice.h").write_text(MID_HEADER.format())
    (field / "channeldevice.cpp").write_text(MID_IMPL)
    for x, value in zip("abc", ("true", "false", "true")):
        (root / "tests" / "mocks" / f"mock_{x}" / "coredevice.h").write_text(MOCK_HEADER.format(value=value))
    (field / "unrelated.cpp").write_text(NESTED_IMPL)
    (root / "js" / "coredevice.js").write_text(JS_HELPER)
    (field / "sessionmanager.h").write_text(SECOND_HEADER)
    (field / "sessionmanager.cpp").write_text(SECOND_IMPL)
    (field / "stepbase.h").write_text(STEP_BASE_HEADER)
    (root / "tests" / "header-mocks" / "stepbase.h").write_text(STEP_BASE_HEADER)
    for x, value in zip("abc", (1, 2, 3)):
        (root / "tests" / "header-mocks" / "steps" / f"step{x}.h").write_text(STEP_MOCK_HEADER.format(x=x.upper(), value=value))

    derived_cpps = []
    for index, (cls, stem) in enumerate(names):
        base, base_header = ("ChannelDevice", "channeldevice.h") if index % 4 == 3 else ("CoreDevice", "coredevice.h")
        (field / f"{stem}.h").write_text(DERIVED_HEADER.format(cls=cls, base=base, base_header=base_header))
        # index % 3 == 1: the first derived file (the one the query tests target) stays plain
        anon = ANON_NAMESPACE.format(stem=stem) if index % FIELD_ANON_NAMESPACE_ONE_IN == 1 else ""
        (field / f"{stem}.cpp").write_text(DERIVED_IMPL.format(cls=cls, base=base, stem=stem, anon=anon))
        derived_cpps.append(f"src/field/{stem}.cpp")
        if index % FIELD_TESTED_ONE_IN == 0:
            (root / "tests" / "field" / f"tst_{stem}.cpp").write_text(
                f'#include "../../src/field/{stem}.h"\nint run_{stem}() {{ {cls} d; d.reset(); return d.waitForReady(1); }}\n'
            )

    # Classes with an external base only.  Names come from the tail of the name pool, so none of them
    # collides with a derived class; nothing calls them, so the manager and the CMake list are untouched.
    cpp_classes = BASES + modules + FIELD_CPP_FIXED_CLASSES + derived_count
    external_count = max(1, round((FIELD_EXTERNAL_SHARE * cpp_classes - FIELD_EXTERNAL_BASES) / (1 - FIELD_EXTERNAL_SHARE)))
    for index, (pool_cls, stem) in enumerate(_field_names(derived_count + external_count)[derived_count:]):
        stem_cls = pool_cls[: -len("Step")]
        kind = index % 10
        if kind < 7:  # QObject
            (field / f"{stem}.h").write_text(EXTERNAL_HEADER.format(include="QObject", cls=f"{stem_cls}Worker", base="QObject"))
        elif kind == 7:  # QThread
            (field / f"{stem}.h").write_text(EXTERNAL_HEADER.format(include="QThread", cls=f"{stem_cls}Thread", base="QThread"))
        else:  # gtest fixtures: `testing::Test` as written, or a bare `Test` under `using namespace testing`
            qualified = kind == 8
            (root / "tests" / "field" / f"{stem}.cpp").write_text(EXTERNAL_FIXTURE.format(
                using="" if qualified else "using namespace testing;\n", cls=f"{stem_cls}Fixture",
                base="::testing::Test" if qualified else "Test",
            ))

    (root / "qml").mkdir(exist_ok=True)
    for q in range(qml_count):
        stem = names[q % len(names)][1] + ("view" if q < len(names) else f"panel{q}")
        (root / "qml" / f"{stem}.qml").write_text(f"import QtQuick 2.15\nItem {{\n    id: root{q}\n}}\n")

    # The central manager: most out-calls go to accessors, through a Base* or a cast local.
    chunk = 40
    chunks = max(1, (len(names) + chunk - 1) // chunk)
    includes = "".join(f'#include "{stem}.h"\n' for _cls, stem in names[: min(len(names), 400)])
    bodies = [
        (
            "void DeviceManager::handleEvent(int code)\n{\n"
            "    SessionManager::refreshSessions(code);\n"
            "    onReattached(m_primary);\n"
            "    pollAll();\n}\n"
        ),
        (
            "void DeviceManager::onReattached(CoreDevice *dev)\n{\n"
            "    if (!dev->isReady()) {\n        dev->reset();\n    }\n"
            "    dev->waitForReady(30);\n}\n"
        ),
        "void DeviceManager::pollAll()\n{\n"
        + "".join(f"    pollChunk{j}();\n" for j in range(chunks))
        + "}\n",
    ]
    accessors = ("name()", "isConnected()", "hasError()", "id()", 'setName("x")', "isReady()")
    for j in range(chunks):
        lines = [f"void DeviceManager::pollChunk{j}()\n{{\n"]
        for i in range(j * chunk, min(len(names), (j + 1) * chunk)):
            cls = names[i][0]
            lines.append(f"    auto *d{i} = qobject_cast<{cls}*>(m_devices.value({i}));\n")
            picks = rng.sample(accessors, 3)
            lines.append(f"    if (d{i}) {{ " + " ".join(f"d{i}->{call};" for call in picks) + " }\n")
            lines.append(f"    m_primary->{rng.choice(accessors)};\n")
        lines.append("}\n")
        bodies.append("".join(lines))
    chunk_decls = "".join(f"    void pollChunk{j}();\n" for j in range(chunks))
    (field / "devicemanager.h").write_text(MANAGER_HEADER.format(chunk_decls=chunk_decls))
    (field / "devicemanager.cpp").write_text(
        '#include "devicemanager.h"\n#include "sessionmanager.h"\n' + includes + "".join(bodies)
    )

    cmake = root / "CMakeLists.txt"
    lines = [f"target_sources(field PRIVATE {' '.join(derived_cpps)} src/field/devicemanager.cpp)\n"]
    cmake.write_text((cmake.read_text() if cmake.exists() else "add_library(field)\n") + "".join(lines))
    return derived_cpps


def generate(
    root: Path, modules: int, commits: int, seed: int = 7, qt: bool = False, field_shape: bool = False
) -> int:
    rng = random.Random(seed)
    if root.exists():
        raise SystemExit(f"{root} already exists; pass a new directory")
    for sub in ("src", "py", "tests"):
        (root / sub).mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "perf@example.invalid")
    _git(root, "config", "user.name", "perf")

    for b in range(BASES):
        hooks = "".join(f"  virtual int hook{m}(int x);\n" for m in range(HOOKS))
        (root / "src" / f"base{b}.hpp").write_text(f"#pragma once\nclass Base{b} {{\npublic:\n{hooks}}};\n")
        bodies = "".join(f"int Base{b}::hook{m}(int x) {{ return x + {m}; }}\n" for m in range(HOOKS))
        (root / "src" / f"base{b}.cpp").write_text(f'#include "base{b}.hpp"\n{bodies}')

    for k in range(modules):
        deps = rng.sample(range(modules), 4)
        forward = "".join(f"class Mod{d};\n" for d in deps)
        methods = "".join(f"  int m{m}(int x);\n" for m in range(METHODS))
        members = "".join(f"  Mod{d}* dep{i};\n" for i, d in enumerate(deps))
        (root / "src" / f"mod{k}.hpp").write_text(
            f'#pragma once\n#include "base{k % BASES}.hpp"\n{forward}'
            f"class Mod{k} : public Base{k % BASES} {{\npublic:\n{methods}  int hook0(int x) override;\n{members}}};\n"
        )
        includes = "".join(f'#include "mod{d}.hpp"\n' for d in deps)
        bodies = []
        for m in range(METHODS):
            calls = " + ".join(f"dep{rng.randrange(4)}->m{rng.randrange(METHODS)}(x)" for _ in range(3))
            bodies.append(f"int Mod{k}::m{m}(int x) {{ int s = helper{k}(x); return s + {calls} + hook1(x); }}\n")
        (root / "src" / f"mod{k}.cpp").write_text(
            f'#include "mod{k}.hpp"\n{includes}static int helper{k}(int x) {{ return x * 2; }}\n'
            f"int Mod{k}::hook0(int x) {{ return m0(x); }}\n" + "".join(bodies)
        )
        if k % 3 == 0:
            others = rng.sample(range(0, modules, 3), 3)
            imports = "".join(f"from py.pmod{o} import pfunc{o}_0\n" for o in others)
            funcs = "".join(
                f"def pfunc{k}_{f}(x):\n    return " + " + ".join(f"pfunc{o}_0(x)" for o in others) + f" + {f}\n\n"
                for f in range(8)
            )
            (root / "py" / f"pmod{k}.py").write_text(f"{imports}\n{funcs}")
            (root / "tests" / f"test_pmod{k}.py").write_text(
                f"from py.pmod{k} import pfunc{k}_1\n\n\ndef test_it():\n    assert pfunc{k}_1(1)\n"
            )
    (root / "py" / "__init__.py").write_text("")
    if qt:
        # Uses its own generator so the default output stays byte-identical.
        _write_qt(root, modules, random.Random(seed + 1))
    if field_shape:
        write_field_shape(root, modules, random.Random(seed + 2))
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")

    files = sorted(_repo_files(root))
    if field_shape:
        # Own history, so the default one (and its rng stream) stays byte-identical.  Every
        # CMAKE_EVERY-th commit touches CMakeLists.txt, so a 100-commit range always does.
        # Test files are left out of the churn: on a small repository a test that changed
        # together with a source file even once would otherwise cover it through co-change.
        history = random.Random(seed + 3)
        churn = [p for p in files if "tests" not in p.relative_to(root).parts]
        for c in range(commits):
            picked = history.sample(churn, 6)
            if c % CMAKE_EVERY == CMAKE_EVERY - 1:
                picked[0] = root / "CMakeLists.txt"
            for path in picked:
                marker = "#" if path.suffix not in {".cpp", ".hpp", ".h", ".js"} else "//"
                path.write_text(path.read_text() + f"{marker} c{c}\n")
            _git(root, "commit", "-qam", f"change {c}")
        return len(files)
    for c in range(commits):
        for path in rng.sample(files, 6):
            marker = "//" if path.suffix in {".cpp", ".hpp"} else "#"
            path.write_text(path.read_text() + f"{marker} c{c}\n")
        _git(root, "commit", "-qam", f"change {c}")
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--modules", type=int, default=800)
    parser.add_argument("--commits", type=int, default=60)
    parser.add_argument("--qt", action="store_true", help="also write Qt widgets, tests and a CMakeLists.txt")
    parser.add_argument("--field-shape", action="store_true", help="add the 0.12.1 field-report statistics (see module docstring)")
    args = parser.parse_args()
    count = generate(args.root.resolve(), args.modules, args.commits, qt=args.qt, field_shape=args.field_shape)
    print(f"{count} files written to {args.root}")


if __name__ == "__main__":
    main()
