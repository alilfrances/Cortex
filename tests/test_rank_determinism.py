from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SWEEP_SCRIPT = r'''
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

from cortex.models import GraphEdge, GraphNode
from cortex.rank import personalized_pagerank


nodes = [
    GraphNode(node_id=f"file:{name}.py", kind="file", label=name, source_ref=f"{name}.py")
    for name in ["seed", "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
]
edges = [
    GraphEdge("e1", "file:seed.py", "file:alpha.py", "imports", "STRUCTURAL", weight=1.0),
    GraphEdge("e2", "file:seed.py", "file:bravo.py", "imports", "STRUCTURAL", weight=0.999999),
    GraphEdge("e3", "file:alpha.py", "file:charlie.py", "imports", "HEADING", weight=1.000001),
    GraphEdge("e4", "file:bravo.py", "file:delta.py", "imports", "HEADING", weight=1.0),
    GraphEdge("e5", "file:charlie.py", "file:echo.py", "imports", "COCHANGE", weight=0.999999),
    GraphEdge("e6", "file:delta.py", "file:foxtrot.py", "imports", "COCHANGE", weight=1.000001),
    GraphEdge("e7", "file:echo.py", "file:golf.py", "imports", "SEMANTIC", weight=1.0),
    GraphEdge("e8", "file:foxtrot.py", "file:golf.py", "imports", "SEMANTIC", weight=0.999999),
]
scores = personalized_pagerank(
    nodes,
    edges,
    {"file:seed.py": 1.0, "file:alpha.py": 0.999999},
)
print(json.dumps({"keys": list(scores), "scores": {key: value.hex() for key, value in sorted(scores.items())}}, sort_keys=True))
'''


def test_pagerank_is_bit_stable_across_python_hash_seeds() -> None:
    outputs = []
    for seed in ("0", "1", "2", "random"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", SEED_SWEEP_SCRIPT],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())

    assert len(set(outputs)) == 1
    payloads = [json.loads(output) for output in outputs]
    assert all(payload["keys"] == payloads[0]["keys"] for payload in payloads)
