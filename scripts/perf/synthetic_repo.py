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
"""
from __future__ import annotations

import argparse
import random
import subprocess
from pathlib import Path

BASES = 10
METHODS = 12
HOOKS = 6


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


def generate(root: Path, modules: int, commits: int, seed: int = 7, qt: bool = False) -> int:
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
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")

    files = sorted(p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts)
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
    args = parser.parse_args()
    print(f"{generate(args.root.resolve(), args.modules, args.commits, qt=args.qt)} files written to {args.root}")


if __name__ == "__main__":
    main()
