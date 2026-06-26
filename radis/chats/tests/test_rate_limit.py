from datetime import UTC
from unittest.mock import patch

import pytest
from django.conf import settings as dj_settings

from radis.chats.utils.chat_client import ChatClient
from radis.chats.utils.rate_limit import (
    RateLimited,
    RateLimitGate,
    _parse_retry_after,
    run_through_gate,
    with_transient_retries,
)
from radis.chats.utils.testing_helpers import make_connection_error, make_rate_limit_error


class FakeClock:
    """Deterministic monotonic clock; sleep just advances time."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_gate(clock: FakeClock) -> RateLimitGate:
    return RateLimitGate(
        base_seconds=5.0,
        fallback_max_seconds=120.0,
        header_ceiling_seconds=3600.0,
        now=clock.now,
        sleep=clock.sleep,
    )


def test_retry_after_within_budget_is_honored_in_full():
    clock = FakeClock()
    gate = make_gate(clock)
    pause = gate.note_rate_limited(200.0)  # NOT clamped to fallback_max (120)
    assert pause == 200.0


def test_retry_after_above_ceiling_is_clamped_to_ceiling():
    clock = FakeClock()
    gate = make_gate(clock)
    assert gate.note_rate_limited(4000.0) == 3600.0


def test_exponential_fallback_when_no_header():
    clock = FakeClock()
    gate = make_gate(clock)
    pauses = [gate.note_rate_limited(None) for _ in range(7)]
    assert pauses == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0]


def test_note_success_resets_the_ladder():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(None)
    gate.note_rate_limited(None)  # ladder now at 10
    gate.note_success()
    assert gate.note_rate_limited(None) == 5.0  # back to base


def test_window_extends_never_shrinks():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(100.0)  # blocked_until = now + 100
    gate.note_rate_limited(10.0)  # smaller; must not pull the window in
    # A deadline 50s out still sees the window closed past it.
    assert gate.wait_until_open(clock.now() + 50.0) is False


def test_wait_until_open_returns_true_when_already_open():
    clock = FakeClock()
    gate = make_gate(clock)
    assert gate.wait_until_open(clock.now() + 10.0) is True
    assert clock.slept == []


def test_wait_until_open_sleeps_then_opens_within_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(30.0)  # window 30s
    assert gate.wait_until_open(clock.now() + 300.0) is True
    assert clock.slept == [30.0]


def test_wait_until_open_defers_without_sleeping_when_window_exceeds_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(600.0)  # window 600s
    assert gate.wait_until_open(clock.now() + 300.0) is False
    assert clock.slept == []


def test_parse_retry_after_seconds():
    exc = make_rate_limit_error({"retry-after": "30"})
    assert _parse_retry_after(exc) == 30.0


def test_parse_retry_after_milliseconds():
    exc = make_rate_limit_error({"retry-after-ms": "1500"})
    assert _parse_retry_after(exc) == 1.5


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=60)
    exc = make_rate_limit_error({"retry-after": format_datetime(when)})
    seconds = _parse_retry_after(exc)
    assert seconds is not None
    assert 55.0 <= seconds <= 60.0


def test_parse_retry_after_missing_returns_none():
    exc = make_rate_limit_error({})
    assert _parse_retry_after(exc) is None


def test_run_through_gate_success_no_wait():
    clock = FakeClock()
    gate = make_gate(clock)
    result = run_through_gate(gate, 300.0, lambda: "ok", now=clock.now)
    assert result == "ok"
    assert clock.slept == []


def test_run_through_gate_429_then_success_waits_once():
    clock = FakeClock()
    gate = make_gate(clock)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error({"retry-after": "30"})
        return "ok"

    result = run_through_gate(gate, 300.0, fn, now=clock.now)
    assert result == "ok"
    assert clock.slept == [30.0]


def test_run_through_gate_retry_after_over_budget_defers_and_arms():
    clock = FakeClock()
    gate = make_gate(clock)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise make_rate_limit_error({"retry-after": "600"})

    with pytest.raises(RateLimited):
        run_through_gate(gate, 300.0, fn, now=clock.now)
    assert calls["n"] == 1  # gave up using the raw header; no wasted second probe
    # Gate is armed so other reports also defer.
    assert gate.wait_until_open(clock.now() + 300.0) is False


def test_run_through_gate_persistent_headerless_429_defers_at_budget():
    clock = FakeClock()
    gate = make_gate(clock)

    def fn():
        raise make_rate_limit_error({})  # no header -> exponential ladder

    with pytest.raises(RateLimited):
        run_through_gate(gate, 300.0, fn, now=clock.now)


def test_run_through_gate_non_429_propagates_untouched():
    clock = FakeClock()
    gate = make_gate(clock)

    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_through_gate(gate, 300.0, fn, now=clock.now)


def test_transient_retry_recovers_after_one_connection_error():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_connection_error()
        return "ok"

    result = with_transient_retries(fn, attempts=2, base=1.0, sleep=sleeps.append)
    assert result == "ok"
    assert sleeps == [1.0]  # one short backoff


def test_transient_retry_propagates_after_exhaustion():
    sleeps: list[float] = []

    def fn():
        raise make_connection_error()

    with pytest.raises(make_connection_error().__class__):
        with_transient_retries(fn, attempts=2, base=1.0, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]  # attempts retries before giving up


def test_transient_retry_does_not_catch_rate_limit_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise make_rate_limit_error({"retry-after": "30"})

    with pytest.raises(make_rate_limit_error().__class__):
        with_transient_retries(fn, attempts=2, base=1.0, sleep=lambda s: None)
    assert calls["n"] == 1  # 429 passes straight through to the gate; no retry/hammer


def test_transient_retry_does_not_catch_generic_error():
    def fn():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        with_transient_retries(fn, attempts=2, base=1.0, sleep=lambda s: None)


def test_chat_client_passes_max_retries_and_timeout_to_sdk():
    with patch("openai.OpenAI") as openai_cls:
        ChatClient(max_retries=0, timeout=60.0)
    kwargs = openai_cls.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == 60.0


def test_chat_client_omits_overrides_by_default():
    with patch("openai.OpenAI") as openai_cls:
        ChatClient()
    kwargs = openai_cls.call_args.kwargs
    assert "max_retries" not in kwargs
    assert "timeout" not in kwargs


def test_rate_limit_settings_have_expected_defaults():
    assert dj_settings.LLM_REQUEST_TIMEOUT_SECONDS == 60.0
    assert dj_settings.LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS == 5.0
    assert dj_settings.LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS == 120.0
    assert dj_settings.LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS == 3600.0
    assert dj_settings.LABELING_RATE_LIMIT_MAX_WAIT_SECONDS == 300.0
    assert dj_settings.LABELING_TRANSIENT_RETRY_ATTEMPTS == 2
    assert dj_settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS == 1.0
    assert dj_settings.LABELING_LLM_CONCURRENCY_LIMIT == 2
