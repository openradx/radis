"""Stratified report sampler for the labels evaluation harness.

The default strategy buckets reports by ``study_datetime`` year and samples
each bucket proportional to its share of the corpus, with a small per-year
floor so rare years aren't crowded out. Future strata (modality, study
description) can be layered in by extending :func:`sample_reports` — the
return shape stays the same.

Sampling is deterministic given a seed: the same seed always picks the
same report IDs, so dev workflows can regenerate the eval set if needed.
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Iterable

from django.db.models import QuerySet

from radis.reports.models import Report


def sample_reports(
    target_size: int,
    seed: int | None = None,
    source: QuerySet[Report] | None = None,
    per_year_floor: int = 5,
) -> list[int]:
    """Return a list of report IDs stratified by ``study_datetime`` year.

    If the corpus has fewer reports than ``target_size``, the full corpus
    is returned. ``per_year_floor`` guarantees a minimum sample per non-empty
    year so a dominant year cannot squeeze out coverage of the others; if
    that conflicts with ``target_size`` (very small targets across many
    years), the floor is relaxed and a proportional sample is returned.
    """
    rng = random.Random(seed)

    candidates = (source if source is not None else Report.objects.all()).values_list(
        "id", "study_datetime"
    )
    by_year: dict[int, list[int]] = defaultdict(list)
    for report_id, study_dt in candidates:
        year = study_dt.year if study_dt else 0
        by_year[year].append(report_id)

    total = sum(len(ids) for ids in by_year.values())
    if total == 0:
        return []
    if total <= target_size:
        all_ids = [rid for ids in by_year.values() for rid in ids]
        rng.shuffle(all_ids)
        return all_ids

    years = sorted(by_year.keys())
    num_years = len(years)

    floor = per_year_floor
    while floor * num_years > target_size and floor > 0:
        floor -= 1

    remaining = target_size - floor * num_years
    chosen: list[int] = []
    for year in years:
        bucket = by_year[year][:]
        rng.shuffle(bucket)
        floor_take = min(floor, len(bucket))
        chosen.extend(bucket[:floor_take])

    # Proportional allocation for the remainder, walking buckets in random
    # order so rounding leftovers don't always bias the same year.
    proportional_pool = [
        rid for year in years for rid in by_year[year][floor:]
    ]
    rng.shuffle(proportional_pool)
    chosen.extend(proportional_pool[:remaining])

    return chosen


def estimate_calls(
    sample_size: int,
    active_questions: int,
    modes: Iterable[str],
    cost_per_1k_tokens: float = 0.0,
    avg_tokens_per_call: int = 4000,
) -> dict[str, float | int]:
    """Rough work/cost estimate for a seed run.

    The numbers are intentionally back-of-envelope — purpose is to flag
    ``you are about to spend X dollars`` before kicking off something
    expensive, not to be accurate to the dollar. REASONED counts as 2 calls
    per report (free-form reasoning + structured); other modes count as 1.
    """
    modes_list = list(modes)
    calls_per_report = sum(2 if m == "RE" else 1 for m in modes_list)
    total_calls = sample_size * calls_per_report
    estimated_tokens = total_calls * avg_tokens_per_call
    estimated_cost = (estimated_tokens / 1000.0) * cost_per_1k_tokens
    return {
        "sample_size": sample_size,
        "active_questions": active_questions,
        "modes": modes_list,
        "calls_per_report": calls_per_report,
        "total_calls": total_calls,
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": round(estimated_cost, 2),
    }
