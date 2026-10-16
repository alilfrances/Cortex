"""Task 5: resolved Python `calls` edges flow through dead-code, path, references and relations.

One tmp repo, three modules; each consumer is asserted on the cross-module
call that previously stopped at an unresolvable `name:` endpoint.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import cortex.callresolve as callresolve
from cortex.deadcode import analyze_dead_code
from cortex.ingest import ingest_repository
from cortex.mcp.tools import call_tool
from cortex.models import GraphEdge, GraphNode
from cortex.store import CortexStore, default_db_path


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_FINGERPRINT_TTL_SECONDS", "0")
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "billing.py").write_text(
        "def compute_invoice_total(items):\n    return sum(items)\n\n\n"
        "def never_called_anywhere():\n    return 0\n"
    )
    (root / "pkg" / "service.py").write_text(
        "from pkg.billing import compute_invoice_total\n\n\n"
        "def checkout(items):\n    return compute_invoice_total(items)\n"
    )
    (root / "main.py").write_text("from pkg.service import checkout\n\n\ndef cli():\n    return checkout([1])\n")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    ingest_repository(root, commit_limit=10)
    return root


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_cross_module_callee_is_not_a_dead_code_candidate(repo: Path) -> None:
    store = CortexStore(default_db_path(repo))
    findings = {item["symbol"] for item in analyze_dead_code(repo, store=store)["findings"]}
    # `compute_invoice_total` is only called from another module -> credited by the resolved edge.
    assert "compute_invoice_total" not in findings
    assert "checkout" not in findings
    assert "never_called_anywhere" in findings

    detailed = _payload(call_tool("cortex_dead_code", {"repo_path": str(repo), "format": "detailed", "budget": 9999}))
    assert "compute_invoice_total" not in {item["symbol"] for item in detailed["findings"]}


def test_call_edges_target_symbols_and_point_at_the_defining_module(repo: Path) -> None:
    _, edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    targets = {(e.source, e.target) for e in edges if e.relation == "calls"}
    assert ("symbol:pkg/service.py:checkout", "symbol:pkg/billing.py:compute_invoice_total") in targets
    assert ("symbol:main.py:cli", "symbol:pkg/service.py:checkout") in targets
    assert ("symbol:pkg/billing.py:compute_invoice_total", "name:sum") in targets  # builtin stays external


def test_cortex_path_walks_calls_edges_across_modules(repo: Path) -> None:
    payload = _payload(
        call_tool(
            "cortex_path",
            {
                "repo_path": str(repo),
                "symbol_a": "symbol:main.py:cli",
                "symbol_b": "symbol:pkg/billing.py:compute_invoice_total",
                "format": "detailed",
            },
        )
    )
    assert payload["paths"], payload
    shortest = min(payload["paths"], key=len)
    assert [hop["relation"] for hop in shortest] == ["calls", "calls"]
    assert [hop["node_id"] for hop in shortest] == [
        "symbol:pkg/service.py:checkout",
        "symbol:pkg/billing.py:compute_invoice_total",
    ]


def test_cortex_references_sees_the_resolved_incoming_call_edge_and_the_grep_caller(repo: Path) -> None:
    incoming = CortexStore(default_db_path(repo)).query_edges(repo, relation="calls", endpoint_substr="compute_invoice_total", direction="in")
    # The graph tier queries incoming edges by endpoint label: the resolved edge now lands on
    # the definition node (it used to end at an unmatchable `name:` endpoint).
    assert [(e.source, e.target) for e in incoming] == [
        ("symbol:pkg/service.py:checkout", "symbol:pkg/billing.py:compute_invoice_total")
    ]
    payload = _payload(call_tool("cortex_references", {"repo_path": str(repo), "symbol": "compute_invoice_total", "format": "detailed"}))
    hits = payload["items"]["code"]
    graph = {(hit["text"], hit["access"]) for hit in hits if hit["origin"] == "graph"}
    # the resolved call is a graph-tier `read` at the *caller's* file and line; the callee's
    # definition is a `definition` (it used to be reported as a `read` of the definition)
    assert ("pkg/service.py:5", "read") in graph
    assert ("pkg/billing.py:1", "definition") in graph
    assert ("pkg/billing.py:1", "read") not in graph
    # the graph hit covers the call line, so grep does not repeat it
    assert "pkg/service.py:5" not in {hit["text"] for hit in hits if hit["origin"] == "grep"}


def test_cortex_relations_calls_reports_resolved_targets(repo: Path) -> None:
    payload = _payload(call_tool("cortex_relations", {"repo_path": str(repo), "relation": "calls", "symbol": "checkout", "format": "detailed"}))
    targets = {item["target"] for item in payload["items"]}
    assert any(target.startswith("compute_invoice_total @ pkg/billing.py") for target in targets)


def test_resolution_builds_its_indexes_once_not_per_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    builds = 0
    original = callresolve._Index.__init__

    def counting(self, nodes, edges):
        nonlocal builds
        builds += 1
        original(self, nodes, edges)

    monkeypatch.setattr(callresolve._Index, "__init__", counting)

    class ExplodingNodes(list):
        """A second pass over the node list per edge would show up as extra iterations."""

        iterations = 0

        def __iter__(self):
            type(self).iterations += 1
            return super().__iter__()

    nodes = ExplodingNodes(
        GraphNode(f"symbol:m{i}.py:f{i}", "func", f"f{i}", f"m{i}.py", "symbol") for i in range(500)
    )
    edges = [
        GraphEdge(
            f"c{i}", f"symbol:m{i}.py:f{i}", f"name:f{(i + 1) % 500}", "calls", "STRUCTURAL",
            metadata={"callee": f"f{(i + 1) % 500}", "receiver_kind": "none", "source_file": f"m{i}.py"},
        )
        for i in range(500)
    ]
    callresolve.resolve_call_endpoints(nodes, edges)
    assert builds == 1
    assert ExplodingNodes.iterations <= 2  # one index pass (full resolve), independent of the edge count
    assert all(e.target.startswith("symbol:") for e in edges)
