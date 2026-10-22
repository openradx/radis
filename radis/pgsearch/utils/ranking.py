"""The window re-rank of full-text mode, as a pure function over ranked lists."""

from collections.abc import Mapping, Sequence


def min_max(values: Mapping[int, float]) -> dict[int, float]:
    """Scale ``values`` into 0..1. A constant input (one value, or equal values)
    scales to 1.0 everywhere, so a lone hit is not punished for having no peers."""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi == lo:
        return {key: 1.0 for key in values}
    return {key: (value - lo) / (hi - lo) for key, value in values.items()}


def rerank_window(
    hits: Sequence[tuple[int, float]],
    similarity_by_id: Mapping[int, float],
    window: int,
    weight: float,
) -> list[tuple[int, float]]:
    """Order ``hits``, ``(report_id, ts_rank)`` pairs in keyword order, for the page.

    The measured window hits (the first ``window`` hits that have a similarity)
    come first, ordered by ``weight * norm(similarity) + (1 - weight) *
    norm(ts_rank)`` with both signals min-max scaled over those hits, ties broken
    by ts_rank then id. Every other hit follows in keyword order with its ts_rank
    as score: the hits beyond the window, and the window hits that have no
    similarity because they are not embedded yet.
    """
    measured = [(rid, rank) for rid, rank in hits[:window] if rid in similarity_by_id]
    similarity_norm = min_max({rid: similarity_by_id[rid] for rid, _ in measured})
    rank_norm = min_max({rid: rank for rid, rank in measured})
    scored = [
        (rid, weight * similarity_norm[rid] + (1.0 - weight) * rank_norm[rid], rank)
        for rid, rank in measured
    ]
    scored.sort(key=lambda item: (-item[1], -item[2], item[0]))
    measured_ids = {rid for rid, _ in measured}
    ordered = [(rid, score) for rid, score, _ in scored]
    ordered.extend((rid, rank) for rid, rank in hits if rid not in measured_ids)
    return ordered
