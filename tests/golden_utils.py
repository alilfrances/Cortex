"""Deterministic golden-snapshot helpers for the baseline characterization suite.

Goldens live under ``tests/goldens/`` and are checked in.  They are rewritten
only when ``CORTEX_UPDATE_GOLDENS=1`` is set::

    CORTEX_UPDATE_GOLDENS=1 python3 -m pytest tests/test_golden_*.py

A change that intentionally alters a golden must regenerate it in the same
commit and name the file and the reason in ``CHANGELOG.md`` (see
``CONTRIBUTING.md``).

Canonical form
--------------
* ``json.dumps(sort_keys=True, indent=2)`` plus a trailing newline, ``\\n`` EOL,
  UTF-8, ``ensure_ascii=False``.
* Floats rounded to 6 places (recursively).
* Order of lists is preserved by default.  Ranked outputs (impact items,
  search hits, risk files) are behaviour, so silently re-sorting them would hide
  regressions.  Graph snapshots sort explicitly via :func:`canonical_graph`
  (nodes by ``node_id``, edges by ``edge_id``), and callers may opt in to the
  generic id-sort with ``assert_golden(..., sort_lists=True)``.

Scrubbing
---------
:func:`scrub` removes everything that legitimately varies between runs and
machines: absolute repository paths (replaced by ``<repo>``), 40-hex commit
SHAs (``<sha>``) and volatile keys (timestamps, fingerprints, ages, token-ledger
numbers).
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

GOLDEN_ROOT = Path(__file__).resolve().parent / "goldens"

UPDATE_ENV = "CORTEX_UPDATE_GOLDENS"

# Keys dropped wherever they occur.  ``saved_tokens`` is a ledger estimate that
# depends on the active tokenizer; the tokenizer is pinned in the tests that
# produce payload goldens, but the value is bookkeeping, not tool data.
VOLATILE_KEYS = frozenset(
    {
        "index_age_seconds",
        "indexed_at",
        "generated_at",
        "fingerprint",
        "current_fingerprint",
        "saved_tokens",
        "started_at",
        "uptime_seconds",
        "elapsed_seconds",
        # Environment provenance carried in symbol metadata.
        "parser_version",
        "runtime_version",
    }
)

# Metadata keys stripped from extraction snapshots (environment dependent).
EXTRACTION_VOLATILE_METADATA = ("parser_version", "runtime_version")

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")
_SORT_KEYS = ("edge_id", "node_id", "path", "name")


def treesitter_available() -> bool:
    """True when the tree-sitter backend will really be used in this process."""
    if os.environ.get("CORTEX_FORCE_REGEX") == "1":
        return False
    try:
        from cortex.runtime import public_capability

        return bool(public_capability().get("ready"))
    except Exception:
        return False


# Mirrors the TREE_SITTER_ONLY convention in tests/test_structural_dispatch.py,
# and additionally skips when the managed parser runtime is not ready.
TREE_SITTER_ONLY = pytest.mark.skipif(
    not treesitter_available(),
    reason="tree-sitter golden needs the managed runtime and CORTEX_FORCE_REGEX unset",
)


def updating() -> bool:
    return os.environ.get(UPDATE_ENV) == "1"


def force_stdlib_tokenizer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the dependency-free calibrated tokenizer (as ``--stdlib-tokens`` does).

    ``tiktoken`` is optional; token counts (and therefore budget packing and
    ``_meta.saved_tokens`` presence) differ with it installed, so goldens are
    pinned to the stdlib path that CI uses.
    """
    import cortex.tokenizer as tokenizer

    monkeypatch.setattr(tokenizer, "_tiktoken_encoding", None)
    monkeypatch.setattr(tokenizer, "_tiktoken_unavailable", True)
    monkeypatch.setattr(tokenizer, "_force_stdlib_segments", True)


def round_floats(value: Any, places: int = 6) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, places)
        return 0.0 if rounded == 0 else rounded  # normalise -0.0
    if isinstance(value, dict):
        return {key: round_floats(item, places) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_floats(item, places) for item in value]
    return value


def _sort_id(item: dict[str, Any]) -> tuple[str, str]:
    for key in _SORT_KEYS:
        if key in item:
            return (str(item[key]), json.dumps(item, sort_keys=True, default=str))
    return ("", json.dumps(item, sort_keys=True, default=str))


