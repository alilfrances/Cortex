"""Small C++ repositories with the shapes that broke `cortex_query` in the field (0.12.1 review, Issue B).

Every builder returns the repository root.  Class labels are duplicated in a mock tree and a JS helper,
and bases are written the way the extractor emits them (`name:Base`), so the repositories fail on a
resolver that only follows `symbol:` targets.  The caller's ``CORTEX_DATA_DIR`` receives the index.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from cortex.ingest import ingest_repository


def make_repo(root: Path, files: dict[str, str], *, ingest: bool = True) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"], cwd=root, check=True
    )
    if ingest:
        ingest_repository(root)
    return root


def _noise_files(count: int, words: str) -> dict[str, str]:
    """Files that share a few task words in comments and carry a long body, so they compete for budget."""
    body = "\n".join(f"    int pad{i} = value + {i};" for i in range(40))
    return {
        f"src/noise_{k}.cpp": f"// {words}\nint noise_{k}(int value) {{\n{body}\n    return value;\n}}\n"
        for k in range(count)
    }


# --- B.2: the fix is in an override reached through a Base* ---------------------------------------

OVERRIDE_TASK = "device card state wrong after reconnect"


def override_files() -> dict[str, str]:
    files = {
        # The manager talks about the task in its own words and calls `apply` through a Base*.
        "src/manager.cpp": (
            '#include "base.h"\n'
            "// device card state wrong after reconnect: the manager restores the card state on reconnect\n"
            "void Manager::reconnect(Base* target) {\n"
            "    target->apply(1);\n"
            "}\n"
        ),
        "src/base.h": "class Base {\npublic:\n    virtual void apply(int v);\nprotected:\n    int slot_ = 0;\n};\n",
        "src/base.cpp": '#include "base.h"\nvoid Base::apply(int v) { slot_ = v; }\n',
        # The behaviour lives in the override; its text shares no word with the task.
        "src/derived.h": '#include "base.h"\nclass Derived : public Base {\npublic:\n    void apply(int v) override;\n};\n',
        "src/derived.cpp": (
            '#include "derived.h"\nvoid Derived::apply(int v) {\n    slot_ = v + 1;\n    if (slot_ > 9) {\n        slot_ = 0;\n    }\n}\n'
        ),
        # Same class label in a mock tree and a JS helper, as in the field.
        "tests/mocks/base.h": "class Base {\npublic:\n    virtual void apply(int v) {}\n};\n",
        "js/base.js": "class Base {\n    apply(v) { return v; }\n}\n",
        "src/other.cpp": "void unrelated() { int x = 1; }\n",
    }
    files.update(_noise_files(24, "device state card"))
    return files


def build_override_repo(root: Path) -> Path:
    return make_repo(root, override_files())


# --- B.3: a handler without a task word in its name calls into a second file ----------------------

HANDLER_TASK = "peripheral reattached then demoted and removed"


def handler_files() -> dict[str, str]:
    # Six better-scoring spans than the handler: three tiny factories with task words in their names and
    # two class nodes (one per header) -- the shapes that take the seed slots in a real Qt repository.
    factories = "".join(
        f"Peripheral* {name}() {{ return new Peripheral(); }}\n"
        for name in ("makeReattachedPeripheral", "makeRemovedEntry", "makeDemotedPeripheralEntry", "makePeripheralRemoved")
    )
    files = {
        "src/hub.h": (
            "// peripheral reattached, demoted and removed: the hub tracks every peripheral entry\n"
            "class PeripheralHub {\npublic:\n    void dispatch(int code);\n    void onReattached(int code);\n"
            "    int value_ = 0;\n};\n"
        ),
        "src/entry.h": (
            "// peripheral entry removed or demoted after it was reattached\n"
            "class PeripheralEntry {\npublic:\n    int removed_ = 0;\n    int demoted_ = 0;\n};\n"
        ),
        # `dispatch` has no task word in its name; its body (the call to onReattached) does.
        "src/hub.cpp": (
            '#include "hub.h"\n#include "registry_store.h"\n'
            + factories
            + "void PeripheralHub::dispatch(int code) {\n"
            "    RegistryStore::compact(code);\n"
            "    onReattached(code);\n"
            "}\n"
            "void PeripheralHub::onReattached(int code) {\n"
            "    value_ = code;\n"
            "}\n"
        ),
        "src/registry_store.h": "class RegistryStore {\npublic:\n    static void compact(int code);\n};\n",
        "src/registry_store.cpp": (
            '#include "registry_store.h"\nvoid RegistryStore::compact(int code) {\n'
            "    int kept = code;\n    kept += 1;\n    kept *= 2;\n    kept -= 3;\n}\n"
        ),
        "src/other.cpp": "void unrelated() { int x = 1; }\n",
    }
    files.update(_noise_files(24, "peripheral reattached"))
    return files


def build_handler_repo(root: Path) -> Path:
    return make_repo(root, handler_files())


# --- B.3: ten accessors are called before the callee that matters ---------------------------------

ACCESSOR_TASK = "device session refresh fails after reconnect"


def accessor_files() -> dict[str, str]:
    names = [f"{prefix}{label}" for prefix, label in zip(("get", "is", "has", "set") * 3, "ABCDEFGHIJKL")][:10]
    files = {
        "src/session.h": "class Session {\npublic:\n    void refresh(int v);\n};\n",
        "src/session.cpp": (
            '#include "session.h"\n#include "props.h"\n'
            "// device session refresh fails after reconnect\n"
            "void Session::refresh(int v) {\n"
            + "".join(f"    Props::{name}();\n" for name in names)
            + "    Engine::rebuild(v);\n}\n"
        ),
        "src/engine.h": "class Engine {\npublic:\n    static void rebuild(int v);\n};\n",
        "src/engine.cpp": (
            '#include "engine.h"\nvoid Engine::rebuild(int v) {\n'
            "    int state = v;\n    state += 1;\n    state *= 2;\n    state -= 3;\n}\n"
        ),
        "src/props.h": "class Props {\npublic:\n" + "".join(f"    static int {name}();\n" for name in names) + "};\n",
    }
    for index, name in enumerate(names):
        files[f"src/props_{index}.cpp"] = f'#include "props.h"\nint Props::{name}() {{ return {index}; }}\n'
    files.update(_noise_files(24, "device session"))
    return files


def build_accessor_repo(root: Path) -> Path:
    return make_repo(root, accessor_files())
