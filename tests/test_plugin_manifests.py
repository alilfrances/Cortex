from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginManifestTests(unittest.TestCase):
    def _load_json(self, path: str) -> dict[str, object]:
        return json.loads((ROOT / path).read_text(encoding="utf-8"))

    def test_plugin_json_files_parse(self) -> None:
        for path in (
            ".claude-plugin/plugin.json",
            ".codex-plugin/plugin.json",
            ".mcp.json",
            ".claude-plugin/marketplace.json",
            "hooks/hooks.json",
        ):
            self.assertIsInstance(self._load_json(path), dict)

    def test_manifest_versions_match_pyproject(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]

        self.assertEqual(self._load_json(".claude-plugin/plugin.json")["version"], version)
        self.assertEqual(self._load_json(".codex-plugin/plugin.json")["version"], version)
        marketplace = self._load_json(".claude-plugin/marketplace.json")
        self.assertEqual(marketplace["version"], version)
        for plugin in marketplace["plugins"]:
            self.assertEqual(plugin["version"], version)

        from cortex.mcp.server import SERVER_INFO

        self.assertEqual(SERVER_INFO["version"], version)

    def test_codex_manifest_references_skills_and_mcp_config(self) -> None:
        manifest = self._load_json(".codex-plugin/plugin.json")
        mcp = self._load_json(".mcp.json")

        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(
            mcp,
            {
                "mcpServers": {
                    "cortex": {
                        "command": "python3",
                        "args": ["${CLAUDE_PLUGIN_ROOT}/bin/cortex-mcp.py"],
                    }
                }
            },
        )

    def test_claude_plugin_wires_session_start_hook(self) -> None:
        # hooks.json is the single source of hook wiring; defining hooks in
        # plugin.json too causes duplicate SessionStart runs (fixed in 0.2.1).
        manifest = self._load_json(".claude-plugin/plugin.json")
        self.assertNotIn("hooks", manifest)

        hook_config = self._load_json("hooks/hooks.json")
        expected_command = 'python3 "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-.}}/hooks/session-start.py"'
        command = hook_config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertEqual(command, expected_command)

        pre_tool = hook_config["hooks"]["PreToolUse"]
        self.assertEqual(pre_tool[0]["matcher"], "Read|Grep|Glob")
        pre_command = pre_tool[0]["hooks"][0]
        self.assertEqual(
            pre_command["command"],
            'python3 "${CLAUDE_PLUGIN_ROOT:-${PLUGIN_ROOT:-.}}/hooks/pre-tool-use.py"',
        )
        self.assertEqual(pre_command["timeout"], 5)
        self.assertTrue((ROOT / "hooks" / "pre-tool-use.py").stat().st_mode & 0o111)

    def test_mcp_launcher_needs_no_pip_install(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "cortex-mcp.py")],
            input='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
            capture_output=True,
            text=True,
            timeout=10,
            cwd=ROOT,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout.splitlines()[0])
        self.assertEqual(response["id"], 1)
        self.assertIn("serverInfo", response["result"])
