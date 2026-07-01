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
TRANSIENT_ERRORS = (openai.APIConnectionError, openai.InternalServerError)


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
        fallback_max_seconds: float,
        header_ceiling_seconds: float,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base = base_seconds
        self._fallback_max = fallback_max_seconds  # caps the header-less exponential guess only
        self._header_ceiling = header_ceiling_seconds  # safety rail for an absurd Retry-After
        self._now = now
        self._sleep = sleep
        self._async_sleep = async_sleep
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


class RpmLimiter:
    """Per-process token bucket capping LLM requests per minute.

    Holds up to `max_rpm` request permits, refilling at max_rpm/60 permits per second.
    Each LLM request consumes one permit. Disabled (every acquire is an immediate no-op)
    when max_rpm <= 0. Permits are abstract request credits, unrelated to LLM tokens.
    """

    def __init__(
        self,
        max_rpm: int,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._enabled = max_rpm > 0
        self._capacity = float(max_rpm)
        self._refill_rate = max_rpm / 60.0  # permits per second
        self._now = now
        self._sleep = sleep
        self._async_sleep = async_sleep
        self._lock = threading.Lock()
        self._permits = float(max_rpm)  # start full so an initial burst is allowed
        self._last_refill = now()

    def reset(self) -> None:
        """Refill to full. For tests that share the process-global limiter."""
        with self._lock:
            self._permits = self._capacity
            self._last_refill = self._now()

    def _take_or_wait(self) -> float:
        """Refill, then consume one permit if available (return 0.0), else return the
        seconds until the next permit. Holds the lock only for this math, never to sleep.
        """
        with self._lock:
            now = self._now()
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._permits = min(self._capacity, self._permits + elapsed * self._refill_rate)
                self._last_refill = now
            if self._permits >= 1.0:
                self._permits -= 1.0
                return 0.0
            return (1.0 - self._permits) / self._refill_rate

    def acquire(self, deadline: float) -> bool:
        """Consume a permit, waiting until one is free. Returns False (without consuming)
        if the next permit would arrive after `deadline`."""
        if not self._enabled:
            return True
        while True:
            wait = self._take_or_wait()
            if wait == 0.0:
                return True
            if self._now() + wait > deadline:
                return False
            self._sleep(wait)

    async def acquire_async(self, deadline: float) -> bool:
        """Async twin of acquire; never blocks the event loop with time.sleep."""
        if not self._enabled:
            return True
        while True:
            wait = self._take_or_wait()
            if wait == 0.0:
                return True
            if self._now() + wait > deadline:
                return False
            await self._async_sleep(wait)


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
    return max(0.0, (retry_date - datetime.now(UTC)).total_seconds())


def run_through_gate[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], T],
    now: Callable[[], float] = time.monotonic,
    rpm: RpmLimiter | None = None,
) -> T:
    """Run `fn` through the gate, backing off on 429 up to `budget` seconds.

    Short rate-limits are waited out so the call succeeds. When the wait would
    exceed the budget the call is deferred (RateLimited). Non-429 errors propagate.
    """
    deadline = now() + budget
    while True:
        if not gate.wait_until_open(deadline):
            raise RateLimited()  # an earlier 429 armed a window past our budget
        if rpm is not None and not rpm.acquire(deadline):
            raise RateLimited()  # RPM cap can't grant a permit within the budget
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
    rpm: RpmLimiter | None = None,
) -> T:
    """Async twin of run_through_gate; `fn` is awaited."""
    deadline = now() + budget
    while True:
        if not await gate.wait_until_open_async(deadline):
            raise RateLimited()
        if rpm is not None and not await rpm.acquire_async(deadline):
            raise RateLimited()  # RPM cap can't grant a permit within the budget
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
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry `fn` a few times on transient non-429 errors (connection/timeout/5xx).

    Not gate-coordinated: these are usually per-request, not a provider-wide stop.
    A 429 is not caught here, so it passes straight to the gate without retrying.
    """
    for attempt in range(attempts + 1):
        try:
            return fn()
        except TRANSIENT_ERRORS:
            if attempt == attempts:
                raise  # exhausted -> let the failure path handle it
            sleep(base * 2**attempt)  # 1s, 2s, ...
    raise AssertionError("unreachable")  # range always runs at least once


async def with_transient_retries_async[T](
    fn: Callable[[], Awaitable[T]],
    attempts: int,
    base: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Async twin of with_transient_retries; `fn` is awaited, `sleep` is awaited."""
    for attempt in range(attempts + 1):
        try:
            return await fn()
        except TRANSIENT_ERRORS:
            if attempt == attempts:
                raise
            await sleep(base * 2**attempt)
    raise AssertionError("unreachable")
