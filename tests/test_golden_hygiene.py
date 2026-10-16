"""Hygiene of the checked-in goldens (Task 0): canonical, scrubbed, and inventoried.

These tests read ``tests/goldens/`` only; they never regenerate anything.  They exist so a
golden cannot quietly pick up a machine path, a real commit SHA, a timestamp, or a
non-canonical byte layout, and so adding/removing a golden is a visible, reviewed change
(update ``EXPECTED_INVENTORY`` in the same commit).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tests.golden_utils import GOLDEN_ROOT, VOLATILE_KEYS, canonical_json

ALL_GOLDENS = sorted(p for p in GOLDEN_ROOT.rglob("*") if p.is_file())

# Counts per category directory (first two path components; ``mcp`` files at its top level
# are counted under ``mcp``).  Update deliberately when a task adds or removes a golden.
EXPECTED_INVENTORY = {
    "evals": 3,
    "extraction/baseline-7b0e6e0": 52,  # frozen pre-calls copy (Task 6); never regenerated
    "extraction/python": 8,
    "extraction/regex": 54,  # +14 calls_cpp (Task 6), +15 calls_js (Task 7)
    "extraction/treesitter": 54,
    "graph": 1,
    "graph/regex": 1,
    "graph/treesitter": 1,
    "mcp": 2,
    "mcp/payloads": 76,
    "mcp/shapes": 30,
}

_MACHINE_PATH_RE = re.compile(r"/Users/|/private/|/var/folders|/tmp/|pytest-of|[A-Za-z]:\\\\Users")
_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
# build_graph goldens use synthetic, obviously fake shas (see baseline_helpers.normalised_commits).
_FAKE_SHA_RE = re.compile(r"\b0{30}[0-9a-f]{10}\b")


def _category(path: Path) -> str:
    parts = path.relative_to(GOLDEN_ROOT).parts
    if parts[0] == "mcp":
        return "mcp" if len(parts) == 2 else "/".join(parts[:2])
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def _dict_keys(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _dict_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _dict_keys(item)


def _strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_golden_inventory() -> None:
    assert ALL_GOLDENS, "tests/goldens is empty"
    counts = Counter(_category(p) for p in ALL_GOLDENS)
    # mcp/payloads/<backend> -> fold into mcp/payloads
    folded: Counter[str] = Counter()
    for key, value in counts.items():
        folded["mcp/payloads" if key.startswith("mcp/payloads") else key] += value

    assert dict(sorted(folded.items())) == EXPECTED_INVENTORY


@pytest.mark.parametrize("path", ALL_GOLDENS, ids=lambda p: p.relative_to(GOLDEN_ROOT).as_posix())
def test_golden_is_canonical_scrubbed_json(path: Path) -> None:
    raw = path.read_bytes()
    assert path.suffix == ".json"
    assert b"\r" not in raw and raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    text = raw.decode("utf-8")
    data = json.loads(text)
    # Re-serialising must reproduce the file byte for byte (sorted keys, 2-space indent,
    # floats rounded to 6 places).
    assert canonical_json(data) == text

    strings = list(_strings(data))
    assert not [s for s in strings if _MACHINE_PATH_RE.search(s)], "machine-specific path leaked"
    rel = path.relative_to(GOLDEN_ROOT).as_posix()
    leaked = [s for s in strings if _SHA_RE.search(_FAKE_SHA_RE.sub("", s))]
    assert not leaked, f"real commit sha leaked in {rel}"

    if rel.startswith(("mcp/payloads", "extraction", "graph", "evals")):
        offenders = sorted(set(_dict_keys(data)) & VOLATILE_KEYS)
        assert not offenders, f"volatile keys present: {offenders}"
