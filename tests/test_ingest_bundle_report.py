from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cortex.benchmark import run_benchmark
from cortex.bundle import generate_bundle
from cortex.ingest import ingest_repository
from cortex.integrations import (
    claude_status,
    codex_status,
    git_hook_status,
    install_claude,
    install_codex,
    install_git_hooks,
    install_global_skill,
    uninstall_claude,
    uninstall_codex,
    uninstall_git_hooks,
)
from cortex.report import generate_report
from cortex.store import CortexStore, default_db_path


FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample_repo"


class IngestBundleReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cortex.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _copy_fixture_repo(self) -> Path:
        destination = Path(self.temp_dir.name) / "sample_repo_copy"
        shutil.copytree(FIXTURE_REPO, destination, ignore=shutil.ignore_patterns(".cortex"))
        return destination

    def test_ingest_repository_populates_store(self) -> None:
        summary = ingest_repository(FIXTURE_REPO, commit_limit=5, db_path=self.db_path)

        self.assertGreaterEqual(summary["source_count"], 2)
        self.assertEqual(summary["commit_count"], 2)
        self.assertGreater(summary["node_count"], 0)
        self.assertGreater(summary["edge_count"], 0)

        store = CortexStore(self.db_path)
        self.assertGreaterEqual(len(store.fetch_sources(FIXTURE_REPO)), 2)
        self.assertEqual(len(store.fetch_commits(FIXTURE_REPO)), 2)
        nodes, edges = store.fetch_graph(FIXTURE_REPO)
        self.assertTrue(nodes)
        self.assertTrue(edges)

    def test_generate_bundle_respects_budget(self) -> None:
        ingest_repository(FIXTURE_REPO, commit_limit=5, db_path=self.db_path)

        bundle = generate_bundle(
            FIXTURE_REPO,
            task="Summarize the retrieval graph and token budget behavior",
            budget=60,
            db_path=self.db_path,
            output_format="json",
        )

        self.assertLessEqual(bundle["total_tokens"], 60)
        self.assertTrue(bundle["items"])
        self.assertTrue(any("retrieval" in item["content"].lower() for item in bundle["items"]))

        store = CortexStore(self.db_path)
        latest = store.fetch_latest_bundle(FIXTURE_REPO)
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertLessEqual(latest.total_tokens, 60)

    def test_generate_report_includes_central_nodes(self) -> None:
        ingest_repository(FIXTURE_REPO, commit_limit=5, db_path=self.db_path)

        report = generate_report(FIXTURE_REPO, db_path=self.db_path)

        self.assertIn("# Cortex Report: sample_repo", report)
        self.assertIn("## God Nodes", report)

    def test_default_db_path_uses_local_cortex_directory(self) -> None:
        path = default_db_path(FIXTURE_REPO)
        self.assertEqual(path.name, "cortex.db")
        self.assertEqual(path.parent.name, ".cortex")

    def test_benchmark_reports_reduction_ratio(self) -> None:
        result = run_benchmark(FIXTURE_REPO, commit_limit=5, budget=60)

        self.assertGreater(result["corpus_tokens"], 0)
        self.assertGreater(result["avg_query_tokens"], 0)
        self.assertGreater(result["reduction_ratio"], 1.0)
        self.assertTrue(result["per_question"])

    def test_refresh_writes_default_report_path(self) -> None:
        repo = self._copy_fixture_repo()

        summary = ingest_repository(repo, commit_limit=5)
        report_path = Path(summary["repo_path"]) / ".cortex" / "cortex_report.md"
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

    def test_codex_install_merges_with_existing_files(self) -> None:
        (self.project_dir / "AGENTS.md").write_text(
            "## existing\n\nExisting instructions\n",
            encoding="utf-8",
        )
        codex_hooks = self.project_dir / ".codex" / "hooks.json"
        codex_hooks.parent.mkdir(parents=True)
        codex_hooks.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "echo existing-hook",
                                    }
                                ],
                            }
                        ]
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        install_codex(self.project_dir)

        agents = (self.project_dir / "AGENTS.md").read_text(encoding="utf-8")
        hooks = codex_hooks.read_text(encoding="utf-8")
        self.assertIn("## existing", agents)
        self.assertIn("## cortex", agents)
        self.assertIn("existing-hook", hooks)
        self.assertIn("cortex", hooks)
        self.assertEqual(codex_status(self.project_dir), {"agents": True, "hook": True})

        uninstall_codex(self.project_dir)

        agents = (self.project_dir / "AGENTS.md").read_text(encoding="utf-8")
        hooks = codex_hooks.read_text(encoding="utf-8")
        self.assertIn("## existing", agents)
        self.assertNotIn("## cortex", agents)
        self.assertIn("existing-hook", hooks)
        self.assertNotIn("cortex", hooks)

    def test_claude_install_merges_with_existing_files(self) -> None:
        (self.project_dir / "CLAUDE.md").write_text(
            "## existing\n\nExisting instructions\n",
            encoding="utf-8",
        )
        settings = self.project_dir / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Glob|Grep",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "echo existing-hook",
                                    }
                                ],
                            }
                        ]
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        install_claude(self.project_dir)

        claude_md = (self.project_dir / "CLAUDE.md").read_text(encoding="utf-8")
        hooks = settings.read_text(encoding="utf-8")
        self.assertIn("## existing", claude_md)
        self.assertIn("## cortex", claude_md)
        self.assertIn("existing-hook", hooks)
        self.assertIn("cortex", hooks)
        self.assertEqual(claude_status(self.project_dir), {"claude_md": True, "hook": True})

        uninstall_claude(self.project_dir)

        claude_md = (self.project_dir / "CLAUDE.md").read_text(encoding="utf-8")
        hooks = settings.read_text(encoding="utf-8")
        self.assertIn("## existing", claude_md)
        self.assertNotIn("## cortex", claude_md)
        self.assertIn("existing-hook", hooks)
        self.assertNotIn("cortex", hooks)

    def test_global_install_uses_temp_home(self) -> None:
        codex = install_global_skill("codex", home_dir=self.home_dir)
        claude = install_global_skill("claude", home_dir=self.home_dir)

        codex_skill = Path(codex["skill"])
        claude_skill = Path(claude["skill"])
        registration = Path(claude["registration"])

        self.assertTrue(codex_skill.exists())
        self.assertTrue(claude_skill.exists())
        self.assertTrue(registration.exists())
        self.assertIn(str(self.home_dir), str(codex_skill))
        self.assertIn("# cortex", registration.read_text(encoding="utf-8"))

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
