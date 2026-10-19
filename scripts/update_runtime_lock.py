#!/usr/bin/env python3
"""Regenerate the managed parser runtime lock for a new language-pack release.

The language-pack version is pinned in three places that must agree:
``pyproject.toml`` (site-packages installs) and the two identical copies of
``runtime-lock.json`` (the managed runtime). Dependabot only edits the first.
This script rewrites the lock entries from PyPI and the GitHub release:

    python scripts/update_runtime_lock.py --language-pack 1.22.1 --runtime-version 0.9.0

Every artifact is downloaded and hashed locally rather than trusting published
digests. Only the standard library is used, like ``cortex.runtime``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cortex import runtime  # noqa: E402

LOCK_PATHS = (ROOT / "src" / "cortex" / "runtime-lock.json", ROOT / "runtime" / "runtime-lock.json")
PYPROJECT = ROOT / "pyproject.toml"
RUNTIME_MODULE = ROOT / "src" / "cortex" / "runtime.py"
RELEASES = "https://github.com/xberg-io/tree-sitter-language-pack/releases"
LANGUAGE_PACK = "tree-sitter-language-pack"
TREE_SITTER = "tree-sitter"

# Wheel platform tag suffix -> runtime platform key.
WHEEL_PLATFORMS = (
    (re.compile(r"^macosx_\d+_\d+_x86_64$"), "macos-x86_64"),
    (re.compile(r"^macosx_\d+_\d+_arm64$"), "macos-arm64"),
    (re.compile(r"^manylinux.*_x86_64$"), "linux-glibc-x86_64"),
    (re.compile(r"^manylinux.*_aarch64$"), "linux-glibc-arm64"),
    (re.compile(r"^win_amd64$"), "windows-x86_64"),
    (re.compile(r"^win_arm64$"), "windows-arm64"),
)


def _wheel_platform(platform_tag: str) -> str | None:
    # A compressed tag set such as manylinux2014_x86_64.manylinux_2_17_x86_64
    # names one platform; any member is enough to classify it.
    for tag in platform_tag.split("."):
        for pattern, key in WHEEL_PLATFORMS:
            if pattern.match(tag):
                return key
    return None


def _fetch(url: str, directory: Path) -> Path:
    destination = directory / hashlib.sha256(url.encode()).hexdigest()
    runtime._download(url, destination, timeout=120.0)
    return destination


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_artifacts(name: str, version: str, platforms: set[str], python_tags: set[str], work: Path) -> list[dict]:
    release = json.loads(_fetch(f"https://pypi.org/pypi/{name}/{version}/json", work).read_text(encoding="utf-8"))
    artifacts = []
    for file in release["urls"]:
        filename = file["filename"]
        if file.get("packagetype") != "bdist_wheel":
            continue
        py_tag, abi_tag, platform_tag = runtime._wheel_tags(filename)
        platform = _wheel_platform(platform_tag)
        # CPython wheels only: an exact cpXY ABI or the stable abi3. This skips
        # PyPy and free-threaded (cp313t) builds the runtime cannot load.
        if platform not in platforms or py_tag not in python_tags or abi_tag not in {py_tag, "abi3"}:
            continue
        path = _fetch(file["url"], work)
        size = path.stat().st_size
        sha256 = _digest(path)
        if sha256 != file["digests"]["sha256"]:
            raise SystemExit(f"{filename}: downloaded digest does not match PyPI")
        path.unlink()
        artifacts.append({
            "filename": filename,
            "platform": platform,
            "sha256": sha256,
            "size": size,
            "size_bytes": size,
            "tags": [py_tag, abi_tag, platform_tag],
            "url": file["url"],
        })
    missing = platforms - {artifact["platform"] for artifact in artifacts}
    if missing:
        raise SystemExit(f"{name} {version} has no wheel for: {', '.join(sorted(missing))}")
    return sorted(artifacts, key=lambda artifact: artifact["filename"])


def _parser_platforms(version: str, platforms: set[str], work: Path) -> dict[str, dict]:
    entries = {}
    for platform in sorted(platforms):
        asset = runtime.PARSER_PLATFORM_ALIASES.get(platform, platform)
        url = f"{RELEASES}/download/v{version}/parsers-{asset}.tar.zst"
        path = _fetch(url, work)
        entries[platform] = {"sha256": _digest(path), "size": path.stat().st_size, "url": url}
        path.unlink()
    return entries


def _replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path.name}: expected one match for {pattern!r}")
    path.write_text(updated, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--language-pack", required=True, metavar="VERSION")
    parser.add_argument("--tree-sitter", metavar="VERSION", help="also re-lock tree-sitter at VERSION")
    parser.add_argument("--runtime-version", metavar="VERSION",
                        help="new runtime directory version; bump it whenever the parser cache layout can change")
    args = parser.parse_args(argv)

    lock = json.loads(LOCK_PATHS[0].read_text(encoding="utf-8"))
    platforms = set(lock["parser_manifest"]["platforms"])
    packages = {package["name"]: package for package in lock["packages"]}
    updates = {LANGUAGE_PACK: args.language_pack}
    if args.tree_sitter:
        updates[TREE_SITTER] = args.tree_sitter

    with tempfile.TemporaryDirectory(prefix="cortex-lock-") as tmp:
        work = Path(tmp)
        for name, version in updates.items():
            # Keep the CPython tags the lock already supports for this package.
            python_tags = {artifact["tags"][0].split("-")[0] for artifact in packages[name]["artifacts"]}
            packages[name]["artifacts"] = _wheel_artifacts(name, version, platforms, python_tags, work)
            packages[name]["version"] = version
            print(f"{name} {version}: {len(packages[name]['artifacts'])} wheels")
        lock["parser_manifest"]["platforms"] = _parser_platforms(args.language_pack, platforms, work)
        print(f"parser archives v{args.language_pack}: {len(platforms)} platforms")

    lock["language_pack_version"] = args.language_pack
    lock["parser_manifest"]["source"] = f"{RELEASES}/tag/v{args.language_pack}"
    if args.tree_sitter:
        lock["tree_sitter_version"] = args.tree_sitter
    if args.runtime_version:
        lock["runtime_version"] = args.runtime_version
        _replace_once(RUNTIME_MODULE, r'^RUNTIME_VERSION = "[^"]+"$', f'RUNTIME_VERSION = "{args.runtime_version}"')

    payload = json.dumps(lock, indent=2, sort_keys=True) + "\n"
    for path in LOCK_PATHS:
        path.write_text(payload, encoding="utf-8")
    for name, version in updates.items():
        _replace_once(PYPROJECT, rf'^(\s*"{re.escape(name)})==[^"]+(",?)$', rf"\g<1>=={version}\g<2>")
    print("updated " + ", ".join(str(path.relative_to(ROOT)) for path in (*LOCK_PATHS, PYPROJECT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
