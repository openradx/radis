"""Tests for radis.pgsearch.utils.rate_limiter (reactive 429 backoff)."""

from datetime import timedelta

import httpx
import openai
import pytest
from django.utils import timezone


class _FakeClock:
    """Deterministic clock for the rate limiter's _now/_sleep seams: time
    stands still except when _sleep advances it, so pause arithmetic in
    assertions is exact."""

    def __init__(self) -> None:
        self.current = timezone.now()
        self.sleeps: list[float] = []

    def now(self):
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> _FakeClock:
    from radis.pgsearch.utils import rate_limiter as rl

    c = _FakeClock()
    monkeypatch.setattr(rl, "_now", c.now)
    monkeypatch.setattr(rl, "_sleep", c.sleep)
    return c


def _make_rate_limit_error(message: str, retry_after: str | None = None) -> "openai.RateLimitError":
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


def test_call_with_429_backoff_returns_on_first_success(monkeypatch):
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    result = rl.call_with_429_backoff(lambda: "ok")

    assert result == "ok"
    assert sleep_calls == []


def test_call_with_429_backoff_waits_exponentially_then_succeeds(monkeypatch):
    """The base wait comes from the server's own hint ("Wait 3s") and doubles
    on each subsequent 429: 3s, then 6s."""
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _make_rate_limit_error("Limit 60/min exceeded. Wait 3s.")
        return "ok"

    result = rl.call_with_429_backoff(flaky)

    assert result == "ok"
    assert attempts["n"] == 3
    assert sleep_calls == [3.0, 6.0]


def test_call_with_429_backoff_raises_after_max_attempts(monkeypatch):
    """The final 429 propagates (no sleep after it) so the caller's
    task-level retry policy applies."""
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise _make_rate_limit_error("Limit 60/min exceeded. Wait 1s.")

    with pytest.raises(openai.RateLimitError):
        rl.call_with_429_backoff(always_fails, max_attempts=3)

    assert attempts["n"] == 3
    assert sleep_calls == [1.0, 2.0]


def test_call_with_429_backoff_does_not_intercept_other_errors(monkeypatch):
    """Only 429s are backed off here — other errors propagate immediately
    to the stamina/Procrastinate layers."""
    from radis.pgsearch.utils import rate_limiter as rl

    sleep_calls = []
    monkeypatch.setattr(rl, "_sleep", lambda seconds: sleep_calls.append(seconds))

    def fails():
        raise ValueError("not a 429")

    with pytest.raises(ValueError):
        rl.call_with_429_backoff(fails)

    assert sleep_calls == []


@pytest.mark.django_db
def test_embedding_backoff_state_defaults():
    from radis.pgsearch.models import EmbeddingBackoffState

    state, created = EmbeddingBackoffState.objects.get_or_create(pk=1)

    assert created
    assert state.paused_until is None
    assert state.consecutive_429s == 0


@pytest.mark.django_db
def test_record_429_sets_pause_and_doubles_globally(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    assert rl.record_429(10.0) == 10.0
    assert rl.record_429(10.0) == 20.0

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.consecutive_429s == 2
    assert state.paused_until == clock.current + timedelta(seconds=20)


@pytest.mark.django_db
def test_record_429_extends_but_never_shortens_the_pause(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(30.0)
    # Second 429 with a short server wait: 1s * 2^1 = 2s candidate loses
    # to the existing 30s pause.
    rl.record_429(1.0)

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.paused_until == clock.current + timedelta(seconds=30)
    assert state.consecutive_429s == 2


@pytest.mark.django_db
def test_shared_wait_seconds_reads_the_pause(clock):
    from radis.pgsearch.utils import rate_limiter as rl

    assert rl.shared_wait_seconds() == 0.0  # no row yet

    rl.record_429(15.0)
    assert rl.shared_wait_seconds() == 15.0

    clock.current += timedelta(seconds=20)  # pause expired
    assert rl.shared_wait_seconds() == 0.0


@pytest.mark.django_db
def test_record_success_resets_counter(clock):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    rl.record_429(5.0)
    rl.record_success()

    state = EmbeddingBackoffState.objects.get(pk=1)
    assert state.consecutive_429s == 0
    # The pause itself is NOT cleared — it expires on its own; only the
    # doubling counter resets.
    assert state.paused_until == clock.current + timedelta(seconds=5)


@pytest.mark.django_db
def test_record_success_is_read_only_when_counter_already_zero(
    clock, django_assert_num_queries
):
    from radis.pgsearch.models import EmbeddingBackoffState
    from radis.pgsearch.utils import rate_limiter as rl

    EmbeddingBackoffState.objects.create(pk=1)

    with django_assert_num_queries(1):
        rl.record_success()
