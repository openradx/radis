"""Postgres-backed sliding-window rate limiter for the embedding gateway.

See docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md for
the empirical findings that drove this design (confirmed: a true sliding
window keyed to request send-time, not a fixed window or continuous refill;
cost possibly weighted by request size, exact formula unconfirmed).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta

import openai
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from ..models import EmbeddingRateLimitEvent

logger = logging.getLogger(__name__)

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
    acquired.

    Raises ValueError immediately if `weight` exceeds the bucket's configured
    capacity — such a request could never be admitted, so looping via
    `acquire_token` would spin forever (opening a transaction and taking an
    advisory lock every ~100ms) instead of failing loudly."""
    capacity = configured_capacity(bucket)
    if weight > capacity:
        raise ValueError(f"weight {weight} exceeds capacity {capacity} for bucket {bucket!r}")
    with transaction.atomic():
        _advisory_lock(bucket)
        now = _now()
        window_start = now - _WINDOW
        EmbeddingRateLimitEvent.objects.filter(bucket=bucket, sent_at__lt=window_start).delete()
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
            # Defensive only: with the weight > capacity guard above, an
            # empty bucket can never fail to admit here, so this shouldn't be
            # reachable in practice. Kept as a safety net (e.g. against a
            # capacity setting changing between the guard and this check)
            # rather than trusting that invariant to hold forever — retry
            # almost immediately since there's nothing to wait on.
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


_WAIT_RE = re.compile(r"[Ww]ait (\d+(?:\.\d+)?)s")
_DEFAULT_RETRY_AFTER = 5.0


def parse_retry_after(exc: openai.RateLimitError) -> float:
    """Extract the gateway's own authoritative wait time from a 429: the
    standard HTTP `Retry-After` header first, then the `"Wait Xs"` phrasing
    this specific gateway uses in its response body, then a conservative
    default if neither is present."""
    response = getattr(exc, "response", None)
    if response is not None:
        header = response.headers.get("Retry-After")
        if header is not None:
            try:
                return float(header)
            except ValueError:
                pass
    match = _WAIT_RE.search(str(exc))
    if match:
        return float(match.group(1))
    return _DEFAULT_RETRY_AFTER


def call_with_rate_limit[T](
    acquire_fn: Callable[[], None], fn: Callable[[], T], max_attempts: int = 3
) -> T:
    """Acquire capacity, then call `fn`. If the real gateway still rejects
    with a 429 despite our own gating (our proactive ledger is a best-effort
    estimate — see the design doc on unconfirmed cost-weighting), honor the
    server's own reported wait time and retry, up to `max_attempts`. The
    ledger entry already recorded for the failed attempt is deliberately not
    rolled back: a 429 means the real gateway is more constrained than our
    estimate, so removing our own record would only make future estimates
    more optimistic, the wrong direction."""
    for attempt in range(1, max_attempts + 1):
        acquire_fn()
        try:
            return fn()
        except openai.RateLimitError as exc:
            wait = parse_retry_after(exc)
            logger.warning(
                "embedding rate-limit gate: got 429 despite internal gating; "
                "server-reported wait=%.1fs (attempt %d/%d)",
                wait,
                attempt,
                max_attempts,
            )
            if attempt == max_attempts:
                raise
            _sleep(wait)
    raise AssertionError("unreachable: loop always returns or raises")
