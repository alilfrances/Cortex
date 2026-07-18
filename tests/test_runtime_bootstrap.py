from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from cortex import runtime


def test_runtime_lock_copies_match():
    repository_lock = Path(__file__).resolve().parents[1] / "runtime" / "runtime-lock.json"
    assert json.loads(repository_lock.read_text(encoding="utf-8")) == json.loads(runtime.LOCK_PATH.read_text(encoding="utf-8"))


def test_runtime_lock_has_wheels_and_parser_bundles_for_documented_platforms():
    lock = runtime._load_lock()
    platforms = {
        "macos-arm64",
        "macos-x86_64",
        "linux-glibc-arm64",
        "linux-glibc-x86_64",
        "windows-arm64",
        "windows-x86_64",
    }
    assert platforms <= set(lock["parser_manifest"]["platforms"])
    for package in lock["packages"]:
        assert platforms <= {artifact["platform"] for artifact in package["artifacts"]}
    assert runtime.PARSER_PLATFORM_ALIASES == {
        "linux-glibc-arm64": "linux-aarch64",
        "linux-glibc-x86_64": "linux-x86_64",
        "windows-arm64": "windows-aarch64",
    }


def test_runtime_status_is_local_and_has_locked_grammars(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.setenv("CORTEX_RUNTIME_DIR", str(tmp_path))
    status = runtime.status()
    assert status["lock_digest"]
    assert set(status["requested_grammars"]) >= {"javascript", "qml"}
    assert not (tmp_path / "ready.json").exists()


def test_runtime_setup_cli_exits_nonzero_when_not_ready(tmp_path):
    env = {
        **os.environ,
        "CORTEX_RUNTIME_DIR": str(tmp_path / "cli-runtime"),
        "CORTEX_RUNTIME_NETWORK": "0",
    }
    completed = subprocess.run(
        [sys.executable, "-m", "cortex", "runtime", "setup", "--force"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["ready"] is False


def test_network_disabled_setup_degrades_without_outside_writes(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.setenv("CORTEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CORTEX_RUNTIME_NETWORK", "0")
    result = runtime.setup(force=True)
    assert result["ready"] is False
    assert result["degraded_reason"]
    assert set((tmp_path / "runtime").iterdir()) <= {tmp_path / "runtime" / "status.json"}


def _fake_ready_runtime(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.delenv("PLUGIN_DATA", raising=False)
    monkeypatch.setenv("CORTEX_RUNTIME_DIR", str(root))
    lock = runtime._load_lock()
    target = runtime._target_path(root, lock)
    site = target / "site"
    cache = target / "parser-cache"
    site.mkdir(parents=True)
    cache.mkdir()
    (site / "payload.py").write_text("trusted = True\n", encoding="utf-8")
    runtime._write_json(
        cache / "manifest.json",
        {"runtime_schema": runtime.RUNTIME_SCHEMA, "downloaded": lock["grammars"]},
    )
    runtime._write_json(
        target / "ready.json",
        {
            "schema_version": runtime.RUNTIME_SCHEMA,
            "runtime_version": lock["runtime_version"],
            "lock_digest": runtime.lock_digest(lock),
            "python_abi": runtime.python_abi(),
            "platform": runtime._platform_key(),
            "downloaded_grammars": lock["grammars"],
            "cache_digest": runtime._tree_digest(cache),
            "site_digest": runtime._tree_digest(site),
        },
    )
    return target


def test_ready_marker_detects_same_size_site_tampering(tmp_path, monkeypatch):
    target = _fake_ready_runtime(tmp_path, monkeypatch)
    assert runtime.status()["ready"] is True

    payload = target / "site" / "payload.py"
    original = payload.read_bytes()
    payload.write_bytes(b"X" * len(original))

    assert runtime.status()["ready"] is False


def test_configure_parser_environment_adds_site_not_runtime_root(tmp_path, monkeypatch):
    target = _fake_ready_runtime(tmp_path, monkeypatch)
    site = str(target / "site")
    old_cache = os.environ.get("CORTEX_PARSER_CACHE")
    old_manifest = os.environ.get("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL")
    try:
        assert runtime.configure_parser_environment() == target
        assert site in sys.path
        assert str(target) not in sys.path
        assert os.environ["TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL"].endswith("/.network-disabled")
    finally:
        while site in sys.path:
            sys.path.remove(site)
        for name, value in (
            ("CORTEX_PARSER_CACHE", old_cache),
            ("TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL", old_manifest),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_stale_setup_lock_is_recovered(tmp_path):
    tmp_path.mkdir(mode=0o700, exist_ok=True)
    lock_path = tmp_path / ".install.lock"
    lock_path.mkdir()
    (lock_path / "owner.json").write_text(
        json.dumps({"pid": 999999, "created_at": time.time() - 3600}),
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(lock_path, (old, old))

    with runtime._lock(tmp_path, timeout=0.05):
        assert lock_path.exists()
    assert not lock_path.exists()


def test_offline_bundle_requires_out_of_band_checksum(tmp_path, monkeypatch):
    monkeypatch.setenv("CORTEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CORTEX_RUNTIME_NETWORK", "0")
    monkeypatch.delenv("CORTEX_RUNTIME_BUNDLE_SHA256", raising=False)
    bundle = tmp_path / "bundle.zip"
    with zipfile.ZipFile(bundle, "w"):
        pass

    result = runtime.setup(force=True, offline_bundle=bundle)

    assert result["ready"] is False
    assert "CORTEX_RUNTIME_BUNDLE_SHA256" in result["degraded_reason"]


def test_runtime_download_rejects_plain_http(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        runtime._download("http://example.com/parser.whl", tmp_path / "parser.whl")


def test_artifact_mirror_preserves_locked_filename(monkeypatch):
    monkeypatch.setenv("CORTEX_RUNTIME_ARTIFACT_MIRROR", "https://mirror.example.invalid/cortex")
    assert runtime._artifact_download_url(
        "https://files.pythonhosted.org/original.whl",
        "tree_sitter-0.26.0-cp311.whl",
    ) == "https://mirror.example.invalid/cortex/tree_sitter-0.26.0-cp311.whl"


def test_public_capability_redacts_runtime_paths_and_error_details():
    payload = runtime.public_capability(
        {
            "ready": False,
            "cache_path": "/private/user/cache",
            "target_path": "/private/user/runtime",
            "degraded_reason": "FileNotFoundError: /private/user/bundle.zip",
        }
    )
    assert "cache_path" not in payload
    assert "target_path" not in payload
    assert payload["degraded_reason"] == "runtime-not-ready"
