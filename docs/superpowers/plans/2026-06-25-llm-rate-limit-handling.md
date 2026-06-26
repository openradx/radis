# LLM Rate-Limit & Error Handling for Labeling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make auto-labeling throttle the whole worker on a 429 (honoring `Retry-After`), ride out short rate-limits, retry occasional non-429 transient errors, and defer cleanly when a limit is too long — without changing chat behavior.

**Architecture:** A process-global `RateLimitGate` coordinates all labeling threads: one 429 closes a shared backoff window, every call waits behind it. A `run_through_gate` wrapper handles 429/backoff with a per-report give-up budget; a separate `with_transient_retries` handles connection/timeout/5xx. A thin `ThrottledChatClient` composes both around the existing `ChatClient` and is the only client the labeling path uses.

**Tech Stack:** Python 3.12, Django, `openai` SDK (+ `httpx`), pytest / pytest-django, `django-environ` settings, Procrastinate workers.

## Global Constraints

- Style: Google Python Style Guide; line length 100 (Ruff E, F, I, DJ); pyright basic.
- Docstrings/comments: short and plain — only the non-obvious *why*; no verbose or dense prose.
- Tests: pytest with `@pytest.mark.django_db` for DB tests; factories from `radis.*.factories`.
- All new tunables are env-configurable via `env.float(...)`/`env.int(...)` in `radis/settings/base.py`, defaults exactly as listed, and documented in `example.env` + `CLAUDE.md`.
- `max_retries=0` is passed **only** to the labeling `ChatClient`; chat clients keep SDK defaults.
- Run a single test with: `uv run cli test -- <pytest args>` (e.g. `-k test_name -v`).

**Default values (verbatim):**
- `LLM_REQUEST_TIMEOUT_SECONDS = 60.0`
- `LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS = 5.0`
- `LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS = 120.0`
- `LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS = 3600.0`
- `LABELING_RATE_LIMIT_MAX_WAIT_SECONDS = 300.0`
- `LABELING_TRANSIENT_RETRY_ATTEMPTS = 2`
- `LABELING_TRANSIENT_RETRY_BASE_SECONDS = 1.0`
- `LABELING_LLM_CONCURRENCY_LIMIT = 2` (was 6)

---

## File Structure

- Create `radis/chats/utils/rate_limit.py` — `RateLimitGate`, `run_through_gate`, `with_transient_retries`, `RateLimited`, `_parse_retry_after`, `TRANSIENT_ERRORS`. Reusable, no labels coupling.
- Create `radis/chats/tests/test_rate_limit.py` — unit tests for the above (fake clock).
- Modify `radis/chats/utils/chat_client.py` — `ChatClient` gains optional `max_retries`/`timeout`.
- Modify `radis/chats/utils/testing_helpers.py` — add `make_rate_limit_error`, `make_connection_error`.
- Create `radis/labels/throttled_client.py` — `ThrottledChatClient` + `_LABELING_GATE` singleton.
- Modify `radis/labels/labeling.py` — build the labeling client through `ThrottledChatClient`.
- Create `radis/labels/tests/test_throttled_client.py` — integration tests through `label_report`.
- Modify `radis/settings/base.py` — add the eight settings above.
- Modify `example.env` and `CLAUDE.md` — document the new env vars.

---

### Task 1: `RateLimitGate` coordinator

**Files:**
- Create: `radis/chats/utils/rate_limit.py`
- Test: `radis/chats/tests/test_rate_limit.py`

**Interfaces:**
- Produces:
  - `RateLimited(Exception)` — sentinel for "defer this report".
  - `TRANSIENT_ERRORS: tuple` — retryable non-429 exception types.
  - `RateLimitGate(base_seconds: float, fallback_max_seconds: float, header_ceiling_seconds: float, now=time.monotonic, sleep=time.sleep)` with methods:
    - `reset() -> None`
    - `note_success() -> None`
    - `note_rate_limited(retry_after: float | None) -> float` (returns the pause used to arm)
    - `wait_until_open(deadline: float) -> bool` (False = defer, no sleep)

- [ ] **Step 1: Write the failing tests**

Create `radis/chats/tests/test_rate_limit.py`:

