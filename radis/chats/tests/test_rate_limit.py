from radis.chats.utils.rate_limit import RateLimitGate


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
