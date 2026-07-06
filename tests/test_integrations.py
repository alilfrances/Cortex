from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cortex.cli import build_parser
from cortex.integrations import migrate, uninstall_claude, uninstall_codex


OLD_CORTEX_SECTION = """\
## cortex

This project has Cortex context artifacts under .cortex/.

Rules:
- Before answering architecture or codebase questions, read .cortex/cortex_report.md before searching raw files.
- If the report is insufficient for the task, run `cortex bundle . --task "<question>" --budget 4000` and answer from that bundle before broad raw-file exploration.
- After meaningful code changes in this session, run `cortex refresh .` to keep Cortex artifacts current when hooks are unavailable.
"""


class IntegrationMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.temp_dir.name) / "project"
        self.project_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_uninstall_removes_old_injected_sections_and_hooks(self) -> None:
        (self.project_dir / "AGENTS.md").write_text(
            "# Project\n\n" + OLD_CORTEX_SECTION + "\n## keep\n\nKeep this.\n",
            encoding="utf-8",
        )
        (self.project_dir / "CLAUDE.md").write_text(
            "# Project\n\n" + OLD_CORTEX_SECTION + "\n## keep\n\nKeep this.\n",
            encoding="utf-8",
        )
        codex_hooks = self.project_dir / ".codex" / "hooks.json"
        claude_settings = self.project_dir / ".claude" / "settings.json"
        codex_hooks.parent.mkdir(parents=True)
        claude_settings.parent.mkdir(parents=True)
        codex_hooks.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"command": "echo cortex"}]}]}}),
            encoding="utf-8",
        )
        claude_settings.write_text(
            json.dumps({"hooks": {"PreToolUse": [{"matcher": "Glob|Grep", "hooks": [{"command": "echo cortex"}]}]}}),
            encoding="utf-8",
        )

        self.assertEqual(uninstall_codex(self.project_dir), {"agents": "removed", "hook": "removed"})
        self.assertEqual(uninstall_claude(self.project_dir), {"claude_md": "removed", "hook": "removed"})

        self.assertNotIn("## cortex", (self.project_dir / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("## cortex", (self.project_dir / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("## keep", (self.project_dir / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("## keep", (self.project_dir / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertNotIn("cortex", codex_hooks.read_text(encoding="utf-8"))
        self.assertNotIn("cortex", claude_settings.read_text(encoding="utf-8"))

    def test_migrate_strips_old_injected_sections_without_touching_hooks(self) -> None:
        (self.project_dir / "AGENTS.md").write_text(
            "# Project\n\n" + OLD_CORTEX_SECTION + "\n## keep\n\nKeep this.\n",
            encoding="utf-8",
        )
        (self.project_dir / "CLAUDE.md").write_text(
            "# Project\n\n" + OLD_CORTEX_SECTION + "\n## keep\n\nKeep this.\n",
            encoding="utf-8",
        )
        hook_path = self.project_dir / ".codex" / "hooks.json"
        hook_path.parent.mkdir(parents=True)
        hook_path.write_text('{"hooks":{"PreToolUse":[{"command":"echo cortex"}]}}', encoding="utf-8")

        result = migrate(self.project_dir)

        self.assertEqual(result["agents"], "removed")
        self.assertEqual(result["claude_md"], "removed")
        self.assertIn("Install the Cortex plugin", result["next_step"])
        self.assertNotIn("## cortex", (self.project_dir / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertNotIn("## cortex", (self.project_dir / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertIn("cortex", hook_path.read_text(encoding="utf-8"))

    def test_cli_exposes_migrate_and_not_install_actions(self) -> None:
        parser = build_parser()

        migrate_args = parser.parse_args(["migrate", str(self.project_dir)])
        self.assertEqual(migrate_args.command, "migrate")

        with self.assertRaises(SystemExit):
            parser.parse_args(["codex", "install", str(self.project_dir)])
        with self.assertRaises(SystemExit):
            parser.parse_args(["claude", "install", str(self.project_dir)])
        with self.assertRaises(SystemExit):
            parser.parse_args(["install", "codex"])
