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
        ):
            self.assertIsInstance(self._load_json(path), dict)

    def test_manifest_versions_match_pyproject(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]

        self.assertEqual(self._load_json(".claude-plugin/plugin.json")["version"], version)
        self.assertEqual(self._load_json(".codex-plugin/plugin.json")["version"], version)
        self.assertEqual(self._load_json(".claude-plugin/marketplace.json")["version"], version)

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
