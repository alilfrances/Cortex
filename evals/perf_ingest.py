#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

import sys

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cortex.ingest import ingest_repository  # noqa: E402


def run_perf(file_count: int = 2000, max_seconds: float = 1.0) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="cortex-perf-ingest-") as tmp:
        base = Path(tmp)
        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        for index in range(file_count):
            (repo / f"module_{index:04d}.py").write_text(
                f"def function_{index}():\n    return {index}\n",
                encoding="utf-8",
            )

        db_path = base / "cortex.db"
        ingest_repository(repo, commit_limit=0, db_path=db_path)
        changed = repo / f"module_{file_count // 2:04d}.py"
        changed.write_text("def changed_function():\n    return 'changed-size'\n", encoding="utf-8")

        repo_resolved = repo.resolve()
        reads: list[str] = []
        original_read_text = Path.read_text

        def counting_read_text(path: Path, *args, **kwargs):
            resolved = path.resolve()
            if resolved.is_relative_to(repo_resolved):
                reads.append(resolved.relative_to(repo_resolved).as_posix())
            return original_read_text(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", counting_read_text):
            started = time.perf_counter()
            summary = ingest_repository(
                repo,
                commit_limit=0,
                db_path=db_path,
                incremental=True,
            )
            elapsed = time.perf_counter() - started

    expected_read = changed.name
    passed = (
        reads == [expected_read]
        and summary["updated_files"] == 1
        and summary["unchanged_files"] == file_count - 1
        and elapsed < max_seconds
    )
    return {
        "files": file_count,
        "elapsed_seconds": round(elapsed, 4),
        "max_seconds": max_seconds,
        "content_reads": reads,
        "expected_content_reads": [expected_read],
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure Cortex warm single-file incremental ingest")
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--max-seconds", type=float, default=1.0)
    args = parser.parse_args()
    result = run_perf(max(1, args.files), max(0.0, args.max_seconds))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
