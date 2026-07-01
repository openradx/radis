"""Postgres-backed sliding-window rate limiter for the embedding gateway.

See docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md for
the empirical findings that drove this design (confirmed: a true sliding
window keyed to request send-time, not a fixed window or continuous refill;
cost possibly weighted by request size, exact formula unconfirmed).
"""

from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent

_WINDOW = timedelta(seconds=60)

_CAPACITY_SETTINGS = {
    "embedding_search": "EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE",
    "embedding_background": "EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE",
}


def configured_capacity(bucket: str) -> int:
    return getattr(settings, _CAPACITY_SETTINGS[bucket])


def _now():
    """Seam so tests can inject a controllable clock instead of real time."""
    return timezone.now()


def _sleep(seconds: float) -> None:
    """Seam so tests can intercept waits instead of really blocking."""
    time.sleep(seconds)


def _advisory_lock(bucket: str) -> None:
    """Postgres transaction-scoped advisory lock keyed on the bucket name.

    Serializes concurrent acquisition attempts across every process sharing
    this database (web, llm_worker, embeddings_worker) — released
    automatically when the enclosing transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [bucket])


def _try_acquire_once(bucket: str, weight: int) -> tuple[bool, float]:
    """Single attempt: lock, prune expired rows, sum current weight, and
    either admit (insert a row) or report how long until enough capacity
    frees up. Returns (acquired, wait_seconds); wait_seconds is 0.0 when
    acquired."""
    with transaction.atomic():
        _advisory_lock(bucket)
        now = _now()
        window_start = now - _WINDOW
        EmbeddingRateLimitEvent.objects.filter(
            bucket=bucket, sent_at__lt=window_start
        ).delete()
        current_weight = (
            EmbeddingRateLimitEvent.objects.filter(bucket=bucket).aggregate(total=Sum("weight"))[
                "total"
            ]
            or 0
        )
        if current_weight + weight <= configured_capacity(bucket):
            EmbeddingRateLimitEvent.objects.create(bucket=bucket, sent_at=now, weight=weight)
            return True, 0.0

        oldest = EmbeddingRateLimitEvent.objects.filter(bucket=bucket).order_by("sent_at").first()
        if oldest is None:
            # Only reachable if weight alone exceeds capacity with an empty
            # bucket — nothing to wait on, so retry almost immediately.
            return False, 0.1
        wait_for = (oldest.sent_at + _WINDOW - now).total_seconds()
        return False, max(wait_for, 0.1)


def try_acquire_immediate(bucket: str, weight: int = 1) -> bool:
    """Non-blocking: returns whether capacity was available right now,
    without waiting if not."""
    acquired, _ = _try_acquire_once(bucket, weight)
    return acquired


def acquire_token(bucket: str, weight: int = 1) -> None:
    """Blocking: waits until capacity is available, then admits."""
    while True:
        acquired, wait_for = _try_acquire_once(bucket, weight)
        if acquired:
            return
        _sleep(wait_for)


def acquire_search_priority_token(weight: int = 1) -> None:
    """Search/retrieval acquisition: try the reserved search floor first,
    then opportunistically spill into background's spare capacity, and only
    block (waiting on the search bucket specifically) if both are exhausted.
    Background's own acquisition path never references "embedding_search",
    so it structurally cannot borrow from this reserved floor."""
    if try_acquire_immediate("embedding_search", weight):
        return
    if try_acquire_immediate("embedding_background", weight):
        return
    acquire_token("embedding_search", weight)
