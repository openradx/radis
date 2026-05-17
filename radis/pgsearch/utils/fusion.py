def rrf_fuse(
    vec_rank: dict[int, int],
    fts_rank: dict[int, int],
    k: int,
) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion.

    vec_rank and fts_rank map report_id -> 1-based rank position in each retriever.
    Returns (report_id, fused_score) tuples ordered by descending score,
    with stable ascending-id tiebreak.
    """
    all_ids = set(vec_rank) | set(fts_rank)

    def score(rid: int) -> float:
        s = 0.0
        if rid in vec_rank:
            s += 1.0 / (k + vec_rank[rid])
        if rid in fts_rank:
            s += 1.0 / (k + fts_rank[rid])
        return s

    scored = [(rid, score(rid)) for rid in all_ids]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored


def summary_with_fallback(body: str, summary: str, max_words: int) -> str:
    """SearchHeadline returns '' for documents that don't match the tsquery
    (e.g., vector-only hits). Fall back to the first `max_words` words of the body."""
    if summary:
        return summary
    words = body.split()
    return " ".join(words[:max_words])
