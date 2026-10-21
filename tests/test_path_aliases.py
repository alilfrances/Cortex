"""B.6 (0.12.2): lowercase concatenated file stems (`widgetwaitready.cpp`) are split into words."""
from __future__ import annotations

from cortex.bundle import NAME_MATCH_BONUS, _tokenize_text, generate_bundle
from cortex.ingest import ingest_repository
from cortex.pathalias import camel_parts, parts_from_labels, segment_stem
from cortex.store import CortexStore, default_db_path
from tests.bundle_shapes import make_repo


def test_a_concatenated_stem_is_split_by_the_classes_of_the_file():
    """Hard gate: with the file's class `WidgetWaitReadyStep`, the path yields `wait` and `ready`; the
    unsplit stem stays so an exact-name query still matches."""
    terms = _tokenize_text("src/widgetwaitready.cpp", labels=("WidgetWaitReadyStep",))

    assert {"wait", "ready", "widget", "step"} <= terms
    assert "widgetwaitready" in terms
    assert "widgetwaitready" in _tokenize_text("src/widgetwaitready.cpp")  # and without labels nothing changes
    assert "wait" not in _tokenize_text("src/widgetwaitready.cpp")


def test_a_label_names_the_stem_in_either_direction_and_unrelated_labels_add_nothing():
    assert parts_from_labels("coredevice", ["CoreDevice"]) == ("core", "device")
    assert parts_from_labels("widgetwaitready", ["Widget"]) == ("widget",)  # the label is a prefix of the stem
    assert parts_from_labels("widgetwaitready", ["SessionManager", "Other"]) == ()
    assert parts_from_labels("wid", ["WidgetWaitReadyStep"]) == ()  # too short to be a concatenation
    assert parts_from_labels("Widgetwaitready", ["WidgetWaitReadyStep"]) == ()  # not lower case: already split
    assert camel_parts("WidgetWaitReadyStep") == ("widget", "wait", "ready", "step")


def test_segmentation_covers_the_whole_stem_with_the_fewest_parts_of_three_or_more_characters():
    vocabulary = {"device", "dev", "ice", "pool", "po", "ol", "wait", "ready", "wa", "it"}

    assert segment_stem("devicepool", vocabulary) == ("device", "pool")  # not dev+ice+pool
    assert segment_stem("waitready", vocabulary) == ("wait", "ready")
    assert segment_stem("devicepoolx", vocabulary) is None  # the whole stem must be covered
    assert segment_stem("pool", vocabulary) is None  # a single part is the stem itself
    assert segment_stem("wait", {"wa", "it"}) is None  # parts shorter than three characters are never used
    # equally short splits are broken deterministically: longer leading part first
    assert segment_stem("abcdef", {"abc", "def", "abcd", "ef", "abcdef"}) is None
    assert segment_stem("abcdefg", {"abc", "defg", "abcd", "efg"}) == ("abcd", "efg")


def _step_repo(tmp_path):
    return make_repo(tmp_path / "repo", {
        "src/widgetwaitready.h": "class WidgetWaitReadyStep {\npublic:\n    void run();\n};\n",
        "src/widgetwaitready.cpp": '#include "widgetwaitready.h"\nvoid WidgetWaitReadyStep::run() { int a = 1; }\n',
        # no class named after the stem: the segmentation fallback over the repository's words applies
        "src/pollqueue.cpp": "void drain() { int a = 1; }\n",
        "src/other.h": "class PollGuard {};\nclass QueueGuard {};\nclass PollWorker {};\nclass QueueWorker {};\n",
        "docs/notes.md": "plain notes\n",
    })


def _aliases(repo):
    nodes, _edges = CortexStore(default_db_path(repo)).fetch_graph(repo)
    return {n.source_ref: n.metadata.get("path_aliases") for n in nodes if n.kind == "file"}


def test_ingest_stores_the_aliases_on_the_file_nodes(tmp_path):
    aliases = _aliases(_step_repo(tmp_path))

    assert aliases["src/widgetwaitready.h"] == ["ready", "step", "wait", "widget"]
    assert aliases["src/widgetwaitready.cpp"] == ["ready", "step", "wait", "widget"]  # from the `Cls::` qualifier
    assert aliases["src/pollqueue.cpp"] == ["poll", "queue"]  # fallback segmentation
    assert aliases["docs/notes.md"] is None and aliases["src/other.h"] is None


def test_the_aliases_give_a_path_name_hit(tmp_path):
    """`wait` appears in the file only inside the class name, never as a symbol of the .cpp: the path hit is
    what lifts it above NAME_MATCH_BONUS."""
    repo = _step_repo(tmp_path)

    bundle = generate_bundle(repo, task="wait", budget=4000, output_format="json")

    scores = {item["path"]: item["score"] for item in bundle["items"] if item["kind"] == "code"}
    assert scores["src/widgetwaitready.cpp"] >= NAME_MATCH_BONUS


def test_incremental_ingest_agrees_with_a_full_one(tmp_path, monkeypatch):
    repo = _step_repo(tmp_path)
    (repo / "src/other.h").write_text(
        "class PollGuard {};\nclass QueueGuard {};\nclass PollWorker {};\nclass QueueWorker {};\n"
        "class DrainGuard {};\nclass DrainWorker {};\n"
    )
    (repo / "src/drainqueue.cpp").write_text("void go() { int a = 1; }\n")
    ingest_repository(repo)  # incremental: the vocabulary grew, a new stem appears

    incremental = _aliases(repo)
    monkeypatch.setenv("CORTEX_DATA_DIR", str(tmp_path / "fresh-data"))
    ingest_repository(repo, incremental=False)
    full = _aliases(repo)

    assert incremental == full
    assert incremental["src/drainqueue.cpp"] == ["drain", "queue"]
