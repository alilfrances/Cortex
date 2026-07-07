# Central Data Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Cortex's per-repo data (`cortex.db`, `cortex_report.md`) out of target repos into a central store at `~/.cortex/data/<sha256-prefix-of-repo-path>/`, so indexing leaves zero footprint in the target repo, with backward compatibility for existing in-repo `.cortex/` directories and a `CORTEX_DATA_DIR` env override.

**Architecture:** `store.default_db_path()` is the single choke point every consumer (CLI, MCP tools, session-start hook, ingest/bundle/report/enrich/benchmark) already uses as its fallback. We change its resolution order — legacy `<repo>/.cortex/cortex.db` if it exists, else `<data_root>/<hash>/cortex.db` — and make `report.default_report_path()` co-locate the report next to whatever db path resolves. A `meta.json` beside each central db records the original repo path so a new `cortex gc` command can find and prune orphans. Tests get an autouse fixture that points `CORTEX_DATA_DIR` at a tmp dir so no test ever writes to the real `~/.cortex`.

**Tech Stack:** Python 3 stdlib only (`hashlib`, `os`, `json`, `shutil`). No new dependencies.

## Global Constraints

- No new runtime dependencies (do NOT add `platformdirs`; use `Path.home() / ".cortex" / "data"`).
- Backward compatibility is mandatory: a repo with an existing `<repo>/.cortex/cortex.db` must keep using it, unchanged.
- Central-store key is `hashlib.sha256(str(resolved_repo_path).encode("utf-8")).hexdigest()[:16]` — never the project name.
- Env override: `CORTEX_DATA_DIR` replaces the `~/.cortex/data` base (expanduser + resolve).
- `cortex gc --prune` may only delete directories directly under `data_root()` that contain a `meta.json` whose recorded `repo_path` no longer exists. Never delete dirs lacking `meta.json`; list them as `unknown`.
- Version bump: `pyproject.toml` 0.3.0 → 0.4.0. All 128+ existing tests must still pass.
- Run tests from repo root: `python3 -m pytest tests/ -q` (conftest adds `src/` to `sys.path`).

---

### Task 1: Test isolation + central path resolution in `store.py`

The conftest fixture MUST land in the same commit as the path change — otherwise every existing test that calls `default_db_path()` on a tmp fixture repo would write into the developer's real `~/.cortex/data`.

**Files:**
- Modify: `src/cortex/store.py:1-13`
- Modify: `tests/conftest.py`
- Modify: `tests/test_ingest_bundle_report.py:113-115` (test `test_default_db_path_uses_local_cortex_directory`)
- Test: `tests/test_central_store.py` (create)

**Interfaces:**
- Produces: `data_root() -> Path`, `repo_data_dir(repo_path: Path) -> Path`, `default_db_path(repo_path: Path) -> Path` (signature unchanged), `write_repo_meta(db_path: Path, repo_root: Path) -> None` — all in `cortex.store`.
- Consumes: nothing new; existing callers keep calling `default_db_path`.

- [ ] **Step 1: Add the autouse isolation fixture to `tests/conftest.py`**

Replace the entire file content with:

```python
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _isolated_cortex_data_dir(tmp_path_factory, monkeypatch):
    """Keep every test's central store inside pytest's tmp tree, never ~/.cortex."""
    data_dir = tmp_path_factory.mktemp("cortex-data")
    monkeypatch.setenv("CORTEX_DATA_DIR", str(data_dir))
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_central_store.py`:

