"""Tests for radis.pgsearch.utils.rate_limiter."""

from datetime import timedelta

import openai
import pytest
from django.utils import timezone

pytestmark = pytest.mark.django_db


class _FakeClock:
    """Deterministic, controllable clock for testing sliding-window timing
    without real sleeps. `sleep()` advances the fake clock instead of
    blocking, matching what the code under test actually needs from time."""

    def __init__(self, start):
        self.current = start

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def _install_fake_clock(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _FakeClock(timezone.now())
    monkeypatch.setattr(rl, "_now", clock.now)
    monkeypatch.setattr(rl, "_sleep", clock.sleep)
    return clock


def test_acquire_token_admits_within_capacity(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 3
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_background")
    rl.acquire_token("embedding_background")
    rl.acquire_token("embedding_background")

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 3


def test_acquire_token_waits_for_capacity_then_succeeds(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_background")
    start = clock.current
    rl.acquire_token("embedding_background")

    assert (clock.current - start).total_seconds() >= 59


def test_try_acquire_immediate_returns_false_without_waiting(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)
    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    rl.acquire_token("embedding_background")
    before = clock.current
    result = rl.try_acquire_immediate("embedding_background")

    assert result is False
    assert sleep_calls == []
    assert clock.current == before  # non-blocking: clock must not advance


def test_acquire_token_raises_when_weight_exceeds_capacity(settings, monkeypatch):
    """weight > capacity can never be admitted; acquire_token must raise
    immediately instead of looping/sleeping forever (a livelock)."""
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 3
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)
    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    with pytest.raises(ValueError):
        rl.acquire_token("embedding_background", weight=4)

    # Raised before ever sleeping/looping — not after some retries.
    assert sleep_calls == []


def test_try_acquire_immediate_raises_when_weight_exceeds_capacity(settings, monkeypatch):
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 2
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)

    with pytest.raises(ValueError):
        rl.try_acquire_immediate("embedding_background", weight=3)


def test_two_waves_recover_independently(settings, monkeypatch):
    """Mirrors the empirical two-wave test: tokens taken at different times
    must expire independently, not all at once or on a shared timer."""
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 5
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    # Wave A: 3 tokens at t=0.
    for _ in range(3):
        rl.acquire_token("embedding_background")
    # Wave B: 2 more at t=30 (fills capacity to 5).
    clock.current += timedelta(seconds=30)
    for _ in range(2):
        rl.acquire_token("embedding_background")

    # A 6th request at t=30 must wait — capacity is full.
    sleep_calls = []
    real_sleep = rl._sleep
    monkeypatch.setattr(
        rl, "_sleep", lambda seconds: (sleep_calls.append(seconds), real_sleep(seconds))
    )
    rl.acquire_token("embedding_background")
    # Wave A's oldest (t=0) expires at t=60; we were at t=30, so the wait
    # should be close to 30s, not close to 0 or close to 60.
    assert sleep_calls and 25 <= sleep_calls[0] <= 31

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    # After sleeping past t=60, Wave A events expire and are pruned on next retry.
    # Remaining: 2 Wave B events + 1 newly created event = 3.
    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 3


def test_search_priority_spillover_uses_background_when_search_exhausted(settings, monkeypatch):
    settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 1
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 2
    from radis.pgsearch.utils import rate_limiter as rl

    _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_search")  # exhausts the search floor
    rl.acquire_search_priority_token()  # must spill into background

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_search").count() == 1
    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_background").count() == 1


def test_search_priority_waits_on_search_when_both_exhausted(settings, monkeypatch):
    settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 1
    settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 1
    from radis.pgsearch.utils import rate_limiter as rl

    clock = _install_fake_clock(monkeypatch)

    rl.acquire_token("embedding_search")
    rl.acquire_token("embedding_background")
    start = clock.current
    rl.acquire_search_priority_token()

    # Waited on search's own bucket (~60s), not left permanently blocked.
    assert (clock.current - start).total_seconds() >= 59

    from radis.pgsearch.models import EmbeddingRateLimitEvent

    # Only 1, not 2: the wait resolves precisely because the original
    # search event ages out of the window and gets pruned before the new
    # one is inserted, so it's gone by the time capacity frees up.
    assert EmbeddingRateLimitEvent.objects.filter(bucket="embedding_search").count() == 1


def _make_rate_limit_error(message: str, retry_after: str | None = None) -> "openai.RateLimitError":
    import httpx

    request = httpx.Request("POST", "http://embed.example/v1/embeddings")
    headers = {"Retry-After": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=request, json={"detail": message})
    return openai.RateLimitError(message=message, response=response, body=None)


def test_parse_retry_after_uses_header_when_present():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("Limit 60/min exceeded. Wait 27s.", retry_after="12")

    assert rl.parse_retry_after(exc) == 12.0


def test_parse_retry_after_falls_back_to_wait_message():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("Limit 60/min exceeded. Wait 27s.")

    assert rl.parse_retry_after(exc) == 27.0


def test_parse_retry_after_default_when_neither_present():
    from radis.pgsearch.utils import rate_limiter as rl

    exc = _make_rate_limit_error("rate limited")

    assert rl.parse_retry_after(exc) == rl._DEFAULT_RETRY_AFTER


def test_call_with_rate_limit_returns_on_first_success(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    acquire_calls = []
    result = rl.call_with_rate_limit(lambda: acquire_calls.append(1), lambda: "ok")

    assert result == "ok"
    assert len(acquire_calls) == 1


def test_call_with_rate_limit_honors_wait_and_retries(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_rate_limit(lambda: None, flaky)

    assert result == "ok"
    assert attempts["n"] == 3
    assert sleep_calls == [3.0, 3.0]


def test_call_with_rate_limit_raises_after_max_attempts(monkeypatch):
    import openai

    from radis.pgsearch.utils import rate_limiter as rl

    monkeypatch.setattr(rl, "_sleep", lambda seconds: None)
    acquire_calls = []

    def always_fails():
        raise _make_rate_limit_error("Limit 60/min exceeded. Wait 1s.")

    with pytest.raises(openai.RateLimitError):
        rl.call_with_rate_limit(lambda: acquire_calls.append(1), always_fails, max_attempts=3)

    assert len(acquire_calls) == 3
