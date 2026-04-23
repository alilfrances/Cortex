from __future__ import annotations

from collections import Counter
from pathlib import Path

from .gitutils import discover_repo_root
from .store import CortexStore, default_db_path


def default_report_path(repo_path: Path) -> Path:
    repo_root = repo_path.resolve()
    return repo_root / ".cortex" / "cortex_report.md"


def generate_report(repo_path: Path, db_path: Path | None = None, out_dir: Path | None = None) -> str:
    repo_root = discover_repo_root(repo_path)
    store = CortexStore(db_path or default_db_path(repo_root))
    nodes, edges = store.fetch_graph(repo_root)
    degree = Counter()
    for edge in edges:
        degree[edge.source] += 1
        degree[edge.target] += 1

    top_nodes = sorted(nodes, key=lambda node: (-degree[node.node_id], node.node_id))[:5]
    weak_nodes = [node for node in nodes if degree[node.node_id] <= 1][:5]

    lines = [
        f"# Cortex Report: {repo_root.name}",
        "",
        f"- Nodes: {len(nodes)}",
        f"- Edges: {len(edges)}",
        "",
        "## Central Nodes",
    ]
    lines.extend(f"- {node.label} ({node.kind}) — degree {degree[node.node_id]}" for node in top_nodes)
    lines.extend(["", "## Weak Links"])
    lines.extend(f"- {node.label} ({node.kind})" for node in weak_nodes)
    report = "\n".join(lines).strip()

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "cortex_report.md").write_text(report, encoding="utf-8")
    return report


def write_report(repo_path: Path, db_path: Path | None = None) -> Path:
    repo_root = discover_repo_root(repo_path)
    report_path = default_report_path(repo_root)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(generate_report(repo_root, db_path=db_path), encoding="utf-8")
    return report_path
