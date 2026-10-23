"""R3: per-file diminishing returns for spans; files without evidence stay out of the leftover budget (Phase 5, hub flood and constructor flood).

The call-link lift is covered by `test_a_hub_constructor_does_not_flood_the_bundle_with_its_helpers` (tests/test_bundle_field_tasks.py)."""
from __future__ import annotations

from cortex import bundle
from cortex.bundle import generate_bundle
from tests.bundle_shapes import make_repo

TASK = "reconnect timeout retry session"


def _hub(count: int) -> str:
    names = ("Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Juliet")
    return "\n".join(f"int reconnect{names[i % 10]}{names[i // 10]}(int value_) {{\n    return value_ + {i};\n}}\n" for i in range(count))


def _spans(out: dict, path: str) -> list[float]:
    return sorted((i["score"] for i in out["items"] if i["kind"] == "symbol" and i["path"] == path), reverse=True)


def test_the_kth_span_of_a_file_scores_decay_to_the_kth_power_of_its_best(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", {"src/hub.cpp": _hub(40), "src/other.cpp": "int unrelated() { return 1; }\n"})
    monkeypatch.setattr(bundle, "SPAN_REPEAT_DECAY", 1.0)
    flat = _spans(generate_bundle(repo, task=TASK, budget=200, output_format="json"), "src/hub.cpp")
    monkeypatch.setattr(bundle, "SPAN_REPEAT_DECAY", 0.5)
    decayed = _spans(generate_bundle(repo, task=TASK, budget=200, output_format="json"), "src/hub.cpp")

    assert len(flat) >= 3 and len(decayed) >= 3, (flat, decayed)
    assert flat[0] == flat[1] == flat[2]  # identical methods tie without the decay
    assert decayed[0] == flat[0]
    assert decayed[1] == flat[1] * 0.5
    assert decayed[2] == flat[2] * 0.25


def test_a_file_without_evidence_does_not_take_the_leftover_budget(tmp_path):
    # Many unrelated files: the graph mass spreads thin over them (below GRAPH_EVIDENCE_MIN each).
    files = {f"src/other{k:03d}.cpp": f"int other{k:03d}(int value_) {{ return value_ * {k}; }}\n" for k in range(80)}
    files["src/reconnect_timeout.cpp"] = "int reconnectTimeout(int value_) { return value_; }\n"
    repo = make_repo(tmp_path / "repo", files)

    out = generate_bundle(repo, task=TASK, budget=4000, output_format="json")
    paths = {i["path"] for i in out["items"] if i["kind"] != "commit"}

    assert paths == {"src/reconnect_timeout.cpp"}, sorted(paths)  # 4000 tokens, and the unrelated files stay out
