from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cortex.benchmark import run_benchmark
from cortex.bundle import generate_bundle
from cortex.ingest import ingest_repository
from cortex.integrations import git_hook_status, install_git_hooks, uninstall_git_hooks
from cortex.report import generate_report
from cortex.store import CortexStore, default_db_path


FIXTURE_SOURCE = Path(__file__).resolve().parent / "fixtures" / "sample_repo"


class IngestBundleReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cortex.db"
        self.fixture_repo = Path(self.temp_dir.name) / "sample_repo"
        self._create_fixture_repo()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_fixture_repo(self) -> None:
        self.fixture_repo.mkdir()
        (self.fixture_repo / "README.md").write_text((FIXTURE_SOURCE / "README.md").read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(["git", "init"], cwd=self.fixture_repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "README.md"],
            cwd=self.fixture_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed sample repo"],
            cwd=self.fixture_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        (self.fixture_repo / "docs.md").write_text((FIXTURE_SOURCE / "docs.md").read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "docs.md"],
            cwd=self.fixture_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "add ranking note"],
            cwd=self.fixture_repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def _copy_fixture_repo(self) -> Path:
        destination = Path(self.temp_dir.name) / "sample_repo_copy"
        shutil.copytree(self.fixture_repo, destination, ignore=shutil.ignore_patterns(".cortex"))
        return destination

    def test_ingest_repository_populates_store(self) -> None:
        summary = ingest_repository(self.fixture_repo, commit_limit=5, db_path=self.db_path)

        self.assertGreaterEqual(summary["source_count"], 2)
        self.assertEqual(summary["commit_count"], 2)
        self.assertGreater(summary["node_count"], 0)
        self.assertGreater(summary["edge_count"], 0)

        store = CortexStore(self.db_path)
        self.assertGreaterEqual(len(store.fetch_sources(self.fixture_repo)), 2)
        self.assertEqual(len(store.fetch_commits(self.fixture_repo)), 2)
        nodes, edges = store.fetch_graph(self.fixture_repo)
        self.assertTrue(nodes)
        self.assertTrue(edges)

    def test_generate_bundle_respects_budget(self) -> None:
        ingest_repository(self.fixture_repo, commit_limit=5, db_path=self.db_path)

        bundle = generate_bundle(
            self.fixture_repo,
            task="Summarize the retrieval graph and token budget behavior",
            budget=60,
            db_path=self.db_path,
            output_format="json",
        )

        self.assertLessEqual(bundle["total_tokens"], 60)
        self.assertTrue(bundle["items"])
        self.assertTrue(any("retrieval" in item["content"].lower() for item in bundle["items"]))

        store = CortexStore(self.db_path)
        latest = store.fetch_latest_bundle(self.fixture_repo)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertLessEqual(latest.total_tokens, 60)

    def test_generate_report_includes_central_nodes(self) -> None:
        ingest_repository(self.fixture_repo, commit_limit=5, db_path=self.db_path)

        report = generate_report(self.fixture_repo, db_path=self.db_path)

        self.assertIn("# Cortex Report: sample_repo", report)
        self.assertIn("## God Nodes", report)

    def test_default_db_path_resolves_to_cortex_db(self) -> None:
        path = default_db_path(self.fixture_repo)
        self.assertEqual(path.name, "cortex.db")

    def test_benchmark_reports_reduction_ratio(self) -> None:
        result = run_benchmark(self.fixture_repo, commit_limit=5, budget=60)

        self.assertGreater(result["corpus_tokens"], 0)
        self.assertGreater(result["avg_query_tokens"], 0)
        self.assertGreater(result["reduction_ratio"], 1.0)
        self.assertTrue(result["per_question"])

    def test_refresh_writes_default_report_path(self) -> None:
        repo = self._copy_fixture_repo()

        summary = ingest_repository(repo, commit_limit=5)
        report_path = default_db_path(Path(summary["repo_path"])).parent / "cortex_report.md"
        report_path.write_text(generate_report(repo), encoding="utf-8")

        self.assertTrue(report_path.exists())
        self.assertIn("Cortex Report", report_path.read_text(encoding="utf-8"))


class IntegrationInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name) / "project"
        self.project_dir.mkdir(parents=True)
        self.home_dir = Path(self.temp_dir.name) / "home"
        self.home_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_git_hook_install_preserves_existing_hook_blocks(self) -> None:
        subprocess.run(["git", "init"], cwd=self.project_dir, check=True, capture_output=True, text=True)
        hooks_dir = self.project_dir / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        post_commit = hooks_dir / "post-commit"
        post_checkout = hooks_dir / "post-checkout"
        post_commit.write_text("#!/bin/sh\n# existing-hook-start\necho existing-hook\n# existing-hook-end\n", encoding="utf-8")
        post_checkout.write_text(
            "#!/bin/sh\n# existing-checkout-hook-start\necho existing-hook\n# existing-checkout-hook-end\n",
            encoding="utf-8",
        )

        install_git_hooks(self.project_dir)

        self.assertEqual(git_hook_status(self.project_dir), {"post_commit": True, "post_checkout": True})
        self.assertIn("existing-hook-start", post_commit.read_text(encoding="utf-8"))
        self.assertIn("cortex-hook-start", post_commit.read_text(encoding="utf-8"))
        self.assertIn("existing-checkout-hook-start", post_checkout.read_text(encoding="utf-8"))
        self.assertIn("cortex-checkout-hook-start", post_checkout.read_text(encoding="utf-8"))

        uninstall_git_hooks(self.project_dir)

        self.assertIn("existing-hook-start", post_commit.read_text(encoding="utf-8"))
        self.assertNotIn("cortex-hook-start", post_commit.read_text(encoding="utf-8"))
        self.assertIn("existing-checkout-hook-start", post_checkout.read_text(encoding="utf-8"))
        self.assertNotIn("cortex-checkout-hook-start", post_checkout.read_text(encoding="utf-8"))
