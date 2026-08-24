import asyncio
import email.utils
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import openai

logger = logging.getLogger(__name__)

# Transient, usually per-request failures (not rate-limits). Worth a small local retry.
# Timeouts are covered too: openai.APITimeoutError subclasses APIConnectionError.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.APIConnectionError,
    openai.InternalServerError,
)


class RateLimited(Exception):
    """A call could not complete within its wait budget; defer it."""

    def __init__(self, message: str = "LLM rate limit exceeded the wait budget") -> None:
        super().__init__(message)


class RateLimitGate:
    """Per-process barrier that makes all LLM callers back off together on a 429.

    A 429 from any caller closes the gate for a while; every caller (sync or async)
    waits behind the same window, so the process stops hammering a provider that is
    already blocking it.
    """

    def __init__(
        self,
        base_seconds: float,
        backoff_max_seconds: float,
        header_ceiling_seconds: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_seconds
        self._backoff_max = backoff_max_seconds  # caps the exponential backoff pause
        self._header_ceiling = header_ceiling_seconds  # above this a Retry-After is deemed absurd
        self._now = now
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._lock = threading.Lock()
        self._blocked_until = 0.0  # monotonic deadline; gate is open when now() >= this
        self._consecutive_429 = 0  # 429s without a usable Retry-After; drives the backoff ladder

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
            if retry_after is not None and retry_after < self._header_ceiling:
                # A sane Retry-After: trust the provider and wait exactly that long.
                pause = retry_after
            else:
                # No header, or an absurd one (>= header_ceiling): ignore it and climb the
                # exponential backoff ladder, same as a header-less 429.
                self._consecutive_429 += 1
                pause = min(self._base * 2 ** (self._consecutive_429 - 1), self._backoff_max)
            # Defensive: if two requests get rate-limited concurrently, don't let the
            # second (shorter) pause shrink an already-armed longer window.
            self._blocked_until = max(self._blocked_until, self._now() + pause)
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
            self._sleep(max(0.0, open_at - self._now()))

    async def wait_until_open_async(self, deadline: float) -> bool:
        """Async twin of wait_until_open; never blocks the event loop with time.sleep."""
        while True:
            with self._lock:
                open_at = self._blocked_until
            if open_at <= self._now():
                return True
            if open_at > deadline:
                return False
            await self._async_sleep(max(0.0, open_at - self._now()))


def _parse_retry_after(exc: openai.RateLimitError) -> float | None:
    """Read Retry-After from a 429 response as seconds, or None.

    Handles `retry-after-ms`, `retry-after` in seconds, and an HTTP-date.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = response.headers

    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return float(ms) / 1000.0
        except ValueError:
            pass

    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)  # plain seconds
    except ValueError:
        pass

    try:
        retry_date = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_date is None:
        return None
    if retry_date.tzinfo is None:  # an RFC "-0000" offset yields a naive datetime; treat as UTC
        retry_date = retry_date.replace(tzinfo=UTC)
    return max(0.0, (retry_date - datetime.now(UTC)).total_seconds())


def run_through_gate[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], T],
    now: Callable[[], float] = time.monotonic,
) -> T:
    """Run `fn` through the gate, backing off on 429 up to `budget` seconds.

    Short rate-limits are waited out so the call succeeds. When the wait would
    exceed the budget the call is deferred (RateLimited). Non-429 errors propagate.
    """
    deadline = now() + budget
    while True:
        if not gate.wait_until_open(deadline):
            raise RateLimited()  # an earlier 429 armed a window past our budget
        try:
            result = fn()
            gate.note_success()
            return result
        except openai.RateLimitError as exc:
            retry_after = _parse_retry_after(exc)
            pause = gate.note_rate_limited(retry_after)  # arm first so others back off too
            logger.warning("Rate-limited; backing off %.1fs", pause)
            # Loop back: wait_until_open() waits out the (clamped) window if it fits the
            # budget, or defers (RateLimited) when the armed window exceeds the deadline.


async def run_through_gate_async[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], Awaitable[T]],
    now: Callable[[], float] = time.monotonic,
) -> T:
    """Async twin of run_through_gate; `fn` is awaited."""
    deadline = now() + budget
    while True:
        if not await gate.wait_until_open_async(deadline):
            raise RateLimited()
        try:
            result = await fn()
            gate.note_success()
            return result
        except openai.RateLimitError as exc:
            retry_after = _parse_retry_after(exc)
            pause = gate.note_rate_limited(retry_after)  # arm first so others back off too
            logger.warning("Rate-limited; backing off %.1fs", pause)
            # Loop back: wait_until_open_async() waits out the (clamped) window if it fits
            # the budget, or defers (RateLimited) when the armed window exceeds the deadline.


def with_transient_retries[T](
    fn: Callable[[], T],
    attempts: int,
    base: float,
    sleep: Callable[[float], None] | None = None,
    retryable: tuple[type[Exception], ...] = TRANSIENT_ERRORS,
) -> T:
    """Retry `fn` a few times on transient non-429 errors (connection/timeout/5xx).

    Not gate-coordinated: these are usually per-request, not a provider-wide stop.
    A 429 is not caught here, so it passes straight to the gate without retrying.
    """
    if sleep is None:  # resolved at call time so tests can patch time.sleep
        sleep = time.sleep
    for attempt in range(attempts + 1):
        try:
            return fn()
        except retryable as exc:
            if attempt == attempts:
                raise  # exhausted -> let the failure path handle it
            wait = base * 2**attempt  # 1s, 2s, ...
            logger.warning(
                "Transient error on attempt %d; retrying in %.2fs. Error: %s",
                attempt + 1,
                wait,
                exc,
            )
            sleep(wait)
    raise AssertionError("unreachable")  # range always runs at least once


async def with_transient_retries_async[T](
    fn: Callable[[], Awaitable[T]],
    attempts: int,
    base: float,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    retryable: tuple[type[Exception], ...] = TRANSIENT_ERRORS,
) -> T:
    """Async twin of with_transient_retries; `fn` is awaited, `sleep` is awaited."""
    if sleep is None:  # resolved at call time so tests can patch asyncio.sleep
        sleep = asyncio.sleep
    for attempt in range(attempts + 1):
        try:
            return await fn()
        except retryable as exc:
            if attempt == attempts:
                raise
            wait = base * 2**attempt
            logger.warning(
                "Transient error on attempt %d; retrying in %.2fs. Error: %s",
                attempt + 1,
                wait,
                exc,
            )
            await sleep(wait)
    raise AssertionError("unreachable")
