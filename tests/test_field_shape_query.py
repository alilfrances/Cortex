"""0.12.2 acceptance on the --field-shape repository: a T1-shaped query reaches the second manager and an override."""
from __future__ import annotations

import re

import pytest

from cortex.bundle import generate_bundle
from cortex.mcp.tools import _bundle_why

# "re-attach of a peripheral demotes its entry, then removes it": the manager's `onReattached` checks the
# device, `handleEvent` (no task word in its name) calls the second manager, and the checks go through a
# `CoreDevice *` whose overrides live in the derived classes.
T1_LIKE = "a device is reattached and the manager waits for it to become ready, then resets it"
ACCESSOR = re.compile(r"^(get|set|is|has)[A-Z]|^(name|id|sessionCount)$|Changed$")
TASK_WORDS = ("device", "reattach", "manager", "wait", "ready", "reset")


def _bundle(repo, budget=2500):
    return generate_bundle(repo, task=T1_LIKE, budget=budget, output_format="json")


@pytest.mark.xfail(strict=True, reason="B.2 + B.3: the second manager and an override are not reached")
def test_t1_like_query_packs_both_managers_and_an_override(field_shape_repo):
    bundle = _bundle(field_shape_repo)

    paths = {item["path"] for item in bundle["items"]}
    assert "src/field/devicemanager.cpp" in paths
    assert "src/field/sessionmanager.cpp" in paths
    overridden = [
        item for item in bundle["items"]
        if any(entry["type"] == "override" for entry in _bundle_why(item, set(), set(), []))
    ]
    assert overridden, "an overriding derived or intermediate file must be packed with why type override"
    assert all(item["path"].startswith("src/field/") for item in overridden)


@pytest.mark.xfail(strict=True, reason="B.3: accessors are chosen through calls")
def test_t1_like_query_selects_no_accessor_through_calls(field_shape_repo, monkeypatch):
    import cortex.bundle as bundle_module

    selected: dict[str, tuple] = {}
    original = bundle_module._callee_scores

    def spy(*args, **kwargs):
        selected.update(original(*args, **kwargs))
        return selected

    monkeypatch.setattr(bundle_module, "_callee_scores", spy)
    _bundle(field_shape_repo)

    assert selected, "the query must expand callees"
    names = [symbol_id.rsplit(":", 1)[-1] for symbol_id in selected]
    # `isReady` carries a task word and is the point of the query; `isConnected` or `setName` are not.
    accessors = [name for name in names if ACCESSOR.search(name) and not any(w in name.lower() for w in TASK_WORDS)]
    assert not accessors, names
