"""Tests for radis.pgsearch.utils.rate_limiter."""

from datetime import timedelta

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
