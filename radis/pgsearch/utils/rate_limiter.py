"""Reactive 429 handling for the embedding gateway.

No proactive gating: requests go straight to the gateway, and a 429 is
answered with exponential backoff seeded by the server's own reported wait
time (Retry-After header or the "Wait Xs" phrasing in the response body).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

import openai

logger = logging.getLogger(__name__)


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
