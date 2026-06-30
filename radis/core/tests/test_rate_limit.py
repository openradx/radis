from datetime import UTC

import pytest

from radis.chats.utils.testing_helpers import make_connection_error, make_rate_limit_error
from radis.core.utils.rate_limit import (
    RateLimited,
    RateLimitGate,
    _parse_retry_after,
    run_through_gate,
    run_through_gate_async,
    with_transient_retries,
    with_transient_retries_async,
)


class FakeClock:
    """Deterministic monotonic clock; sync and async sleep both just advance time."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_gate(clock: FakeClock) -> RateLimitGate:
    return RateLimitGate(
        base_seconds=5.0,
        fallback_max_seconds=120.0,
        header_ceiling_seconds=3600.0,
        now=clock.now,
        sleep=clock.sleep,
        async_sleep=clock.async_sleep,
    )


# --- gate state ---


def test_retry_after_within_budget_is_honored_in_full():
    gate = make_gate(FakeClock())
    assert gate.note_rate_limited(200.0) == 200.0  # not clamped to fallback_max (120)


def test_retry_after_above_ceiling_is_clamped_to_ceiling():
    gate = make_gate(FakeClock())
    assert gate.note_rate_limited(4000.0) == 3600.0


def test_exponential_fallback_when_no_header():
    gate = make_gate(FakeClock())
    pauses = [gate.note_rate_limited(None) for _ in range(7)]
    assert pauses == [5.0, 10.0, 20.0, 40.0, 80.0, 120.0, 120.0]


def test_note_success_resets_the_ladder():
    gate = make_gate(FakeClock())
    gate.note_rate_limited(None)
    gate.note_rate_limited(None)
    gate.note_success()
    assert gate.note_rate_limited(None) == 5.0


def test_window_extends_never_shrinks():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(100.0)
    gate.note_rate_limited(10.0)
    assert gate.wait_until_open(clock.now() + 50.0) is False


def test_reset_clears_state():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(600.0)
    gate.reset()
    assert gate.wait_until_open(clock.now() + 1.0) is True


# --- sync wait ---


def test_wait_until_open_returns_true_when_already_open():
    clock = FakeClock()
    gate = make_gate(clock)
    assert gate.wait_until_open(clock.now() + 10.0) is True
    assert clock.slept == []


def test_wait_until_open_sleeps_then_opens_within_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(30.0)
    assert gate.wait_until_open(clock.now() + 300.0) is True
    assert clock.slept == [30.0]


def test_wait_until_open_defers_without_sleeping_when_window_exceeds_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(600.0)
    assert gate.wait_until_open(clock.now() + 300.0) is False
    assert clock.slept == []


# --- async wait ---


@pytest.mark.asyncio
async def test_wait_until_open_async_returns_true_when_already_open():
    clock = FakeClock()
    gate = make_gate(clock)
    assert await gate.wait_until_open_async(clock.now() + 10.0) is True
    assert clock.slept == []


@pytest.mark.asyncio
async def test_wait_until_open_async_sleeps_then_opens_within_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(30.0)
    assert await gate.wait_until_open_async(clock.now() + 300.0) is True
    assert clock.slept == [30.0]


@pytest.mark.asyncio
async def test_wait_until_open_async_defers_without_sleeping_when_window_exceeds_deadline():
    clock = FakeClock()
    gate = make_gate(clock)
    gate.note_rate_limited(600.0)
    assert await gate.wait_until_open_async(clock.now() + 300.0) is False
    assert clock.slept == []


# --- _parse_retry_after ---


def test_parse_retry_after_seconds():
    assert _parse_retry_after(make_rate_limit_error({"retry-after": "30"})) == 30.0


def test_parse_retry_after_milliseconds():
    assert _parse_retry_after(make_rate_limit_error({"retry-after-ms": "1500"})) == 1.5


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    when = datetime.now(UTC) + timedelta(seconds=60)
    seconds = _parse_retry_after(make_rate_limit_error({"retry-after": format_datetime(when)}))
    assert seconds is not None
    assert 55.0 <= seconds <= 60.0


def test_parse_retry_after_missing_returns_none():
    assert _parse_retry_after(make_rate_limit_error({})) is None


# --- run_through_gate (sync) ---


def test_run_through_gate_success_no_wait():
    clock = FakeClock()
    gate = make_gate(clock)
    assert run_through_gate(gate, 300.0, lambda: "ok", now=clock.now) == "ok"
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

    assert run_through_gate(gate, 300.0, fn, now=clock.now) == "ok"
    assert clock.slept == [30.0]


def test_run_through_gate_retry_after_over_budget_defers_and_arms():
    clock = FakeClock()
    gate = make_gate(clock)

    def fn():
        raise make_rate_limit_error({"retry-after": "600"})

    with pytest.raises(RateLimited):
        run_through_gate(gate, 300.0, fn, now=clock.now)
    assert gate.wait_until_open(clock.now() + 300.0) is False


def test_run_through_gate_non_429_propagates_untouched():
    clock = FakeClock()
    gate = make_gate(clock)

    def fn():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        run_through_gate(gate, 300.0, fn, now=clock.now)


# --- run_through_gate_async ---


@pytest.mark.asyncio
async def test_run_through_gate_async_success_no_wait():
    clock = FakeClock()
    gate = make_gate(clock)

    async def fn():
        return "ok"

    assert await run_through_gate_async(gate, 300.0, fn, now=clock.now) == "ok"
    assert clock.slept == []


@pytest.mark.asyncio
async def test_run_through_gate_async_429_then_success_waits_once():
    clock = FakeClock()
    gate = make_gate(clock)
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error({"retry-after": "30"})
        return "ok"

    assert await run_through_gate_async(gate, 300.0, fn, now=clock.now) == "ok"
    assert clock.slept == [30.0]


@pytest.mark.asyncio
async def test_run_through_gate_async_over_budget_defers():
    clock = FakeClock()
    gate = make_gate(clock)

    async def fn():
        raise make_rate_limit_error({"retry-after": "600"})

    with pytest.raises(RateLimited):
        await run_through_gate_async(gate, 300.0, fn, now=clock.now)


# --- transient retries (sync) ---


def test_transient_retry_recovers_after_one_connection_error():
    sleeps: list[float] = []
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_connection_error()
        return "ok"

    assert with_transient_retries(fn, attempts=2, base=1.0, sleep=sleeps.append) == "ok"
    assert sleeps == [1.0]


def test_transient_retry_propagates_after_exhaustion():
    sleeps: list[float] = []

    def fn():
        raise make_connection_error()

    with pytest.raises(make_connection_error().__class__):
        with_transient_retries(fn, attempts=2, base=1.0, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0]


def test_transient_retry_does_not_catch_rate_limit_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise make_rate_limit_error({"retry-after": "30"})

    with pytest.raises(make_rate_limit_error().__class__):
        with_transient_retries(fn, attempts=2, base=1.0, sleep=lambda s: None)
    assert calls["n"] == 1


# --- transient retries (async) ---


@pytest.mark.asyncio
async def test_transient_retry_async_recovers_after_one_connection_error():
    sleeps: list[float] = []
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_connection_error()
        return "ok"

    async def fake_sleep(s):
        sleeps.append(s)

    assert await with_transient_retries_async(fn, attempts=2, base=1.0, sleep=fake_sleep) == "ok"
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_transient_retry_async_does_not_catch_rate_limit_error():
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        raise make_rate_limit_error({"retry-after": "30"})

    async def fake_sleep(s):
        pass

    with pytest.raises(make_rate_limit_error().__class__):
        await with_transient_retries_async(fn, attempts=2, base=1.0, sleep=fake_sleep)
    assert calls["n"] == 1
