from __future__ import annotations

from cortex.fusion import rrf_fuse


def test_rrf_fuse_empty_lists_returns_empty_dict():
    assert rrf_fuse([]) == {}
    assert rrf_fuse([[], []]) == {}


def test_rrf_fuse_single_list_orders_by_reciprocal_rank():
    scores = rrf_fuse([["a", "b", "c"]], k=60)
    assert scores["a"] > scores["b"] > scores["c"]
    assert scores["a"] == 1.0 / 61
    assert scores["b"] == 1.0 / 62
    assert scores["c"] == 1.0 / 63


def test_rrf_fuse_item_in_multiple_lists_outranks_single_list_item():
    # "shared" is rank 2 in both lists; "only_a" is rank 1 in list a only.
    # Multi-list membership should let a lower-ranked-but-repeated item
    # out-score a single top rank in exactly one list.
    list_a = ["only_a", "shared", "z"]
    list_b = ["other_b", "shared", "y"]
    scores = rrf_fuse([list_a, list_b])
    assert scores["shared"] > scores["only_a"]
    assert scores["shared"] > scores["other_b"]


def test_rrf_fuse_is_commutative_in_list_order():
    list_a = ["a", "b", "c"]
    list_b = ["b", "c", "d"]
    forward = rrf_fuse([list_a, list_b])
    backward = rrf_fuse([list_b, list_a])
    assert forward == backward


def test_rrf_fuse_duplicate_id_within_one_list_counts_only_best_rank():
    scores_with_dup = rrf_fuse([["a", "b", "a", "c"]])
    scores_without_dup = rrf_fuse([["a", "b", "c"]])
    assert scores_with_dup == scores_without_dup


def test_rrf_fuse_accepts_n_lists_generically():
    lists = [["x", "y"], ["y", "z"], ["z", "x"], ["x"]]
    scores = rrf_fuse(lists)
    # x appears in 3 lists (rank 1, rank 2, rank 1) -- should score highest.
    assert scores["x"] > scores["y"]
    assert scores["x"] > scores["z"]


def test_rrf_fuse_default_k_is_60():
    scores = rrf_fuse([["only"]])
    assert scores["only"] == 1.0 / 61


def test_rrf_fuse_custom_k_changes_scale_but_not_ordering():
    list_a = ["a", "b", "c"]
    default_scores = rrf_fuse([list_a])
    small_k_scores = rrf_fuse([list_a], k=1)
    assert small_k_scores["a"] > default_scores["a"]
    assert small_k_scores["a"] > small_k_scores["b"] > small_k_scores["c"]
