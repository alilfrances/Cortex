# tests/test_smoke.py
"""
Smoke tests: run full pipeline against the Cortex repo itself.
These are integration tests that exercise the real code path end-to-end.
"""
from __future__ import annotations
import tempfile
from pathlib import Path
import pytest
from cortex.ingest import ingest_repository
from cortex.bundle import generate_bundle
from cortex.report import generate_report


CORTEX_ROOT = Path(__file__).resolve().parents[1]


def test_ingest_cortex_repo():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cortex.db"
        result = ingest_repository(CORTEX_ROOT, db_path=db_path)
        assert result["source_count"] > 0
        # AST + co-change nodes should push node_count above source_count
        assert result["node_count"] > result["source_count"]
        assert result["edge_count"] > 0


def test_bundle_cortex_repo():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cortex.db"
        ingest_repository(CORTEX_ROOT, db_path=db_path)
        result = generate_bundle(
            CORTEX_ROOT,
            task="bundle packing graph traversal",
            budget=3000,
            db_path=db_path,
        )
        # bundle.py or graph.py should appear since task mentions "bundle" and "graph"
        assert isinstance(result, str)
        assert len(result) > 0
        assert "bundle" in result.lower() or "graph" in result.lower()


def test_report_cortex_repo():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cortex.db"
        ingest_repository(CORTEX_ROOT, db_path=db_path)
        report = generate_report(CORTEX_ROOT, db_path=db_path)
        assert "God Nodes" in report
        assert "Communities" in report
        assert "Surprising" in report


def test_incremental_ingest_cortex_repo():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cortex.db"
        # Full ingest first
        result1 = ingest_repository(CORTEX_ROOT, db_path=db_path)
        # Incremental — nothing changed, should show all files as unchanged
        result2 = ingest_repository(CORTEX_ROOT, db_path=db_path, incremental=True)
        assert result2["new_files"] == 0
        assert result2["updated_files"] == 0
        assert result2["unchanged_files"] == result1["source_count"]