```python
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
    gate.note_rate_limited(10.0)   # smaller; must not pull the window in
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -v`
Expected: FAIL — `ModuleNotFoundError: radis.chats.utils.rate_limit`.

- [ ] **Step 3: Write the gate implementation**

Create `radis/chats/utils/rate_limit.py`:

```python
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
            self._blocked_until = max(self._blocked_until, self._now() + pause)  # extend, never shrink
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add radis/chats/utils/rate_limit.py radis/chats/tests/test_rate_limit.py
git commit -m "feat(chats): add RateLimitGate coordinator for labeling backoff"
```

---

### Task 2: `_parse_retry_after` + test helper

**Files:**
- Modify: `radis/chats/utils/rate_limit.py`
- Modify: `radis/chats/utils/testing_helpers.py`
- Test: `radis/chats/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: `RateLimited`, `RateLimitGate` (Task 1).
- Produces:
  - `_parse_retry_after(exc: openai.RateLimitError) -> float | None`.
  - `make_rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError`.

- [ ] **Step 1: Add the test helper**

Add to `radis/chats/utils/testing_helpers.py` (add `import httpx` at top with the other imports):

```python
def make_rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError:
    """Build a real openai.RateLimitError carrying chosen response headers (e.g. retry-after)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    response = httpx.Response(429, headers=headers or {}, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)
```

- [ ] **Step 2: Write the failing tests**

Append to `radis/chats/tests/test_rate_limit.py` (add the import at the top):

```python
from radis.chats.utils.rate_limit import _parse_retry_after
from radis.chats.utils.testing_helpers import make_rate_limit_error
```

```python
def test_parse_retry_after_seconds():
    exc = make_rate_limit_error({"retry-after": "30"})
    assert _parse_retry_after(exc) == 30.0


def test_parse_retry_after_milliseconds():
    exc = make_rate_limit_error({"retry-after-ms": "1500"})
    assert _parse_retry_after(exc) == 1.5


def test_parse_retry_after_http_date():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) + timedelta(seconds=60)
    exc = make_rate_limit_error({"retry-after": format_datetime(when)})
    seconds = _parse_retry_after(exc)
    assert seconds is not None
    assert 55.0 <= seconds <= 60.0


def test_parse_retry_after_missing_returns_none():
    exc = make_rate_limit_error({})
    assert _parse_retry_after(exc) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k parse_retry_after -v`
Expected: FAIL — `ImportError: cannot import name '_parse_retry_after'`.

- [ ] **Step 4: Implement `_parse_retry_after`**

Add to `radis/chats/utils/rate_limit.py` (add `import email.utils` and `from datetime import datetime, timezone` to the imports):

```python
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
    return max(0.0, (retry_date - datetime.now(timezone.utc)).total_seconds())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k parse_retry_after -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add radis/chats/utils/rate_limit.py radis/chats/utils/testing_helpers.py radis/chats/tests/test_rate_limit.py
git commit -m "feat(chats): parse Retry-After (seconds, ms, HTTP-date) from 429s"
```

---

### Task 3: `run_through_gate` wrapper

**Files:**
- Modify: `radis/chats/utils/rate_limit.py`
- Test: `radis/chats/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: `RateLimitGate`, `RateLimited`, `_parse_retry_after`, `make_rate_limit_error`.
- Produces: `run_through_gate(gate: RateLimitGate, budget: float, fn: Callable[[], T], now=time.monotonic) -> T`.

- [ ] **Step 1: Write the failing tests**

Append to `radis/chats/tests/test_rate_limit.py` (extend the import line):

```python
import pytest
from radis.chats.utils.rate_limit import RateLimited, run_through_gate
```

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k run_through_gate -v`
Expected: FAIL — `ImportError: cannot import name 'run_through_gate'`.

- [ ] **Step 3: Implement `run_through_gate`**

Add to `radis/chats/utils/rate_limit.py` (add `from typing import TypeVar` and `T = TypeVar("T")` near the top):

```python
def run_through_gate(
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], T],
    now: Callable[[], float] = time.monotonic,
) -> T:
    """Run `fn` through the gate, backing off on 429 up to `budget` seconds.

    Short rate-limits are waited out so the call succeeds. When the wait would
    exceed the budget the report is deferred (RateLimited). Non-429 errors propagate.
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
            pause = gate.note_rate_limited(retry_after)  # arm first so other threads back off too
            effective = retry_after if retry_after is not None else pause
            logger.warning("Labeling rate-limited; backing off %.1fs", effective)
            if now() + effective > deadline:
                raise RateLimited() from exc  # can't wait it out; defer this report
            # else loop: wait_until_open() waits out the (<=budget) window, then retries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k run_through_gate -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add radis/chats/utils/rate_limit.py radis/chats/tests/test_rate_limit.py
