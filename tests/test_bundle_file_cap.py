"""B.7 (0.12.2): one keyword-dense file may not take the whole bundle while other matching files wait."""
from __future__ import annotations

from cortex.bundle import FILE_SPAN_SHARE, SPANS_BEFORE_CAP, generate_bundle
from tests.bundle_shapes import make_repo

TASK = "reconnect device timeout"


def _files() -> dict[str, str]:
    body = "\n".join(f"    int step{i} = value_ + {i};  // reconnect device timeout" for i in range(8))
    dense = "".join(
        f"int Dense::reconnectDevice{n}(int value_) {{\n{body}\n    return value_;\n}}\n\n" for n in range(14)
    )
    files = {"src/dense.cpp": dense}
    for k in range(5):
        pad = "\n".join(f"    int pad{i} = value_ + {i};" for i in range(40))
        files[f"src/helper_{k}.cpp"] = (
            f"int helper{k}(int value_) {{\n    // reconnect\n    value_ += {k};\n{pad}\n    return value_;\n}}\n"
        )
    return files


def _span_tokens(bundle: dict) -> dict[str, int]:
    tokens: dict[str, int] = {}
    for item in bundle["items"]:
        if item["kind"] == "symbol":
            tokens[item["path"]] = tokens.get(item["path"], 0) + item["token_count"]
    return tokens


def test_a_dense_file_leaves_room_for_the_other_matching_files(tmp_path):
    repo = make_repo(tmp_path / "repo", _files())

    bundle = generate_bundle(repo, task=TASK, budget=1200, output_format="json")

    paths = {item["path"] for item in bundle["items"]}
    assert {f"src/helper_{k}.cpp" for k in range(5)} <= paths, sorted(paths)
    dense = [i for i in bundle["items"] if i["path"] == "src/dense.cpp" and i["kind"] == "symbol"]
    assert len(dense) >= SPANS_BEFORE_CAP  # its first spans are never held back
    assert bundle["total_tokens"] <= 1200


def test_the_cap_is_released_when_nothing_else_is_waiting(tmp_path):
    files = {"src/dense.cpp": _files()["src/dense.cpp"]}
    repo = make_repo(tmp_path / "repo", files)

    bundle = generate_bundle(repo, task=TASK, budget=1200, output_format="json")

    spans = _span_tokens(bundle).get("src/dense.cpp", 0)
    assert spans > FILE_SPAN_SHARE * 1200  # a lone file still fills the bundle
    assert bundle["total_tokens"] <= 1200


def test_the_span_share_shrinks_with_the_number_of_matching_files():
    from cortex.bundle import file_span_share

    assert file_span_share(1) == 1.0  # a lone match keeps the whole budget
    assert file_span_share(2) == 0.5
    assert file_span_share(3) == 1 / 3
    assert file_span_share(4) == FILE_SPAN_SHARE  # today's quarter from four files on ...
    assert file_span_share(8) == 1 / 8  # ... and a smaller share as more files match
    assert file_span_share(500) == 0.1  # floor


def test_many_matching_files_shrink_what_a_dense_file_may_take_first(tmp_path):
    files = _files()
    for k in range(5, 17):
        pad = "\n".join(f"    int pad{i} = value_ + {i};" for i in range(40))
        files[f"src/helper_{k}.cpp"] = (
            f"int helper{k}(int value_) {{\n    // reconnect\n    value_ += {k};\n{pad}\n    return value_;\n}}\n"
        )
    repo = make_repo(tmp_path / "repo", files)

    bundle = generate_bundle(repo, task=TASK, budget=3000, output_format="json")

    helpers = {item["path"] for item in bundle["items"] if item["path"].startswith("src/helper_")}
    assert len(helpers) == 17, sorted(helpers)  # a quarter of the budget for the dense file left 5 of them out
    assert bundle["total_tokens"] <= 3000
