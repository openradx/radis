import pytest

from radis.pgsearch.utils.fusion import rrf_fuse, summary_with_fallback


def test_rrf_both_sides_have_hits_overlap():
    vec_rank = {1: 1, 2: 2, 3: 3}
    fts_rank = {2: 1, 3: 2, 4: 3}
    # Expected scores (k=60):
    #   1: 1/(60+1)         = 0.01639
    #   2: 1/(61)+1/(61)    = 0.03279
    #   3: 1/(63)+1/(62)    = 0.03200
    #   4: 1/(63)           = 0.01587
    assert [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=60)] == [2, 3, 1, 4]


def test_rrf_disjoint_universes():
    vec_rank = {1: 1}
    fts_rank = {2: 1}
    assert [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=60)] == [1, 2]


def test_rrf_only_fts():
    vec_rank: dict[int, int] = {}
    fts_rank = {10: 1, 20: 2, 30: 3}
    assert [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=60)] == [10, 20, 30]


def test_rrf_only_vec():
    vec_rank = {10: 1, 20: 2, 30: 3}
    fts_rank: dict[int, int] = {}
    assert [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=60)] == [10, 20, 30]


def test_rrf_empty():
    assert rrf_fuse({}, {}, k=60) == []


def test_rrf_tiebreak_by_id():
    # Two ids with identical contributions; smaller id wins.
    vec_rank = {2: 1}
    fts_rank = {1: 1}
    # Both contribute 1/61. Tiebreak by id ascending.
    assert [rid for rid, _ in rrf_fuse(vec_rank, fts_rank, k=60)] == [1, 2]


def test_rrf_returns_scores_descending_with_tiebreak():
    vec_rank = {1: 1}
    fts_rank = {1: 1, 2: 2}
    pairs = rrf_fuse(vec_rank, fts_rank, k=60)
    # id 1: in both, score = 1/61 + 1/61 = 2/61
    # id 2: in fts only, score = 1/62
    assert pairs[0][0] == 1
    assert pairs[1][0] == 2
    assert pairs[0][1] == pytest.approx(2.0 / 61.0)
    assert pairs[1][1] == pytest.approx(1.0 / 62.0)


def test_summary_with_fallback_keeps_nonempty():
    assert summary_with_fallback("any body", "an <em>existing</em> headline", 30) == (
        "an <em>existing</em> headline"
    )


def test_summary_with_fallback_uses_body_head_when_empty():
    body = " ".join(f"word{i}" for i in range(100))
    out = summary_with_fallback(body, "", max_words=5)
    assert out == "word0 word1 word2 word3 word4"


def test_summary_with_fallback_short_body():
    assert summary_with_fallback("only three words here", "", max_words=10) == (
        "only three words here"
    )
