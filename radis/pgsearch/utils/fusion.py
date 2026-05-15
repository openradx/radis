def rrf_fuse(
    vec_rank: dict[int, int],
    fts_rank: dict[int, int],
    k: int,
) -> list[int]:
    """Reciprocal Rank Fusion.

    vec_rank and fts_rank map report_id -> 1-based rank position in each retriever.
    Returns report ids ordered by descending RRF score, with stable id tiebreak.
    """
    all_ids = set(vec_rank) | set(fts_rank)

    def score(rid: int) -> float:
        s = 0.0
        if rid in vec_rank:
            s += 1.0 / (k + vec_rank[rid])
        if rid in fts_rank:
            s += 1.0 / (k + fts_rank[rid])
        return s

    return sorted(all_ids, key=lambda rid: (-score(rid), rid))


def summary_with_fallback(body: str, summary: str, max_words: int) -> str:
    """SearchHeadline returns '' for documents that don't match the tsquery
    (e.g., vector-only hits). Fall back to the first `max_words` words of the body."""
    if summary:
        return summary
    words = body.split()
    return " ".join(words[:max_words])
