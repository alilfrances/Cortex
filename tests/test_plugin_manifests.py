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
            "marketplace.json",
        ):
            self.assertIsInstance(self._load_json(path), dict)

    def test_manifest_versions_match_pyproject(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = pyproject["project"]["version"]

        self.assertEqual(self._load_json(".claude-plugin/plugin.json")["version"], version)
        self.assertEqual(self._load_json(".codex-plugin/plugin.json")["version"], version)
        self.assertEqual(self._load_json("marketplace.json")["version"], version)

    def test_codex_manifest_references_skills_and_mcp_config(self) -> None:
        manifest = self._load_json(".codex-plugin/plugin.json")
        mcp = self._load_json(".mcp.json")

        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        self.assertEqual(mcp, {"mcpServers": {"cortex": {"command": "cortex", "args": ["mcp"]}}})
