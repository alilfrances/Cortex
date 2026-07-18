#!/usr/bin/env python3
"""Build an attested, current-platform offline Cortex parser bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from cortex import runtime


def _copy_locked_wheels(stage: Path, lock: dict) -> list[dict]:
    wheels = stage / "wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    records = []
    for package in lock.get("packages", []):
        artifact = runtime._artifact_for(lock, package["name"], package.get("version"))
        if artifact is None:
            raise RuntimeError(f"no locked wheel for current platform: {package['name']}")
        destination = wheels / artifact["filename"]
        if not destination.exists():
            runtime._download(runtime._artifact_download_url(artifact["url"], artifact["filename"]), destination)
        runtime._verify_digest(destination, artifact["sha256"])
        records.append({"name": package["name"], "filename": destination.name, "sha256": artifact["sha256"], "size": destination.stat().st_size})
    return records


def build(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    lock = runtime._load_lock()
    target = runtime.target_path()
    if target is None:
        result = runtime.setup(force=True)
        target = runtime.target_path()
        if target is None:
            raise RuntimeError(f"runtime is not ready: {result.get('degraded_reason')}")
    with tempfile.TemporaryDirectory(prefix="cortex-bundle-") as temporary:
        stage = Path(temporary)
        shutil.copy2(runtime.LOCK_PATH, stage / "runtime-lock.json")
        shutil.copytree(target / "parser-cache", stage / "parser-cache")
        wheels = _copy_locked_wheels(stage, lock)
        sbom = {
            "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
            "components": [{"type": "library", "name": p["name"], "version": next(x["version"] for x in lock["packages"] if x["name"] == p["name"]), "hashes": [{"alg": "SHA-256", "content": p["sha256"]}]} for p in wheels],
        }
        (stage / "sbom.cdx.json").write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        spdx = {"spdxVersion": "SPDX-2.3", "dataLicense": "CC0-1.0", "SPDXID": "SPDXRef-DOCUMENT", "name": "cortex-runtime", "packages": [{"SPDXID": "SPDXRef-" + p["name"].replace("-", ""), "name": p["name"], "versionInfo": next(x["version"] for x in lock["packages"] if x["name"] == p["name"]), "checksums": [{"algorithm": "SHA256", "checksumValue": p["sha256"]}]} for p in wheels]}
        (stage / "sbom.spdx.json").write_text(json.dumps(spdx, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (stage / "THIRD_PARTY_NOTICES.txt").write_text("Cortex runtime bundles tree-sitter and tree-sitter-language-pack wheels. See each wheel's embedded license metadata.\n", encoding="utf-8")
        manifest = {
            "schema_version": runtime.RUNTIME_SCHEMA,
            "runtime_version": lock["runtime_version"],
            "lock_digest": runtime.lock_digest(lock),
            "platform": runtime._platform_key(),
            "wheels": wheels,
            "grammars": lock.get("grammars", []),
            "cache_digest": runtime._tree_digest(stage / "parser-cache"),
        }
        (stage / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        name = f"cortex-runtime-{runtime._platform_key() or 'unsupported'}-{lock['runtime_version']}.zip"
        destination = out / name
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(stage.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(stage).as_posix())
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        (destination.with_suffix(destination.suffix + ".sha256")).write_text(f"{digest}  {destination.name}\n", encoding="ascii")
        return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", choices=["current"], default="current")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