```python
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cortex.store import data_root, default_db_path, repo_data_dir, write_repo_meta


class CentralStorePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_data_root_defaults_to_home_dot_cortex(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("CORTEX_DATA_DIR", None)
            self.assertEqual(data_root(), Path.home() / ".cortex" / "data")

    def test_data_root_honours_env_override(self) -> None:
        override = Path(self.temp_dir.name) / "custom"
        with mock.patch.dict("os.environ", {"CORTEX_DATA_DIR": str(override)}):
            self.assertEqual(data_root(), override.resolve())

    def test_repo_data_dir_keys_by_path_hash(self) -> None:
        resolved = self.repo.resolve()
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(repo_data_dir(self.repo), data_root() / digest)

    def test_default_db_path_is_central_for_fresh_repo(self) -> None:
        path = default_db_path(self.repo)
        self.assertEqual(path, repo_data_dir(self.repo) / "cortex.db")
        self.assertNotIn(str(self.repo.resolve()), str(path))

    def test_default_db_path_prefers_existing_legacy_dir(self) -> None:
        legacy = self.repo / ".cortex" / "cortex.db"
        legacy.parent.mkdir()
        legacy.touch()
        self.assertEqual(default_db_path(self.repo), legacy.resolve())

    def test_write_repo_meta_records_repo_path(self) -> None:
        db_path = repo_data_dir(self.repo) / "cortex.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        write_repo_meta(db_path, self.repo)
        meta = json.loads((db_path.parent / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["repo_path"], str(self.repo.resolve()))
        self.assertIn("updated_at", meta)

    def test_write_repo_meta_skips_legacy_layout(self) -> None:
        legacy_db = self.repo / ".cortex" / "cortex.db"
        legacy_db.parent.mkdir()
        write_repo_meta(legacy_db, self.repo)
        self.assertFalse((legacy_db.parent / "meta.json").exists())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_central_store.py -q`
Expected: FAIL / ERROR with `ImportError: cannot import name 'data_root' from 'cortex.store'`

- [ ] **Step 4: Implement in `src/cortex/store.py`**

Replace lines 1-13 (imports + `default_db_path`) with:

```python
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from .models import BundleItem, CommitRecord, Community, GraphEdge, GraphNode, RetrievalBundle, SourceRecord

LEGACY_DIR_NAME = ".cortex"


def data_root() -> Path:
    """Base directory for all central per-repo data dirs."""
    override = os.environ.get("CORTEX_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".cortex" / "data"


def repo_data_dir(repo_path: Path) -> Path:
    """Central data dir for one repo, keyed by hash of its resolved path."""
    resolved = repo_path.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return data_root() / digest


def default_db_path(repo_path: Path) -> Path:
    root = repo_path.resolve()
    legacy = root / LEGACY_DIR_NAME / "cortex.db"
    if legacy.exists():
        return legacy
    return repo_data_dir(root) / "cortex.db"


def write_repo_meta(db_path: Path, repo_root: Path) -> None:
    """Record which repo a central data dir belongs to, for `cortex gc` and debugging."""
    parent = db_path.parent
    if parent.name == LEGACY_DIR_NAME:
        return
    parent.mkdir(parents=True, exist_ok=True)
    meta = {"repo_path": str(repo_root.resolve()), "updated_at": int(time.time())}
    (parent / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
```

Keep the existing `class CortexStore` and everything below it unchanged.

- [ ] **Step 5: Update the stale legacy-layout test**

In `tests/test_ingest_bundle_report.py`, replace:

```python
    def test_default_db_path_uses_local_cortex_directory(self) -> None:
        path = default_db_path(self.fixture_repo)
        self.assertEqual(path.name, "cortex.db")
```

with:

```python
    def test_default_db_path_resolves_to_cortex_db(self) -> None:
        path = default_db_path(self.fixture_repo)
        self.assertEqual(path.name, "cortex.db")
```

(The name asserted a local `.cortex` dir; the body never did. Rename only.)

- [ ] **Step 6: Run the new tests, then the full suite**

Run: `python3 -m pytest tests/test_central_store.py -q`
Expected: all PASS

Run: `python3 -m pytest tests/ -q`
Expected: all PASS (existing tests now resolve central paths into the fixture tmp dir via `CORTEX_DATA_DIR`)

- [ ] **Step 7: Commit**

```bash
git add src/cortex/store.py tests/conftest.py tests/test_central_store.py tests/test_ingest_bundle_report.py
git commit -m "feat(store): central per-repo data dir with legacy .cortex fallback"
```

---

### Task 2: Write `meta.json` on ingest

**Files:**
- Modify: `src/cortex/ingest.py:158-167` (top of `ingest_repository`)
- Test: `tests/test_central_store.py` (append)

