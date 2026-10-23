"""R2: the file text score is normalised by length only far above the median size (plan Task 5.7, second try)."""
from __future__ import annotations

import pytest

from cortex import bundle
from cortex.bundle import SPAN_FILE_SHARE_CAP, SYMBOL_FILE_SCORE_SHARE, generate_bundle
from tests.bundle_shapes import make_repo

TASK = "reconnect timeout retry session"
WORDS = "reconnect timeout retry session".split()


def _small(k: int) -> str:
    return f"int helper{k}(int value_) {{\n    // {WORDS[k % 4]} handling\n    return value_ + {k};\n}}\n"


def _big(lines: int, prefix: str) -> str:
    """`lines // 4` small functions, each naming every task word in a comment."""
    return "\n".join(
        f"int {prefix}{i}(int value_) {{\n    // {' '.join(WORDS)}\n    return value_ + {i};\n}}\n" for i in range(lines // 4)
    )


def _repo(tmp_path, big_lines: int, big_name: str = "bulky", prefix: str = "bulky"):
    files = {f"src/small_{k}.cpp": _small(k) for k in range(9)}
    files[f"src/{big_name}.cpp"] = _big(big_lines, prefix)
    return make_repo(tmp_path / "repo", files)


def _run(repo, norm: float, monkeypatch, budget: int = 20000) -> dict:
    monkeypatch.setattr(bundle, "LENGTH_NORM", norm)
    return generate_bundle(repo, task=TASK, budget=budget, output_format="json")


def _keyword_score(out: dict, path: str) -> float:
    """The file's score without the graph bonus (which follows the other files' scores)."""
    item = next(i for i in out["items"] if i["path"] == path and i["kind"] == "code")
    return item["score"] - item["metadata"]["graph_bonus"]


def test_a_file_far_above_the_median_has_its_text_score_divided(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 400)  # about 150x the median size
    plain, normed = _run(repo, 0.0, monkeypatch), _run(repo, 0.5, monkeypatch)
    assert _keyword_score(normed, "src/bulky.cpp") < _keyword_score(plain, "src/bulky.cpp") * 0.6
    # the small files are not touched
    assert _keyword_score(normed, "src/small_0.cpp") == pytest.approx(_keyword_score(plain, "src/small_0.cpp"), abs=0.05)


def test_a_file_up_to_the_threshold_times_the_median_is_not_touched(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 12)  # about 3.3x the median size: below LENGTH_NORM_FROM
    plain, normed = _run(repo, 0.0, monkeypatch), _run(repo, 1.0, monkeypatch)
    assert _keyword_score(normed, "src/bulky.cpp") == pytest.approx(_keyword_score(plain, "src/bulky.cpp"), abs=0.05)


def test_the_name_bonus_is_not_divided(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 400, big_name="reconnect")
    plain, normed = _run(repo, 0.0, monkeypatch), _run(repo, 1.0, monkeypatch)
    name_part = bundle.NAME_MATCH_BONUS * bundle.NAME_BONUS_BASE
    assert _keyword_score(normed, "src/reconnect.cpp") >= name_part
    assert _keyword_score(normed, "src/reconnect.cpp") < _keyword_score(plain, "src/reconnect.cpp")


def test_spans_inherit_the_unnormalised_file_score(tmp_path, monkeypatch):
    repo = _repo(tmp_path, 400, prefix="reconnectBulky")

    def spans(norm: float) -> dict[str, float]:
        out = _run(repo, norm, monkeypatch, budget=300)
        return {i["title"]: i["score"] for i in out["items"] if i["kind"] == "symbol"}

    plain, normed = spans(0.0), spans(1.0)
    assert any(title.startswith("src/bulky.cpp") for title in plain), plain
    assert plain == normed  # the span share does not follow the normalisation


def test_the_span_file_share_is_capped(tmp_path, monkeypatch):
    assert SPAN_FILE_SHARE_CAP == 2 * bundle.NAME_MATCH_BONUS
    repo = _repo(tmp_path, 400, prefix="reconnectBulky")

    def top_span(cap: float) -> float:
        monkeypatch.setattr(bundle, "SPAN_FILE_SHARE_CAP", cap)
        out = _run(repo, 0.0, monkeypatch, budget=300)
        return max(i["score"] for i in out["items"] if i["kind"] == "symbol" and i["path"] == "src/bulky.cpp")

    uncapped, capped = top_span(1e9), top_span(4.0)
    assert uncapped - capped == pytest.approx(SYMBOL_FILE_SCORE_SHARE * (_file_score(repo, monkeypatch) - 4.0), abs=0.5)


def _file_score(repo, monkeypatch) -> float:
    out = _run(repo, 0.0, monkeypatch)
    item = next(i for i in out["items"] if i["path"] == "src/bulky.cpp" and i["kind"] == "code")
    return item["score"] - item["metadata"]["graph_bonus"]
