"""B.1 (0.12.2): `inherits` edges are resolved to `symbol:` ids in the graph, like `calls` edges."""
from __future__ import annotations

import json

import pytest

from cortex.store import CortexStore, default_db_path


@pytest.mark.xfail(strict=True, reason="B.1: inherits targets stay name:")
def test_inherits_resolution_rate(field_shape_repo):
    """Hard gate: at least 80% of the `inherits` edges whose base class is defined in the repository resolve to
    a `symbol:` target on the --field-shape repository (0% in 0.12.1)."""
    nodes, edges = CortexStore(default_db_path(field_shape_repo)).fetch_graph(field_shape_repo)
    class_labels = {node.label for node in nodes if node.kind == "class" and node.granularity == "symbol"}
    in_repo = []
    for edge in edges:
        if edge.relation != "inherits":
            continue
        original = edge.metadata.get("unresolved_target") or edge.target
        label = original.split(":", 1)[1].split("::")[-1]
        if original.startswith("name:") and label in class_labels:
            in_repo.append(edge)
    assert len(in_repo) >= 10, "the profile must contain in-repo bases"
    resolved = [edge for edge in in_repo if edge.target.startswith("symbol:")]
    assert len(resolved) / len(in_repo) >= 0.8, (len(resolved), len(in_repo))


@pytest.mark.xfail(strict=True, reason="B.1: cortex_status has no resolution block")
def test_status_reports_the_resolution_block(field_shape_repo):
    from cortex.mcp.tools import call_tool

    result = call_tool("cortex_status", {"repo_path": str(field_shape_repo), "response_format": "detailed"})
    payload = json.loads(result["content"][0]["text"])

    resolution = payload["resolution"]
    assert set(resolution["calls"]) == {"symbol", "name", "external"}
    assert set(resolution["inherits"]) == {"symbol", "name", "module"}
    assert resolution["inherits"]["symbol"] > 0 and resolution["calls"]["symbol"] > 0