**Interfaces:**
- Consumes: `write_repo_meta(db_path, repo_root)` from Task 1.
- Produces: every `ingest_repository()` call on a central-store repo leaves `meta.json` beside `cortex.db`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_central_store.py` (top-level imports already present; add `subprocess` and the ingest import inside the class):

```python
class IngestWritesMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        (self.repo / "main.py").write_text("print('hi')\n", encoding="utf-8")
        import subprocess

        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "."], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=self.repo,
            check=True,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ingest_writes_meta_beside_central_db(self) -> None:
        from cortex.ingest import ingest_repository

        ingest_repository(self.repo, commit_limit=5)
        db_path = default_db_path(self.repo)
        meta_path = db_path.parent / "meta.json"
        self.assertTrue(db_path.exists())
        self.assertTrue(meta_path.exists())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["repo_path"], str(self.repo.resolve()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_central_store.py::IngestWritesMetaTests -q`
Expected: FAIL on `self.assertTrue(meta_path.exists())`

- [ ] **Step 3: Implement**

In `src/cortex/ingest.py`, change the import on line 11 from:

```python
from .store import CortexStore, default_db_path
```

to:

```python
from .store import CortexStore, default_db_path, write_repo_meta
```

and in `ingest_repository`, immediately after `store = CortexStore(db_path or default_db_path(repo_root))`, add:

```python
    write_repo_meta(store.db_path, repo_root)
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_central_store.py tests/test_ingest_incremental.py tests/test_ingest_bundle_report.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/cortex/ingest.py tests/test_central_store.py
git commit -m "feat(ingest): record repo path in meta.json beside central db"
```

---

### Task 3: Co-locate report with the resolved db

**Files:**
- Modify: `src/cortex/report.py:28-30` and `report.py:136-138`
- Test: `tests/test_central_store.py` (append)

**Interfaces:**
- Consumes: `default_db_path` from Task 1.
- Produces: `default_report_path(repo_path: Path, db_path: Path | None = None) -> Path` — report lands in the same directory as the db (legacy `.cortex/` or central dir). `write_report(repo_path, db_path)` behavior follows.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_central_store.py`:

```python
class ReportPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_report_co_locates_with_central_db(self) -> None:
        from cortex.report import default_report_path

        expected = default_db_path(self.repo).parent / "cortex_report.md"
        self.assertEqual(default_report_path(self.repo), expected)

    def test_report_co_locates_with_legacy_db(self) -> None:
        from cortex.report import default_report_path

        legacy = self.repo / ".cortex" / "cortex.db"
        legacy.parent.mkdir()
        legacy.touch()
        self.assertEqual(default_report_path(self.repo), legacy.parent / "cortex_report.md")

    def test_report_follows_explicit_db_path(self) -> None:
        from cortex.report import default_report_path

        custom_db = Path(self.temp_dir.name) / "elsewhere" / "cortex.db"
        self.assertEqual(
            default_report_path(self.repo, db_path=custom_db),
            custom_db.parent / "cortex_report.md",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_central_store.py::ReportPathTests -q`
Expected: FAIL — central case returns `<repo>/.cortex/cortex_report.md` instead of the central dir; explicit-db case raises `TypeError` (no `db_path` parameter)

- [ ] **Step 3: Implement**

In `src/cortex/report.py`, replace:

```python
def default_report_path(repo_path: Path) -> Path:
    repo_root = repo_path.resolve()
    return repo_root / ".cortex" / "cortex_report.md"
```

with:

```python
def default_report_path(repo_path: Path, db_path: Path | None = None) -> Path:
    resolved_db = db_path or default_db_path(repo_path.resolve())
    return resolved_db.parent / "cortex_report.md"
```

(`default_db_path` is already imported at the top of `report.py`.)

In `write_report` (line 136-138), change:

```python
    report_path = default_report_path(repo_root)
```

to:

```python
    report_path = default_report_path(repo_root, db_path=db_path)
```

- [ ] **Step 4: Update the stale refresh test**

In `tests/test_ingest_bundle_report.py`, `test_refresh_writes_default_report_path` builds the path as `Path(summary["repo_path"]) / ".cortex" / "cortex_report.md"`. Replace those lines with:

```python
        summary = ingest_repository(repo, commit_limit=5)
        from cortex.report import default_report_path

        report_path = default_report_path(Path(summary["repo_path"]))
        report_path.write_text(generate_report(repo), encoding="utf-8")

        self.assertTrue(report_path.exists())
```

(Keep the surrounding test method structure; only the path construction changes. If the import style clashes with the file's top-level imports, hoist `default_report_path` to the top-level import block instead.)

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/test_central_store.py tests/test_ingest_bundle_report.py tests/test_report_v2.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/cortex/report.py tests/test_central_store.py tests/test_ingest_bundle_report.py
git commit -m "feat(report): co-locate report with resolved db path"
```

---

### Task 4: `cortex gc` command

**Files:**
- Modify: `src/cortex/cli.py` (new subparser near line 143-161; new handler near line 238)
- Test: `tests/test_central_store.py` (append)

**Interfaces:**
- Consumes: `data_root()` from Task 1.
- Produces: `gc_data_dirs(prune: bool = False) -> dict` in `cortex.cli` returning `{"active": [...], "orphaned": [...], "unknown": [...], "pruned": [...]}` where each list item is `{"dir": str, "repo_path": str | None}`; CLI subcommand `cortex gc [--prune]` printing that dict as JSON.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_central_store.py`:

```python
class GcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.live_repo = Path(self.temp_dir.name) / "live"
        self.live_repo.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_entry(self, repo_path: Path) -> Path:
        entry = repo_data_dir(repo_path)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "cortex.db").touch()
        write_repo_meta(entry / "cortex.db", repo_path)
        return entry

    def test_gc_classifies_active_orphaned_unknown(self) -> None:
        from cortex.cli import gc_data_dirs

        active = self._make_entry(self.live_repo)
        gone_repo = Path(self.temp_dir.name) / "gone"
        gone_repo.mkdir()
        orphan = self._make_entry(gone_repo)
        gone_repo.rmdir()
        unknown = data_root() / "deadbeefdeadbeef"
        unknown.mkdir(parents=True)

        result = gc_data_dirs(prune=False)
        self.assertEqual([e["dir"] for e in result["active"]], [str(active)])
        self.assertEqual([e["dir"] for e in result["orphaned"]], [str(orphan)])
        self.assertEqual([e["dir"] for e in result["unknown"]], [str(unknown)])
        self.assertEqual(result["pruned"], [])
        self.assertTrue(orphan.exists())

    def test_gc_prune_deletes_only_orphans_with_meta(self) -> None:
        from cortex.cli import gc_data_dirs

        active = self._make_entry(self.live_repo)
        gone_repo = Path(self.temp_dir.name) / "gone"
        gone_repo.mkdir()
        orphan = self._make_entry(gone_repo)
        gone_repo.rmdir()
        unknown = data_root() / "deadbeefdeadbeef"
        unknown.mkdir(parents=True)

        result = gc_data_dirs(prune=True)
        self.assertEqual([e["dir"] for e in result["pruned"]], [str(orphan)])
        self.assertFalse(orphan.exists())
        self.assertTrue(active.exists())
        self.assertTrue(unknown.exists())

    def test_gc_handles_missing_data_root(self) -> None:
        from cortex.cli import gc_data_dirs

        import shutil

        shutil.rmtree(data_root(), ignore_errors=True)
        result = gc_data_dirs(prune=False)
        self.assertEqual(result, {"active": [], "orphaned": [], "unknown": [], "pruned": []})
```

Also extend the top-of-file import in `tests/test_central_store.py` so it reads:

```python
from cortex.store import data_root, default_db_path, repo_data_dir, write_repo_meta
```

(already the case from Task 1 — verify, don't duplicate).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_central_store.py::GcTests -q`
Expected: FAIL with `ImportError: cannot import name 'gc_data_dirs' from 'cortex.cli'`

- [ ] **Step 3: Implement in `src/cortex/cli.py`**

Change the import on line 27 from:

```python
from .store import CortexStore, default_db_path
```

to:

```python
from .store import CortexStore, data_root, default_db_path
```

Add near the other top-level helpers (after the `_store` helper around line 33):

```python
def gc_data_dirs(prune: bool = False) -> dict:
    """Classify central data dirs by whether their source repo still exists."""
    import shutil

    result: dict[str, list[dict[str, str | None]]] = {
        "active": [],
        "orphaned": [],
        "unknown": [],
        "pruned": [],
    }
    base = data_root()
    if not base.is_dir():
        return result
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        meta_path = entry / "meta.json"
        if not meta_path.exists():
            result["unknown"].append({"dir": str(entry), "repo_path": None})
            continue
        try:
            repo_path = json.loads(meta_path.read_text(encoding="utf-8")).get("repo_path")
        except (json.JSONDecodeError, OSError):
            result["unknown"].append({"dir": str(entry), "repo_path": None})
            continue
        record = {"dir": str(entry), "repo_path": repo_path}
        if repo_path and Path(repo_path).is_dir():
            result["active"].append(record)
        else:
            result["orphaned"].append(record)
            if prune:
                shutil.rmtree(entry)
                result["pruned"].append(record)
    return result
```

(`json` is already imported in `cli.py`; verify, add if missing.)

Add the subparser next to the others (after the `refresh` parser around line 161):

```python
    gc_parser = subparsers.add_parser("gc", help="List or prune central data dirs whose repo is gone")
    gc_parser.add_argument("--prune", action="store_true", help="Delete orphaned data dirs")
```

Add the dispatch branch alongside the other `if args.command == ...` blocks:

```python
    if args.command == "gc":
        print(json.dumps(gc_data_dirs(prune=args.prune), indent=2))
        return 0
```

Match the surrounding dispatch style (if other branches don't `return 0`, mirror what they do).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_central_store.py -q`
Expected: all PASS

Sanity-check the CLI wiring: `python3 -m cortex gc` (from the Cortex repo root with `PYTHONPATH=src` if needed)
Expected: JSON dict with the four keys.

- [ ] **Step 5: Commit**

```bash
git add src/cortex/cli.py tests/test_central_store.py
git commit -m "feat(cli): cortex gc lists and prunes orphaned central data dirs"
```

---

### Task 5: Docs, changelog, version bump

**Files:**
- Modify: `README.md:40,76,96`
- Modify: `skills/cortex/SKILL.md:8,38`
- Modify: `CHANGELOG.md`
- Modify: `pyproject.toml:7`

**Interfaces:** none (docs only).

- [ ] **Step 1: Update `README.md`**

Line 40 (SessionStart hook paragraph): replace `When a project has `.cortex/cortex.db`` with `When a project has a Cortex index (legacy `.cortex/cortex.db` in-repo, or the central store under `~/.cortex/data/`)`.

Line 76: replace

```
This creates `.cortex/cortex.db` and `.cortex/cortex_report.md` inside the target repo.
```

with:

```
This creates `cortex.db` and `cortex_report.md` under `~/.cortex/data/<repo-path-hash>/` — the target repo itself is never touched. Repos indexed before v0.4.0 keep using their existing in-repo `.cortex/` directory. Set `CORTEX_DATA_DIR` to relocate the central store. Run `cortex gc --prune` to delete data for repos that no longer exist.
```

Line 96 (CLI table): update the `cortex report` row's `--out .cortex` default mention to `--out` (defaults beside the db), and add a table row:

```
| `cortex gc [--prune]` | List central data dirs; `--prune` deletes ones whose repo is gone. |
```

- [ ] **Step 2: Update `skills/cortex/SKILL.md`**

Line 8: replace `when `.cortex/` exists` with `when a Cortex index exists (in-repo `.cortex/` or central `~/.cortex/data/`)`.

Line 38: replace `Use when `.cortex/cortex.db` is missing, stale, ...` with `Use when the Cortex index is missing, stale, ...` (keep the rest of the sentence).

- [ ] **Step 3: Update `CHANGELOG.md` and `pyproject.toml`**

`pyproject.toml` line 7: `version = "0.3.0"` → `version = "0.4.0"`.

Prepend a `## 0.4.0` section to `CHANGELOG.md` following the file's existing entry format:

```markdown
## 0.4.0 — 2026-07-07

- Per-repo data (`cortex.db`, `cortex_report.md`) now lives in a central store at `~/.cortex/data/<sha256-prefix-of-repo-path>/` instead of a `.cortex/` directory inside the target repo. Indexing no longer touches the target repo.
- Existing in-repo `.cortex/` directories keep working (legacy fallback); delete one to migrate that repo to the central store on next refresh.
- New: `CORTEX_DATA_DIR` env var overrides the central store location.
- New: `cortex gc [--prune]` lists or deletes central data dirs whose source repo is gone.
- Each central data dir carries a `meta.json` recording the source repo path.
```

- [ ] **Step 4: Full suite + commit**

Run: `python3 -m pytest tests/ -q`
Expected: all PASS

```bash
git add README.md skills/cortex/SKILL.md CHANGELOG.md pyproject.toml
git commit -m "docs: central data store docs, changelog, bump to 0.4.0"
```
