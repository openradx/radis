import pytest

from radis.pgsearch.utils.ranking import min_max, rerank_window

HITS = [(1, 0.9), (2, 0.5), (3, 0.1), (4, 0.05), (5, 0.01)]


def _order(pairs):
    return [rid for rid, _ in pairs]


def test_min_max_scales_into_the_unit_interval():
    assert min_max({1: 0.1, 2: 0.3, 3: 0.5}) == {1: 0.0, 2: pytest.approx(0.5), 3: 1.0}


def test_min_max_of_a_constant_input_is_one():
    assert min_max({1: 0.4, 2: 0.4}) == {1: 1.0, 2: 1.0}
    assert min_max({7: 0.0}) == {7: 1.0}
    assert min_max({}) == {}


def test_measured_window_hits_order_by_the_blended_score():
    # norm similarity: 1 -> 0, 2 -> 1, 3 -> 0.571; norm ts_rank: 1 -> 1, 2 -> 0.5, 3 -> 0
    # scores at w=0.7: 1 -> 0.30, 2 -> 0.85, 3 -> 0.40
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    ordered = rerank_window(HITS[:3], sims, window=3, weight=0.7)
    assert _order(ordered) == [2, 3, 1]
    assert dict(ordered)[2] == pytest.approx(0.85)
    assert dict(ordered)[1] == pytest.approx(0.30)


def test_weight_one_is_pure_similarity_order():
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    assert _order(rerank_window(HITS[:3], sims, window=3, weight=1.0)) == [2, 3, 1]


def test_weight_zero_keeps_the_keyword_order():
    sims = {1: 0.2, 2: 0.9, 3: 0.6}
    assert _order(rerank_window(HITS[:3], sims, window=3, weight=0.0)) == [1, 2, 3]


def test_hits_beyond_the_window_follow_in_keyword_order_with_their_ts_rank():
    sims = {1: 0.2, 2: 0.9}
    ordered = rerank_window(HITS, sims, window=2, weight=0.7)
    assert _order(ordered) == [2, 1, 3, 4, 5]
    assert ordered[2:] == [(3, 0.1), (4, 0.05), (5, 0.01)]


def test_window_hits_without_a_similarity_follow_the_measured_block_in_keyword_order():
    # Report 1 sits in the window but has no vector: it is not scored and keeps its
    # keyword position relative to the other unscored hits.
    sims = {2: 0.9, 3: 0.6}
    ordered = rerank_window(HITS, sims, window=3, weight=0.7)
    assert _order(ordered) == [2, 3, 1, 4, 5]
    assert dict(ordered)[1] == 0.9


def test_all_unmeasured_is_the_keyword_order():
    assert rerank_window(HITS, {}, window=3, weight=0.7) == HITS


def test_a_window_of_one_scores_one():
    assert rerank_window(HITS[:1], {1: 0.3}, window=1, weight=0.7) == [(1, pytest.approx(1.0))]


def test_ties_break_by_ts_rank_then_id():
    sims = {1: 0.5, 2: 0.5, 3: 0.5}
    ordered = rerank_window([(3, 0.4), (1, 0.4), (2, 0.4)], sims, window=3, weight=0.7)
    assert _order(ordered) == [1, 2, 3]


def test_empty_input():
    assert rerank_window([], {}, window=10, weight=0.7) == []


def test_window_larger_than_the_list():
    sims = {1: 0.1, 2: 0.9}
    assert _order(rerank_window(HITS[:2], sims, window=1000, weight=0.7)) == [2, 1]