git commit -m "feat(chats): add run_through_gate per-call backoff with give-up budget"
```

---

### Task 4: `with_transient_retries`

**Files:**
- Modify: `radis/chats/utils/rate_limit.py`
- Modify: `radis/chats/utils/testing_helpers.py`
- Test: `radis/chats/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: `TRANSIENT_ERRORS`, `make_rate_limit_error`.
- Produces:
  - `with_transient_retries(fn: Callable[[], T], attempts: int, base: float, sleep=time.sleep) -> T`.
  - `make_connection_error() -> openai.APIConnectionError`.

- [ ] **Step 1: Add the connection-error test helper**

Add to `radis/chats/utils/testing_helpers.py`:

```python
def make_connection_error() -> openai.APIConnectionError:
    """Build a real openai.APIConnectionError (a transient, non-429 error)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    return openai.APIConnectionError(message="connection failed", request=request)
```

- [ ] **Step 2: Write the failing tests**

Append to `radis/chats/tests/test_rate_limit.py` (extend imports):

```python
from radis.chats.utils.rate_limit import with_transient_retries
from radis.chats.utils.testing_helpers import make_connection_error
```

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k transient -v`
Expected: FAIL — `ImportError: cannot import name 'with_transient_retries'`.

- [ ] **Step 4: Implement `with_transient_retries`**

Add to `radis/chats/utils/rate_limit.py`:

```python
def with_transient_retries(
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
                raise  # exhausted -> let the failure path defer the report
            sleep(base * 2 ** attempt)  # 1s, 2s, ...
    raise AssertionError("unreachable")  # range always runs at least once
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k transient -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add radis/chats/utils/rate_limit.py radis/chats/utils/testing_helpers.py radis/chats/tests/test_rate_limit.py
git commit -m "feat(chats): add with_transient_retries for non-429 errors"
```

---

### Task 5: `ChatClient` gains `max_retries` / `timeout`

**Files:**
- Modify: `radis/chats/utils/chat_client.py`
- Test: `radis/chats/tests/test_rate_limit.py`

**Interfaces:**
- Produces: `ChatClient(max_retries: int | None = None, timeout: float | None = None)`.

Note: only `ChatClient` (used by labeling) gains these params. `AsyncChatClient` (chat only) is left unchanged — no consumer needs them (YAGNI). Chat is unaffected either way because defaults preserve current SDK behavior.

- [ ] **Step 1: Write the failing test**

Append to `radis/chats/tests/test_rate_limit.py`:

```python
from unittest.mock import patch

from radis.chats.utils.chat_client import ChatClient


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k chat_client -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'max_retries'`.

- [ ] **Step 3: Update `ChatClient.__init__`**

In `radis/chats/utils/chat_client.py`, replace the `ChatClient.__init__` body:

```python
    def __init__(self, max_retries: int | None = None, timeout: float | None = None) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY

        # Only pass overrides when given, so chat keeps the SDK defaults.
        client_kwargs: dict = {"base_url": base_url, "api_key": api_key}
        if max_retries is not None:
            client_kwargs["max_retries"] = max_retries
        if timeout is not None:
            client_kwargs["timeout"] = timeout

        self._client = openai.OpenAI(**client_kwargs)
        self._llm_model_name = settings.LLM_MODEL_NAME
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k chat_client -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add radis/chats/utils/chat_client.py radis/chats/tests/test_rate_limit.py
git commit -m "feat(chats): allow ChatClient max_retries/timeout overrides"
```

---

### Task 6: Settings + env documentation

**Files:**
- Modify: `radis/settings/base.py` (labeling block, near line 486)
- Modify: `example.env`
- Modify: `CLAUDE.md`

**Interfaces:**
- Produces (Django settings): `LLM_REQUEST_TIMEOUT_SECONDS`, `LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS`, `LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS`, `LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS`, `LABELING_RATE_LIMIT_MAX_WAIT_SECONDS`, `LABELING_TRANSIENT_RETRY_ATTEMPTS`, `LABELING_TRANSIENT_RETRY_BASE_SECONDS`; and `LABELING_LLM_CONCURRENCY_LIMIT` default changed 6 → 2.

- [ ] **Step 1: Write the failing test**

Append to `radis/chats/tests/test_rate_limit.py`:

```python
from django.conf import settings as dj_settings


def test_rate_limit_settings_have_expected_defaults():
    assert dj_settings.LLM_REQUEST_TIMEOUT_SECONDS == 60.0
    assert dj_settings.LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS == 5.0
    assert dj_settings.LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS == 120.0
    assert dj_settings.LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS == 3600.0
    assert dj_settings.LABELING_RATE_LIMIT_MAX_WAIT_SECONDS == 300.0
    assert dj_settings.LABELING_TRANSIENT_RETRY_ATTEMPTS == 2
    assert dj_settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS == 1.0
    assert dj_settings.LABELING_LLM_CONCURRENCY_LIMIT == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k settings -v`
Expected: FAIL — `AttributeError` on the first new setting (and/or concurrency == 6).

- [ ] **Step 3: Add the settings**

In `radis/settings/base.py`, change the existing concurrency default:

```python
LABELING_LLM_CONCURRENCY_LIMIT = env.int("LABELING_LLM_CONCURRENCY_LIMIT", default=2)
```

Then add, just below the labeling block (after `LABELING_SCAN_CRON`):

```python
# Labeling LLM rate-limit & error handling (see the rate-limit design doc).
LLM_REQUEST_TIMEOUT_SECONDS = env.float("LLM_REQUEST_TIMEOUT_SECONDS", default=60.0)
LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS = env.float(
    "LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS", default=5.0
)
LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS = env.float(
    "LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS", default=120.0
)
LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS = env.float(
    "LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS", default=3600.0
)
LABELING_RATE_LIMIT_MAX_WAIT_SECONDS = env.float(
    "LABELING_RATE_LIMIT_MAX_WAIT_SECONDS", default=300.0
)
LABELING_TRANSIENT_RETRY_ATTEMPTS = env.int("LABELING_TRANSIENT_RETRY_ATTEMPTS", default=2)
LABELING_TRANSIENT_RETRY_BASE_SECONDS = env.float(
    "LABELING_TRANSIENT_RETRY_BASE_SECONDS", default=1.0
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run cli test -- radis/chats/tests/test_rate_limit.py -k settings -v`
Expected: PASS.

- [ ] **Step 5: Document the env vars**

Add to `example.env` (in the labeling section, alongside other `LABELING_*` vars):

```bash
# Labeling LLM rate-limit & error handling
LLM_REQUEST_TIMEOUT_SECONDS=60.0
LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS=5.0
LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS=120.0
LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS=3600.0
LABELING_RATE_LIMIT_MAX_WAIT_SECONDS=300.0
LABELING_TRANSIENT_RETRY_ATTEMPTS=2
LABELING_TRANSIENT_RETRY_BASE_SECONDS=1.0
```

Add to `CLAUDE.md`, in the "Auto-labeling (`radis.labels`)" env list:

```markdown
- `LLM_REQUEST_TIMEOUT_SECONDS`: Per-request timeout for the labeling client (default `60`).
- `LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS`: First exponential pause when no `Retry-After` (default `5`).
- `LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS`: Caps the header-less exponential guess (default `120`).
- `LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS`: Safety rail on how long one `Retry-After` may hold the gate (default `3600`).
- `LABELING_RATE_LIMIT_MAX_WAIT_SECONDS`: Per-report give-up budget before deferring (default `300`).
- `LABELING_TRANSIENT_RETRY_ATTEMPTS`: Local retries for non-429 transient errors (default `2`).
- `LABELING_TRANSIENT_RETRY_BASE_SECONDS`: Base backoff for transient retries (default `1`).
```

Also update the existing `LABELING_LLM_CONCURRENCY_LIMIT` line in `CLAUDE.md` to read `default 2` (was `6`), if it states a default.

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py example.env CLAUDE.md radis/chats/tests/test_rate_limit.py
git commit -m "feat(labels): add env-configurable rate-limit settings; concurrency 6->2"
```

---

### Task 7: `ThrottledChatClient` + wire into labeling

**Files:**
- Create: `radis/labels/throttled_client.py`
- Modify: `radis/labels/labeling.py` (imports + the `client = ...` line, ~line 40)
- Test: `radis/labels/tests/test_throttled_client.py`

**Interfaces:**
- Consumes: `ChatClient` (Task 5), `RateLimitGate`/`run_through_gate`/`with_transient_retries` (Tasks 1,3,4), all rate-limit settings (Task 6), `make_rate_limit_error`/`make_connection_error` (Tasks 2,4), `create_labeling_openai_mock` (existing helper).
- Produces:
  - `_LABELING_GATE: RateLimitGate` (module global in `throttled_client.py`).
  - `ThrottledChatClient(client: ChatClient)` with `extract_data(prompt: str, schema: type[BaseModel]) -> BaseModel`.

- [ ] **Step 1: Write the failing integration tests**

Create `radis/labels/tests/test_throttled_client.py`:

```python
"""label_report runs the real ChatClient + gate with only openai.OpenAI mocked, so a 429 or a
transient error is handled end-to-end (retry/defer) and results still persist."""

from unittest.mock import patch

import pytest

from radis.chats.utils.testing_helpers import make_connection_error, make_rate_limit_error
from radis.labels.factories import LabelFactory, LabelGroupFactory
from radis.labels.labeling import label_report
from radis.labels.models import GateAnswer, LabelResult
from radis.labels.tests.helpers import create_labeling_openai_mock
from radis.labels.throttled_client import _LABELING_GATE
from radis.reports.factories import ReportFactory


@pytest.fixture(autouse=True)
def reset_labeling_gate():
    _LABELING_GATE.reset()
    yield
    _LABELING_GATE.reset()


def _inject_failures(client_mock, error, times):
    """Make the first `times` parse calls raise `error`, then behave normally."""
    normal = client_mock.beta.chat.completions.parse.side_effect
    state = {"left": times}

    def flaky(**kwargs):
        if state["left"] > 0:
            state["left"] -= 1
            raise error
        return normal(**kwargs)

    client_mock.beta.chat.completions.parse.side_effect = flaky
    return client_mock


@pytest.mark.django_db
def test_label_report_recovers_after_one_rate_limit():
    report = ReportFactory.create(body="CT thorax: consolidation right lower lobe.")
    group = LabelGroupFactory.create(name="Chest")
    label = LabelFactory.create(group=group, name="pneumonia")

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"}, label_values={label.name: "PRESENT"}
    )
    _inject_failures(client, make_rate_limit_error({"retry-after": "0"}), times=1)

    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    assert LabelResult.objects.get(report=report, label=label).value == LabelResult.Value.PRESENT


@pytest.mark.django_db
def test_label_report_recovers_after_one_transient_error(settings):
    settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS = 0.0  # no real sleep in the test
    report = ReportFactory.create(body="MRI head: no acute infarct.")
    group = LabelGroupFactory.create(name="Neuro")
    label = LabelFactory.create(group=group, name="stroke")

    client = create_labeling_openai_mock(
        gate_values={group.name: "YES"}, label_values={label.name: "ABSENT"}
    )
    _inject_failures(client, make_connection_error(), times=1)

    with patch("openai.OpenAI", return_value=client):
        label_report(report.pk)

    assert LabelResult.objects.get(report=report, label=label).value == LabelResult.Value.ABSENT


@pytest.mark.django_db
def test_label_report_defers_when_rate_limit_exceeds_budget():
    from radis.chats.utils.rate_limit import RateLimited

    report = ReportFactory.create(body="CT abdomen: normal.")
    group = LabelGroupFactory.create(name="Abdomen")
    LabelFactory.create(group=group, name="appendicitis")

    client = create_labeling_openai_mock(gate_values={group.name: "YES"})
    # retry-after 600 > 300 budget -> give up on the first call.
    _inject_failures(client, make_rate_limit_error({"retry-after": "600"}), times=1)

    with patch("openai.OpenAI", return_value=client):
        with pytest.raises(RateLimited):
            label_report(report.pk)

    assert not GateAnswer.objects.filter(report=report).exists()
    assert not LabelResult.objects.filter(report=report).exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/labels/tests/test_throttled_client.py -v`
Expected: FAIL — `ModuleNotFoundError: radis.labels.throttled_client`.

- [ ] **Step 3: Create `ThrottledChatClient`**

Create `radis/labels/throttled_client.py`:

```python
from django.conf import settings
from pydantic import BaseModel

from radis.chats.utils.chat_client import ChatClient
from radis.chats.utils.rate_limit import (
    RateLimitGate,
    run_through_gate,
    with_transient_retries,
)

# Process-global so every labeling thread in this worker shares one backoff window.
_LABELING_GATE = RateLimitGate(
    base_seconds=settings.LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    fallback_max_seconds=settings.LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS,
    header_ceiling_seconds=settings.LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS,
)


class ThrottledChatClient:
    """ChatClient wrapper that routes every call through a small transient retry and
    the shared rate-limit gate. Same extract_data surface as ChatClient."""

    def __init__(self, client: ChatClient) -> None:
        self._client = client

    def extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        return run_through_gate(
            _LABELING_GATE,
            settings.LABELING_RATE_LIMIT_MAX_WAIT_SECONDS,
            lambda: with_transient_retries(
                lambda: self._client.extract_data(prompt, schema),
                settings.LABELING_TRANSIENT_RETRY_ATTEMPTS,
                settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )
```

- [ ] **Step 4: Wire it into `label_report`**

In `radis/labels/labeling.py`, add the import near the other imports:

```python
from radis.labels.throttled_client import ThrottledChatClient
```

Replace the client construction (`client = ChatClient()`, ~line 40):

```python
    client = ThrottledChatClient(
        ChatClient(max_retries=0, timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS)
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/labels/tests/test_throttled_client.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the wider labeling suite to confirm no regressions**

Run: `uv run cli test -- radis/labels/tests/test_labeling.py radis/labels/tests/test_labeling_integration.py -v`
Expected: PASS (the existing flow is unchanged; the client is just wrapped).

- [ ] **Step 7: Commit**

```bash
git add radis/labels/throttled_client.py radis/labels/labeling.py radis/labels/tests/test_throttled_client.py
git commit -m "feat(labels): route labeling LLM calls through ThrottledChatClient"
```

---

### Task 8: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full chats + labels suites**

Run: `uv run cli test -- radis/chats/ radis/labels/ -v`
Expected: PASS (no regressions; the new tests pass).

- [ ] **Step 2: Lint and type-check**

Run: `uv run cli lint`
Expected: clean (ruff + djlint). Fix any line-length/import-order issues in the new files.

- [ ] **Step 3: Confirm chat is untouched**

Verify by inspection that `AsyncChatClient` and any chat call sites do not pass `max_retries`/`timeout` and do not import `rate_limit` — only `radis/labels/` uses the gate. No code change expected.

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -A
git commit -m "chore: lint fixes for rate-limit handling"
```

---

## Self-Review Notes

- **Spec coverage:** gate honoring full `Retry-After` ≤ budget and clamping the exponential only (Task 1); `Retry-After` > budget defer using the raw header + arm-before-defer (Task 3); budget-aware `wait_until_open` (Task 1); header ceiling vs fallback cap as two settings (Tasks 1, 6); `with_transient_retries` with 429 passthrough (Task 4); `max_retries=0` + timeout (Tasks 5, 7); env configurability (Task 6); integration recovery + over-budget defer (Task 7). Recovery-via-manual-backfill is unchanged behavior — no task needed; documented in the spec.
- **No across-run/scan changes:** intentional, per the spec's accepted limitation.
- **Type consistency:** `RateLimitGate` ctor params (`base_seconds`, `fallback_max_seconds`, `header_ceiling_seconds`) match between Task 1 and the Task 7 singleton; `run_through_gate(gate, budget, fn, now)` and `with_transient_retries(fn, attempts, base, sleep)` signatures match across tasks.
