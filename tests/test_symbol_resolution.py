from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cortex.ingest import ingest_repository
from cortex.mcp import tools

FILES = {
    "src/mod1.hpp": "class Mod1 {\npublic:\n    int m3(int x);\n    int other(int x);\n};\n",
    "src/mod1.cpp": '#include "mod1.hpp"\nint Mod1::m3(int x) { return x + 1; }\nint Mod1::other(int x) { return x; }\n',
    "src/mod2.cpp": "class Mod2 {\npublic:\n    int m3(int x) { return x; }\n};\n",
    "src/helper_one.py": "def helper_one():\n    return 1\n",
    "src/helper_two.py": "def helper_two():\n    return 2\n",
    "src/Svc.java": "public class Svc {\n    public void run() { }\n}\n",
}


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CORTEX_AUTO_REFRESH", "0")
    root = tmp_path / "repo"
    for rel, content in FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i"], cwd=root, check=True)
    ingest_repository(root)
    return root


def _read(repo: Path, symbol: str) -> dict:
    result = tools.call_tool("cortex_read_symbol", {"repo_path": str(repo), "symbol": symbol})
    return json.loads(result["content"][0]["text"])


def _resolved_id(payload: dict) -> str | None:
    node = payload.get("symbol") if isinstance(payload.get("symbol"), dict) else payload
    return node.get("node_id") if isinstance(node, dict) else None


def test_a_nonsense_name_is_not_found_with_suggestions_instead_of_loose_matches(repo):
    payload = _read(repo, "DefinitelyNotHelperXyz")
    assert payload["error"] == "symbol_not_found"
    assert "matches" not in payload
    assert payload["did_you_mean"], "suggests near names"


@pytest.mark.parametrize("spelling", ["src/mod1.cpp:m3", "src/mod1.cpp::m3"])
def test_path_qualified_names_resolve(repo, spelling):
    payload = _read(repo, spelling)
    assert "error" not in payload and "matches" not in payload, payload
    assert "src/mod1.cpp" in json.dumps(payload)


@pytest.mark.parametrize("spelling", ["Mod1::m3", "Mod1.m3"])
def test_class_qualified_names_pick_the_right_class(repo, spelling):
    payload = _read(repo, spelling)
    assert "matches" not in payload, payload
    assert "src/mod1.cpp" in json.dumps(payload)
    assert "src/mod2.cpp" not in json.dumps(payload)


def test_in_class_definitions_are_found_by_span(repo):
    payload = _read(repo, "Mod2::m3")
    assert "matches" not in payload, payload
    assert "src/mod2.cpp" in json.dumps(payload)


def test_a_bare_name_with_several_owners_stays_ambiguous(repo):
    payload = _read(repo, "m3")
    assert len(payload["matches"]) >= 2


def test_a_partial_but_related_name_is_still_ambiguous_not_missing(repo):
    payload = _read(repo, "helper")
    assert "matches" in payload and {m["label"] for m in payload["matches"]} >= {"helper_one", "helper_two"}


def test_cortex_path_accepts_a_class_qualified_name(repo):
    result = tools.call_tool(
        "cortex_path", {"repo_path": str(repo), "symbol_a": "Mod1::m3", "symbol_b": "Mod1::other"}
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload.get("error") != "symbol_not_found"
    assert "ambiguous" not in payload


def test_prefer_definition_picks_the_implementation_over_header_declarations():
    class N:
        def __init__(self, source_ref: str, label: str = "run") -> None:
            self.source_ref, self.label = source_ref, label

    header, impl = N("a/x.hpp"), N("a/x.cpp")
    assert tools._prefer_definition([header, impl]) == [impl]
    headers = [header, N("b/y.hpp")]
    assert tools._prefer_definition(headers) == headers
    two_impl = [impl, N("b/y.cpp")]
    assert tools._prefer_definition(two_impl) == two_impl
