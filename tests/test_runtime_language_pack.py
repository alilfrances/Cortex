from __future__ import annotations

import hashlib
import sys
import textwrap
import tomllib
import types
from pathlib import Path

import pytest

from cortex import runtime

REPO = Path(__file__).resolve().parents[1]
REGENERATE = "run `python scripts/update_runtime_lock.py --language-pack <version>`"


def _pyproject_pins() -> dict[str, str]:
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = {}
    for requirement in project["dependencies"]:
        name, _, version = requirement.partition("==")
        pins[name.strip()] = version.strip()
    return pins


def test_pyproject_pins_match_runtime_lock():
    # Dependabot bumps pyproject.toml only. The managed runtime installs the
    # wheels and parser archive named by the lock, so a bump that leaves the
    # lock behind splits the install across two language-pack versions.
    pins = _pyproject_pins()
    lock = runtime._load_lock()
    assert pins["tree-sitter"] == lock["tree_sitter_version"], REGENERATE
    assert pins["tree-sitter-language-pack"] == lock["language_pack_version"], REGENERATE
    locked = {package["name"]: package["version"] for package in lock["packages"]}
    assert locked == {
        "tree-sitter": pins["tree-sitter"],
        "tree-sitter-language-pack": pins["tree-sitter-language-pack"],
    }, REGENERATE
    version = lock["language_pack_version"]
    assert lock["parser_manifest"]["source"].endswith(f"/v{version}"), REGENERATE
    for entry in lock["parser_manifest"]["platforms"].values():
        assert f"/download/v{version}/" in entry["url"], REGENERATE


BUNDLE = b"fake parser bundle"
BUNDLE_SHA = hashlib.sha256(BUNDLE).hexdigest()


def _write_fake_pack(site: Path, version: str, layout: str) -> None:
    package = site / "tree_sitter_language_pack"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(textwrap.dedent(f"""
        from pathlib import Path

        __version__ = {version!r}
        _cache = None


        class PackConfig:
            def __init__(self, cache_dir, languages):
                self.cache_dir = cache_dir


        def configure(config):
            global _cache
            _cache = Path(config.cache_dir)


        def _state_dir():
            if {layout!r} == "1.12":
                return _cache.parent, _cache
            versioned = _cache / "tree-sitter-language-pack" / "v{version}"
            return versioned, versioned / "libs"


        def prefetch(names):
            state, libs = _state_dir()
            (state / "bundles").mkdir(parents=True, exist_ok=True)
            libs.mkdir(parents=True, exist_ok=True)
            (state / "bundles" / "linux-x86_64-{BUNDLE_SHA}.tar.zst").write_bytes({BUNDLE!r})
            (state / "manifest.json").write_text("{{}}")
            (state / ".download.lock").write_text("")
            for name in names:
                (libs / f"libtree_sitter_{{name}}.so").write_bytes(b"lib")


        def get_language(name):
            _, libs = _state_dir()
            if not (libs / f"libtree_sitter_{{name}}.so").is_file():
                raise RuntimeError(f"missing {{name}}")
            return name
    """), encoding="utf-8")


@pytest.fixture
def isolated_imports(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tree_sitter_language_pack", raising=False)
    monkeypatch.delenv("CORTEX_PARSER_CACHE", raising=False)
    monkeypatch.delenv("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL", raising=False)
    monkeypatch.setattr(runtime, "_platform_key", lambda: "linux-glibc-x86_64")
    yield
    sys.modules.pop("tree_sitter_language_pack", None)


@pytest.mark.parametrize(("version", "layout"), [("1.12.5", "1.12"), ("1.22.1", "1.2x")])
def test_prefetch_verifies_and_strips_setup_state_for_each_cache_layout(tmp_path, isolated_imports, version, layout):
    site = tmp_path / "stage" / "site"
    cache = tmp_path / "stage" / "parser-cache"
    cache.mkdir(parents=True)
    _write_fake_pack(site, version, layout)
    lock = {
        "language_pack_version": version,
        "parser_manifest": {
            "platforms": {"linux-glibc-x86_64": {"sha256": BUNDLE_SHA, "size": len(BUNDLE)}},
        },
    }

    loaded = runtime._prefetch_and_verify(site, cache, ["c", "qml"], offline=False, lock=lock)

    assert loaded == ["c", "qml"]
    leftovers = sorted(
        str(path.relative_to(tmp_path)) for path in (tmp_path / "stage").rglob("*")
        if path.name in {"bundles", ".download.lock"}
        or (path.name == "manifest.json" and path != cache / "manifest.json")
    )
    assert leftovers == []
    assert {path.name for path in cache.rglob("*.so")} == {"libtree_sitter_c.so", "libtree_sitter_qmljs.so"}


def test_prefetch_rejects_bundle_with_wrong_digest(tmp_path, isolated_imports):
    site = tmp_path / "stage" / "site"
    cache = tmp_path / "stage" / "parser-cache"
    cache.mkdir(parents=True)
    _write_fake_pack(site, "1.22.1", "1.2x")
    lock = {
        "language_pack_version": "1.22.1",
        "parser_manifest": {"platforms": {"linux-glibc-x86_64": {"sha256": "0" * 64}}},
    }

    with pytest.raises(ValueError, match="did not retain the locked parser bundle"):
        runtime._prefetch_and_verify(site, cache, ["c"], offline=False, lock=lock)


def _imported_pack(monkeypatch, version: str) -> types.SimpleNamespace:
    configured = []
    module = types.SimpleNamespace(
        __version__=version,
        PackConfig=lambda cache_dir, languages: cache_dir,
        configure=configured.append,
        configured=configured,
    )
    monkeypatch.setitem(sys.modules, "tree_sitter_language_pack", module)
    return module


def test_language_pack_rejects_version_skew(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_PARSER_CACHE", str(tmp_path))
    _imported_pack(monkeypatch, "0.0.1")

    with pytest.raises(runtime.LanguagePackVersionSkew, match="0.0.1 is imported"):
        runtime.language_pack()


def test_language_pack_configures_locked_version_for_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_PARSER_CACHE", str(tmp_path))
    module = _imported_pack(monkeypatch, runtime._load_lock()["language_pack_version"])

    assert runtime.language_pack() is module
    assert module.configured == [str(tmp_path)]


def test_status_reports_version_skew_of_imported_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    _imported_pack(monkeypatch, "0.0.1")

    status = runtime.status()

    assert status["ready"] is False
    assert status["degraded_reason"] == "language-pack-version-skew"