def sort_id_lists(value: Any) -> Any:
    """Recursively sort lists of dicts that carry an id-like key by that key."""
    if isinstance(value, dict):
        return {key: sort_id_lists(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [sort_id_lists(item) for item in value]
        if items and all(isinstance(item, dict) and any(k in item for k in _SORT_KEYS) for item in items):
            items.sort(key=_sort_id)
        return items
    return value


def canonical_json(payload: Any, *, sort_lists: bool = False) -> str:
    value = round_floats(payload)
    if sort_lists:
        value = sort_id_lists(value)
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def scrub(payload: Any, roots: tuple[str | os.PathLike[str], ...] = ()) -> Any:
    """Remove run- and machine-specific content from ``payload``.

    ``roots`` are absolute repository paths (both the literal and the
    ``resolve()``d form are replaced) that become ``<repo>``.
    """
    replacements: list[str] = []
    for root in roots:
        literal = str(root)
        resolved = str(Path(root).resolve())
        for candidate in (resolved, literal):
            if candidate and candidate not in replacements:
                replacements.append(candidate)
    # Longest first so a nested path is never half-replaced.
    replacements.sort(key=len, reverse=True)

    def clean_string(text: str) -> str:
        for root in replacements:
            text = text.replace(root, "<repo>")
        return _SHA_RE.sub("<sha>", text)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: walk(item)
                for key, item in value.items()
                if key not in VOLATILE_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        if isinstance(value, str):
            return clean_string(value)
        return value

    return walk(payload)


def canonical_graph(nodes: list[Any], edges: list[Any], *, drop_metadata: tuple[str, ...] = EXTRACTION_VOLATILE_METADATA) -> dict[str, Any]:
    """``{"nodes": [...], "edges": [...]}`` sorted by id, volatile metadata removed."""

    def dump(item: Any) -> dict[str, Any]:
        data = item.to_dict()
        metadata = dict(data.get("metadata") or {})
        for key in drop_metadata:
            metadata.pop(key, None)
        data["metadata"] = metadata
        return data

    node_dicts = sorted((dump(node) for node in nodes), key=lambda d: (d["node_id"], json.dumps(d, sort_keys=True, default=str)))
    edge_dicts = sorted((dump(edge) for edge in edges), key=lambda d: (d["edge_id"], json.dumps(d, sort_keys=True, default=str)))
    return {"nodes": node_dicts, "edges": edge_dicts}


def shape_of(payload: Any) -> dict[str, Any]:
    """Key-set fingerprint of a tool response: sorted top-level keys plus, for each
    list-valued field, the sorted key set of its first item (``None`` when the
    list is empty or its items are not objects; a list of lists descends one level).  Values are intentionally absent.
    """
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    item_keys: dict[str, Any] = {}
    for key, value in sorted(payload.items()):
        if isinstance(value, list):
            first = value[0] if value else None
            if isinstance(first, list):  # e.g. cortex_path ``paths``: list of hop lists
                first = first[0] if first else None
            item_keys[key] = sorted(first) if isinstance(first, dict) else None
    return {"keys": sorted(payload), "first_item_keys": item_keys}


def assert_golden(relpath: str, payload: Any, *, sort_lists: bool = False) -> None:
    """Compare ``payload`` with ``tests/goldens/<relpath>`` (or rewrite it when updating)."""
    path = GOLDEN_ROOT / relpath
    actual = canonical_json(payload, sort_lists=sort_lists)
    if updating():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != actual.encode("utf-8"):
            path.write_bytes(actual.encode("utf-8"))
        return
    if not path.exists():
        pytest.fail(
            f"golden missing: tests/goldens/{relpath}\n"
            f"Create it with: {UPDATE_ENV}=1 python3 -m pytest tests/test_golden_*.py",
            pytrace=False,
        )
    expected = path.read_bytes().decode("utf-8")
    if expected != actual:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"goldens/{relpath} (checked in)",
                tofile=f"goldens/{relpath} (actual)",
                n=3,
            )
        )
        pytest.fail(
            f"golden mismatch: tests/goldens/{relpath}\n{diff}\n"
            f"If the change is intentional: {UPDATE_ENV}=1 python3 -m pytest tests/test_golden_*.py, "
            "commit the regenerated golden in the same change and add a CHANGELOG.md line naming it and the reason.",
            pytrace=False,
        )
