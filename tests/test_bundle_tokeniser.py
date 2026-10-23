"""Tokeniser fixes of the 0.13.0 field review, section F cause 8 (docs/plan-0.14-field-review.md, Task 5.5)."""
from __future__ import annotations

import pytest

from cortex import bundle
from cortex.bundle import _split_identifier, _tokenize_query, _tokenize_text


# --- 1. a lower-case prefix plus an `OS` run is one word


@pytest.mark.parametrize("token,expected", [("iOS", "ios"), ("macOS", "macos"), ("tvOS", "tvos"), ("watchOS", "watchos")])
def test_an_os_name_is_kept_whole(token, expected):
    assert _split_identifier(token) == (expected,)
    assert _tokenize_text(f"runs on {token} only") >= {expected}
    assert "os" not in _tokenize_text(f"runs on {token} only")


def test_other_camel_case_identifiers_still_split():
    assert _split_identifier("getURL") == ("get", "url")  # an acronym after a word is a real boundary
    assert _split_identifier("deviceOsInfo") == ("device", "os", "info")
    assert _split_identifier("GizmoPods") == ("gizmo", "pods")
    assert _split_identifier("device_list") == ("device", "list")
    assert _split_identifier("OS") == ("os",)


# --- 2. instruction words are stop words on the query side only


INSTRUCTION_WORDS = "find then very all after because every too shows show come back during handles handle path re".split()


def test_instruction_words_are_dropped_from_the_query_but_not_from_text():
    task = " ".join(INSTRUCTION_WORDS) + " gizmo"
    assert _tokenize_query(task) == {"gizmo"}
    assert set(INSTRUCTION_WORDS) <= _tokenize_text(task)  # file text and path aliases keep them


def test_a_hyphenated_re_prefix_does_not_become_a_term():
    assert _tokenize_query("re-enumerates on replug") == {"enumerates", "replug"}


# --- 3. stemming, for matching only


@pytest.mark.parametrize(
    "term,other",
    [
        ("gizmos", "gizmo"),
        ("installs", "install"),
        ("removed", "remove"),
        ("removing", "remove"),
        ("removes", "remove"),
        ("reported", "report"),
        ("entries", "entry"),
        ("caches", "cache"),
        ("matches", "match"),
        ("settings", "setting"),
        ("handled", "handle"),
        ("handles", "handle"),
    ],
)
def test_inflected_forms_share_a_stem(term, other):
    assert bundle._stem(term) == bundle._stem(other)


@pytest.mark.parametrize("term", ["status", "class", "analysis", "bus", "is", "ios", "its", "red", "sing", "string", "usb", "ids", "x86", "c++"])
def test_short_or_ambiguous_terms_are_left_alone(term):
    assert bundle._stem(term) == term


def test_tokenize_text_output_does_not_change():
    assert _tokenize_text("Gizmos removed installing entries") == {"gizmos", "removed", "installing", "entries"}
    assert _tokenize_text("Gizmos removed installing entries") == {"gizmos", "removed", "installing", "entries"}
    assert bundle._tokenize_match("Gizmos removed installing entries") == {"gizmo", "remov", "install", "entry"}


def test_stemmed_task_terms_match_stemmed_text_in_the_score():
    terms, _concat = bundle._match_terms("gizmos removed")
    plain = bundle._score_text({"gizmos", "removed"}, "the gizmo was remove") 
    stemmed = bundle._score_text(terms, "the gizmo was remove")
    assert plain == 0 and stemmed == 20


# --- 4. adjacent query parts, concatenated


def test_adjacent_query_parts_add_their_concatenation_as_a_term():
    terms, concat = bundle._match_terms("GizmoPods hang")
    assert {"gizmo", "pod", "hang"} <= terms
    assert "gizmopod" in terms  # `Gizmopod` in the code is one token
    assert concat["gizmopod"] == ("gizmo", "pod")


def test_a_concatenation_is_weighted_like_its_rarer_part():
    class Source:
        def __init__(self, path, content):
            self.path, self.content = path, content

    sources = [Source(f"f{i}.py", "common " + ("rare" if i < 3 else "")) for i in range(10)]
    docs = [bundle._tokenize_match(f"{s.path}\n{s.content}") for s in sources]
    weights = bundle._term_weights({"common", "rare", "commonrare"}, sources, docs, {"commonrare": ("common", "rare")})
    assert weights["commonrare"] == weights["rare"] > weights["common"]
    assert bundle._term_weights({"common", "rare", "commonrare"}, sources, docs)["commonrare"] != weights["rare"]
