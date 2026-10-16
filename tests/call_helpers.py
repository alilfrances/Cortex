"""Shared helpers for the per-family call-resolution tests (Tasks 6-8)."""

from __future__ import annotations

from pathlib import Path

from cortex.graph import build_graph
from cortex.models import GraphEdge
from tests.baseline_helpers import FIXTURES, fixture_files, read_sources

CONTRACT_KEYS = {
    "lineno", "source_file", "callee", "receiver", "receiver_kind", "scope", "receiver_type", "enclosing_class",
}


def fixture_graph(name: str):
    """Full `build_graph` (extraction + repo-wide resolution) of a fixture directory."""
    root = FIXTURES / name
    return build_graph(read_sources(root, fixture_files(root)), [])


def calls(edges: list[GraphEdge]) -> list[GraphEdge]:
    return [e for e in edges if e.relation == "calls"]


def by_site(edges: list[GraphEdge]) -> dict[tuple[str, int, str], GraphEdge]:
    """`(file, line, callee) -> edge` (the fixtures keep one call per callee per line)."""
    sites: dict[tuple[str, int, str], GraphEdge] = {}
    for edge in calls(edges):
        key = (edge.metadata["source_file"], edge.metadata["lineno"], edge.metadata["callee"])
        assert key not in sites, f"two call edges at one site: {key}"
        sites[key] = edge
    return sites


def assert_resolved(sites, file: str, line: int, callee: str, target: str, tier: str, source: str | None = None) -> GraphEdge:
    edge = sites[(file, line, callee)]
    assert edge.target == target, (file, line, callee, edge.target, edge.metadata)
    assert edge.metadata.get("resolution") == tier, (file, line, callee, edge.metadata)
    assert "ambiguous" not in edge.metadata
    if source is not None:
        assert edge.source == source
    return edge


def assert_unresolved(sites, file: str, line: int, callee: str) -> GraphEdge:
    edge = sites[(file, line, callee)]
    assert edge.target == f"name:{callee}", (file, line, callee, edge.target)
    assert "resolution" not in edge.metadata and "candidates" not in edge.metadata
    return edge


def assert_ambiguous(sites, file: str, line: int, callee: str, candidates: list[str]) -> GraphEdge:
    edge = sites[(file, line, callee)]
    assert edge.target == f"name:{callee}", edge.target
    assert edge.metadata["ambiguous"] is True
    assert edge.metadata["candidates"] == sorted(candidates)
    assert edge.confidence == "LOW"
    assert "resolution" not in edge.metadata
    return edge


def assert_regex_name_only(edges: list[GraphEdge], expected_sites: set[tuple[str, int, str]]) -> None:
    """Forced-regex contract: every call is name-based, LOW, full contract, untyped; and the
    repo-wide resolver left them alone even where a unique definition exists."""
    got = calls(edges)
    for edge in got:
        md = edge.metadata
        assert CONTRACT_KEYS <= set(md), md
        assert edge.confidence == "LOW" and edge.target == f"name:{md['callee']}", edge
        assert md["receiver_kind"] == "none" and md["receiver"] is None
        assert md["receiver_type"] is None and md["scope"] is None and md["enclosing_class"] is None
        assert md["name_only"] is True
        assert not {"resolution", "candidates", "ambiguous"} & set(md)
    sites = {(e.metadata["source_file"], e.metadata["lineno"], e.metadata["callee"]) for e in got}
    missing = expected_sites - sites
    assert not missing, f"regex fallback missed call sites: {sorted(missing)}"
    assert len({e.edge_id for e in got}) == len(got)
