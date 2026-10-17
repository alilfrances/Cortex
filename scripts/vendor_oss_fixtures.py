#!/usr/bin/env python3
"""Acquire and prune the vendored OSS eval snapshots under ``evals/fixtures/oss/``.

This is the *only* Cortex script that touches the network, and only when run by a
maintainer to (re)create a snapshot.  The eval harness (``evals/run_evals.py``)
reads the vendored copies and never fetches anything.

Every fixture is pinned to an immutable upstream commit SHA.  ``--verify`` re-reads
the vendored tree offline and checks it against ``PROVENANCE.md``-recorded counts
(it never contacts upstream).

Usage::

    python3 scripts/vendor_oss_fixtures.py --fetch            # clone + prune + write (network)
    python3 scripts/vendor_oss_fixtures.py --fetch --only go_mux
    python3 scripts/vendor_oss_fixtures.py --verify           # offline consistency check
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OSS_ROOT = ROOT / "evals" / "fixtures" / "oss"
MAX_FILES = 300
MAX_TOTAL_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Snapshot:
    name: str
    family: str
    url: str
    commit: str
    commit_date: str
    license_id: str
    license_file: str  # upstream path copied verbatim to <fixture>/LICENSE
    include: tuple[str, ...]  # fnmatch patterns over upstream repo-relative paths
    exclude: tuple[str, ...] = ()
    extra_license_files: tuple[str, ...] = ()  # kept verbatim under their upstream names
    notes: str = ""


SNAPSHOTS: tuple[Snapshot, ...] = (
    Snapshot(
        name="cpp_qt_sortfilterproxymodel",
        family="C/C++ with Qt",
        url="https://github.com/oKcerG/SortFilterProxyModel",
        commit="5a930885b7ea99f7f41c25fce08bf8006ee54e3f",
        commit_date="2021-05-03T22:51:37+02:00",
        license_id="MIT",
        license_file="LICENSE",
        include=(
            "CMakeLists.txt",
            "README.md",
            "SortFilterProxyModel.pri",
            "SortFilterProxyModel.qbs",
            "qqmlsortfilterproxymodel.*",
            "filters/*",
            "sorters/*",
            "proxyroles/*",
            "tests/*",
        ),
        notes="Qt 5 QML plugin: QAbstractProxyModel/QObject subclasses with Q_PROPERTY, signals, slots, "
        "qmlRegisterType, CMake + qmake/qbs build files, and Qt Quick Test QML tests.",
    ),
    Snapshot(
        name="ts_p_queue",
        family="JavaScript/TypeScript",
        url="https://github.com/sindresorhus/p-queue",
        commit="7f272ab5ff2254c0dc2537b7977e88ede1cd55e8",
        commit_date="2026-10-13T00:56:40+07:00",
        license_id="MIT",
        license_file="license",
        include=("source/*", "test/*", "test-d/*", "package.json", "tsconfig.json", "readme.md", "bench.ts"),
    ),
    Snapshot(
        name="go_mux",
        family="Go",
        url="https://github.com/gorilla/mux",
        commit="db9d1d0073d27a0a2d9a8c1bc52aa0af4374d265",
        commit_date="2024-06-19T19:50:04-04:00",
        license_id="BSD-3-Clause",
        license_file="LICENSE",
        include=("*.go", "go.mod", "README.md", "Makefile"),
    ),
    Snapshot(
        name="rust_semver",
        family="Rust",
        url="https://github.com/dtolnay/semver",
        commit="280ebcb6edac3aa4cdc545dbff8a26c5ac4861fe",
        commit_date="2026-06-23T20:02:34-07:00",
        license_id="MIT OR Apache-2.0",
        license_file="LICENSE-MIT",
        extra_license_files=("LICENSE-APACHE", "LICENSE-MIT"),
        include=("src/*", "tests/*", "Cargo.toml", "build.rs", "README.md"),
        notes="Dual-licensed upstream (`MIT OR Apache-2.0`, Cargo.toml). Both upstream license files are "
        "kept verbatim; recipients may use either license. `LICENSE` is a byte-identical copy of "
        "`LICENSE-MIT` only so every fixture directory has a `LICENSE` file; it does not narrow the grant.",
    ),
    Snapshot(
        name="swift_splash",
        family="Swift",
        url="https://github.com/JohnSundell/Splash",
        commit="2e3f17c2d09689c8bf175c4a84ff7f2ad3353301",
        commit_date="2022-06-08T22:13:57+02:00",
        license_id="MIT",
        license_file="LICENSE",
        include=("Sources/*", "Tests/*", "Package.swift", "README.md", "Makefile"),
    ),
    Snapshot(
        name="java_javapoet",
        family="Java",
        url="https://github.com/square/javapoet",
        commit="b9017a9503b76e11b4ad4c1a9f050e2d29112cb0",
        commit_date="2024-10-10T14:04:58-07:00",
        license_id="Apache-2.0",
        license_file="LICENSE.txt",
        include=("src/main/*", "src/test/*", "pom.xml", "README.md"),
    ),
    Snapshot(
        name="ruby_rack",
        family="Ruby",
        url="https://github.com/rack/rack",
        commit="a9833c8f3bd6b6d1e0ab35de00a1f1a16b5095f5",
        commit_date="2026-09-08T12:16:22+12:00",
        license_id="MIT",
        license_file="MIT-LICENSE",
        include=("lib/*", "test/*.rb", "rack.gemspec", "Gemfile", "Rakefile", "README.md"),
        notes="Non-Ruby test data (multipart bodies, CGI assets, .ru rackup files) is pruned; the "
        "specs are never executed by the eval harness.",
    ),
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _selected(paths: list[str], snap: Snapshot) -> list[str]:
    out: list[str] = []
    for path in paths:
        if any(part.startswith(".") for part in path.split("/")):
            continue
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in snap.include):
            continue
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in snap.exclude):
            continue
        out.append(path)
    return sorted(out)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_stats(fixture: Path) -> tuple[int, int]:
    files = [p for p in fixture.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def inventory(fixture: Path) -> list[tuple[str, int, str]]:
    """(relpath, bytes, sha256) of every vendored file except PROVENANCE.md, sorted."""
    rows = []
    for path in sorted(fixture.rglob("*")):
        rel = path.relative_to(fixture).as_posix()
        if path.is_file() and rel != "PROVENANCE.md":
            rows.append((rel, path.stat().st_size, _sha256(path)))
    return rows


def _provenance(
    snap: Snapshot, upstream_files: int, license_sha: str, files: int, size: int, acquired: str,
    rows: list[tuple[str, int, str]],
) -> str:
    include = "\n".join(f"- `{pattern}`" for pattern in snap.include)
    exclude = "\n".join(f"- `{pattern}`" for pattern in snap.exclude) or "- (none beyond the default rule)"
    extra = ", ".join(f"`{name}`" for name in snap.extra_license_files) or "(none)"
    lines = [
        f"# Provenance: {snap.name}",
        "",
        f"- Language family: {snap.family}",
        f"- Upstream: {snap.url}",
        f"- Commit: `{snap.commit}` (immutable; committer date {snap.commit_date})",
        f"- Archive URL: {snap.url}/tree/{snap.commit}",
        f"- License: {snap.license_id}",
        f"- `LICENSE`: verbatim copy of upstream `{snap.license_file}` (sha256 `{license_sha}`)",
        f"- Other upstream license files kept verbatim: {extra}",
        f"- Snapshot acquired: {acquired} by `scripts/vendor_oss_fixtures.py --fetch` (clone at the pinned commit)",
        f"- Upstream tracked files at that commit: {upstream_files}",
        f"- Vendored files (including LICENSE and this file): {files}",
        f"- Vendored bytes (including LICENSE and this file): {size}",
        f"- Inventoried files (everything except this file): {len(rows)}, {sum(r[1] for r in rows)} bytes",
        "",
        "## Prune rules",
        "",
        "Applied to `git ls-files` at the pinned commit. Any path with a dot-prefixed component",
        "(`.github/`, `.gitignore`, ...) is dropped. A path is kept only when it matches an include",
        "pattern (fnmatch; `*` also matches `/`) and no exclude pattern. File contents are",
        "byte-identical to upstream; nothing is edited.",
        "",
        "Include:",
        "",
        include,
        "",
        "Exclude:",
        "",
        exclude,
        "",
    ]
    if snap.notes:
        lines.extend(["## Notes", "", snap.notes, ""])
    lines.extend(["## Inventory", "", "| File | Bytes | SHA-256 |", "|---|---:|---|"])
    lines.extend(f"| `{rel}` | {nbytes} | `{digest}` |" for rel, nbytes, digest in rows)
    lines.append("")
    lines.extend(
        [
            "## Eval use",
            "",
            "`evals/run_evals.py` copies this directory into a temporary git repository, creates",
            "deterministic synthetic commits (fixed dates) so COCHANGE exists, ingests it offline and",
            "scores OSS retrieval tasks, call-edge resolution, and impacted-test selection.",
            "",
        ]
    )
    return "\n".join(lines)


def fetch(snap: Snapshot, acquired: str) -> None:
    dest = OSS_ROOT / snap.name
    with tempfile.TemporaryDirectory(prefix="cortex-oss-") as tmp:
        clone = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "--quiet", "--no-checkout", snap.url, str(clone)], check=True)
        _git(clone, "checkout", "--quiet", "--detach", snap.commit)
        head = _git(clone, "rev-parse", "HEAD").strip()
        if head != snap.commit:
            raise SystemExit(f"{snap.name}: HEAD {head} != pinned {snap.commit}")
        committed = _git(clone, "show", "-s", "--format=%cI", "HEAD").strip()
        if committed != snap.commit_date:
            raise SystemExit(f"{snap.name}: committer date {committed} != recorded {snap.commit_date}")
        upstream = [line for line in _git(clone, "ls-files").splitlines() if line]
        selected = _selected(upstream, snap)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for rel in selected:
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(clone / rel, target)
        for rel in snap.extra_license_files:
            shutil.copyfile(clone / rel, dest / rel)
        shutil.copyfile(clone / snap.license_file, dest / "LICENSE")
        license_sha = _sha256(clone / snap.license_file)
        upstream_count = len(upstream)
    # Two passes so the recorded counts include PROVENANCE.md itself.
    (dest / "PROVENANCE.md").write_text("", encoding="utf-8")
    for _ in range(3):
        files, size = fixture_stats(dest)
        (dest / "PROVENANCE.md").write_text(
            _provenance(snap, upstream_count, license_sha, files, size, acquired, inventory(dest)),
            encoding="utf-8",
        )
    files, size = fixture_stats(dest)
    print(f"{snap.name}: {files} files, {size} bytes @ {snap.commit}")


def verify() -> int:
    failures: list[str] = []
    total = 0
    for snap in SNAPSHOTS:
        dest = OSS_ROOT / snap.name
        if not dest.is_dir():
            failures.append(f"{snap.name}: missing")
            continue
        files, size = fixture_stats(dest)
        total += size
        text = (dest / "PROVENANCE.md").read_text(encoding="utf-8")
        rows = inventory(dest)
        needles = [snap.commit, snap.url, f"Vendored files (including LICENSE and this file): {files}",
                   f"Vendored bytes (including LICENSE and this file): {size}", _sha256(dest / "LICENSE"),
                   f"Inventoried files (everything except this file): {len(rows)}, {sum(r[1] for r in rows)} bytes"]
        needles += [f"| `{rel}` | {nbytes} | `{digest}` |" for rel, nbytes, digest in rows]
        for needle in needles:
            if needle not in text:
                failures.append(f"{snap.name}: PROVENANCE.md lacks {needle!r}")
        listed = text.count("\n| `")
        if listed != len(rows):
            failures.append(f"{snap.name}: inventory lists {listed} files, directory has {len(rows)}")
        if files > MAX_FILES:
            failures.append(f"{snap.name}: {files} files > {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        failures.append(f"total {total} bytes > {MAX_TOTAL_BYTES}")
    for line in failures:
        print(line, file=sys.stderr)
    print(f"total vendored bytes: {total}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch", action="store_true", help="clone upstream (network) and rewrite snapshots")
    parser.add_argument("--verify", action="store_true", help="offline consistency check of vendored snapshots")
    parser.add_argument("--only", action="append", default=[], help="limit --fetch to these fixture names")
    parser.add_argument("--acquired", default=datetime.date.today().isoformat(),
                        help="acquisition date recorded in PROVENANCE.md (default: today, local time)")
    args = parser.parse_args()
    if args.fetch:
        for snap in SNAPSHOTS:
            if args.only and snap.name not in args.only:
                continue
            fetch(snap, args.acquired)
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
