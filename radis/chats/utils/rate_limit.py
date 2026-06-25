import logging
import threading
import time
from collections.abc import Callable

import openai

logger = logging.getLogger(__name__)

# Transient, usually per-request failures (not rate-limits). Worth a small local retry.
TRANSIENT_ERRORS = (openai.APIConnectionError, openai.InternalServerError)


class RateLimited(Exception):
    """A report could not be labeled within its wait budget; defer it."""


class RateLimitGate:
    """Per-process barrier that makes all labeling threads back off together on a 429.

    A 429 from any thread closes the gate for a while; every thread waits behind the
    same window, so the worker stops hammering a provider that is already blocking it.
    """

    def __init__(
        self,
        base_seconds: float,
        fallback_max_seconds: float,
        header_ceiling_seconds: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = base_seconds
        self._fallback_max = fallback_max_seconds  # caps the header-less exponential guess only
        self._header_ceiling = header_ceiling_seconds  # safety rail for an absurd Retry-After
        self._now = now
        self._sleep = sleep
        self._lock = threading.Lock()
        self._blocked_until = 0.0  # monotonic deadline; gate is open when now() >= this
        self._consecutive_429 = 0  # header-less 429s in a row; drives the exponential fallback

    def reset(self) -> None:
        """Clear runtime state. For tests that share the process-global gate."""
        with self._lock:
            self._blocked_until = 0.0
            self._consecutive_429 = 0

    def note_success(self) -> None:
        with self._lock:
            self._consecutive_429 = 0  # provider healthy -> reset the ladder

    def note_rate_limited(self, retry_after: float | None) -> float:
        """Close the gate after a 429. Returns the pause used to arm it."""
        with self._lock:
            if retry_after is not None:
                pause = min(retry_after, self._header_ceiling)
            else:
                self._consecutive_429 += 1
                pause = min(self._base * 2 ** (self._consecutive_429 - 1), self._fallback_max)
            self._blocked_until = max(
                self._blocked_until, self._now() + pause
            )  # extend, never shrink
            return pause

    def wait_until_open(self, deadline: float) -> bool:
        """Block until the gate opens.

        Returns True once open. Returns False (without sleeping) if the gate opens
        after `deadline` — we never wait past the caller's budget.
        """
        while True:
            with self._lock:
                open_at = self._blocked_until
            if open_at <= self._now():
                return True
            if open_at > deadline:
                return False
            self._sleep(open_at - self._now())
