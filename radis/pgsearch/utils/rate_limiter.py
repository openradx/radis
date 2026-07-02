"""Reactive 429 handling for the embedding gateway, shared across processes.

No proactive gating: requests go straight to the gateway. But when any
process receives a 429, the server-reported wait (Retry-After header or the
"Wait Xs" phrasing in the body) is recorded in a shared singleton DB row
(EmbeddingBackoffState) that all background embedding traffic consults
before sending — one process's backoff gates every container, and repeat
429s double the wait globally. The search path deliberately bypasses the
shared pause (a user is waiting) but still records the 429s it receives.
See docs/superpowers/specs/2026-07-02-shared-429-backoff-design.md.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import timedelta

import openai
from django.db import transaction
from django.utils import timezone

from ..models import EmbeddingBackoffState

logger = logging.getLogger(__name__)

_STATE_PK = 1


def _now():
    """Seam so tests can inject a controllable clock instead of real time."""
    return timezone.now()


def _sleep(seconds: float) -> None:
    """Seam so tests can intercept waits instead of really blocking."""
    time.sleep(seconds)


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


def shared_wait_seconds() -> float:
    """Seconds every gated (background) caller must still wait before
    sending, per the shared pause. 0.0 when there is no active pause."""
    state = EmbeddingBackoffState.objects.filter(pk=_STATE_PK).first()
    if state is None or state.paused_until is None:
        return 0.0
    return max((state.paused_until - _now()).total_seconds(), 0.0)


def record_429(retry_after: float) -> float:
    """Record a 429 in the shared state: extend the pause (never shorten —
    concurrent 429s take the max) and bump the global doubling counter.
    Returns the wait this 429 contributed, `retry_after * 2**counter`."""
    with transaction.atomic():
        state, _ = EmbeddingBackoffState.objects.select_for_update().get_or_create(pk=_STATE_PK)
        wait = retry_after * 2**state.consecutive_429s
        candidate = _now() + timedelta(seconds=wait)
        if state.paused_until is None or candidate > state.paused_until:
            state.paused_until = candidate
        state.consecutive_429s += 1
        state.save()
    return wait


def record_success() -> None:
    """Reset the global doubling counter after a gated call succeeded. The
    pause itself is left to expire on its own. Cheap read first so the
    steady-state happy path costs one SELECT and no writes."""
    state = EmbeddingBackoffState.objects.filter(pk=_STATE_PK).first()
    if state is None or state.consecutive_429s == 0:
        return
    EmbeddingBackoffState.objects.filter(pk=_STATE_PK).update(consecutive_429s=0)


def call_with_429_backoff[T](fn: Callable[[], T], max_attempts: int = 3) -> T:
    """Call `fn`; on a 429 wait and retry with exponential backoff, up to
    `max_attempts`. The base wait is the server's own reported wait time
    (see `parse_retry_after`), doubled on each subsequent rejection. The
    final 429 propagates so the caller's task-level retry policy applies."""
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except openai.RateLimitError as exc:
            if attempt == max_attempts:
                raise
            wait = parse_retry_after(exc) * 2 ** (attempt - 1)
            logger.warning(
                "embedding 429 backoff: waiting %.1fs before retry (attempt %d/%d)",
                wait,
                attempt,
                max_attempts,
            )
            _sleep(wait)
    raise AssertionError("unreachable: loop always returns or raises")
