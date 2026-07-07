from __future__ import annotations

import hashlib
import json
import subprocess
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

class IngestWritesMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp_dir.name) / "repo"
        self.repo.mkdir()
        (self.repo / "main.py").write_text("print('hi')\n", encoding="utf-8")

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
        self.assertEqual(default_report_path(self.repo), legacy.parent.resolve() / "cortex_report.md")

    def test_report_follows_explicit_db_path(self) -> None:
        from cortex.report import default_report_path

        custom_db = Path(self.temp_dir.name) / "elsewhere" / "cortex.db"
        self.assertEqual(
            default_report_path(self.repo, db_path=custom_db),
            custom_db.parent / "cortex_report.md",
        )


if __name__ == "__main__":
    unittest.main()
