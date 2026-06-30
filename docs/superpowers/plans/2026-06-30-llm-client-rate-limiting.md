# Shared LLM Client with Built-in Rate Limiting — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the LLM client code into `radis/core`, rename the sync `ChatClient` to `LLMClient` (keeping `AsyncChatClient`'s name), and apply always-on, per-process rate limiting to both clients so every app (chats, extractions, subscriptions) shares one backoff window.

**Architecture:** A single per-process `RateLimitGate` (in `radis/core/utils/rate_limit.py`) coordinates backoff for both the sync `LLMClient.extract_data` and the async `AsyncChatClient.chat`. The gate keeps its sync API and gains async wait helpers (`asyncio.sleep`) so sync and async callers share one in-process window. Throttling is folded directly into the clients (`radis/core/utils/llm_client.py`); there is no separate wrapper. `radis/chats/utils/chat_client.py` is deleted (moved to core).

**Tech Stack:** Python 3.12+, Django 5.1+, OpenAI Python SDK (`openai`), `environs` for settings, `httpx` (test error builders), pytest / pytest-asyncio.

## Global Constraints

- Line length: 100 (Ruff). Style: Google Python Style Guide.
- Type checking: pyright basic mode.
- Lint/format/typecheck command: `uv run cli lint`. Format: `uv run cli format-code`. Tests: `uv run cli test`.
- Branch: `feature/llm-client-rate-limiting` (already created off `main`). Do **not** touch `feature/auto-labeling_muhammad`.
- Settings names are exactly: `LLM_REQUEST_TIMEOUT_SECONDS`, `LLM_EXTRA_BODY`, `LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS`, `LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS`, `LLM_RATE_LIMIT_HEADER_CEILING_SECONDS`, `LLM_RATE_LIMIT_MAX_WAIT_SECONDS`, `LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS`, `LLM_TRANSIENT_RETRY_ATTEMPTS`, `LLM_TRANSIENT_RETRY_BASE_SECONDS`.
- `extract_data` sends the prompt as a **user** message and passes `extra_body`. The async `chat()` path does **not** apply `extra_body`.
- Both clients construct their OpenAI SDK client with `max_retries=0` and `timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS` (the gate owns backoff).

## File Structure

- `radis/settings/base.py` — add the LLM rate-limit settings (modify).
- `example.env` — document the new env vars (modify).
- `radis/chats/utils/testing_helpers.py` — add `make_rate_limit_error` / `make_connection_error` (modify).
- `radis/core/utils/rate_limit.py` — gate + sync/async run helpers (create).
- `radis/core/utils/llm_client.py` — `LLMClient`, `AsyncChatClient`, module-global `_LLM_GATE` (create).
- `radis/core/tests/test_rate_limit.py` — gate + helper tests (create).
- `radis/core/tests/test_llm_client.py` — client throttling tests (create).
- `radis/core/tests/test_llm_settings.py` — settings-defaults test (create).
- `radis/extractions/processors.py`, `radis/subscriptions/processors.py` — import `LLMClient` from core (modify).
- `radis/chats/views.py` — import `AsyncChatClient` from core + handle `RateLimited` (modify).
- `radis/chats/templates/chats/chat.html` — inline error alert (modify).
- `radis/chats/utils/chat_client.py` — delete.

---

### Task 1: LLM rate-limit settings + example.env

**Files:**
- Modify: `radis/settings/base.py` (after the `LLM_SERVICE_URL` line, ~line 337)
- Modify: `example.env` (LLM configuration section, after `LLAMACPP_USE_GPU=false`, ~line 122)
- Test: `radis/core/tests/test_llm_settings.py` (create)

**Interfaces:**
- Produces: Django settings `LLM_REQUEST_TIMEOUT_SECONDS: float`, `LLM_EXTRA_BODY: dict`, `LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS: float`, `LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS: float`, `LLM_RATE_LIMIT_HEADER_CEILING_SECONDS: float`, `LLM_RATE_LIMIT_MAX_WAIT_SECONDS: float`, `LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS: float`, `LLM_TRANSIENT_RETRY_ATTEMPTS: int`, `LLM_TRANSIENT_RETRY_BASE_SECONDS: float`.

- [ ] **Step 1: Write the failing test**

Create `radis/core/tests/test_llm_settings.py`:

```python
from django.conf import settings as dj_settings


def test_llm_rate_limit_settings_have_expected_defaults():
    assert dj_settings.LLM_REQUEST_TIMEOUT_SECONDS == 60.0
    assert dj_settings.LLM_EXTRA_BODY == {"chat_template_kwargs": {"enable_thinking": False}}
    assert dj_settings.LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS == 5.0
    assert dj_settings.LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS == 120.0
    assert dj_settings.LLM_RATE_LIMIT_HEADER_CEILING_SECONDS == 3600.0
    assert dj_settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS == 300.0
    assert dj_settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS == 20.0
    assert dj_settings.LLM_TRANSIENT_RETRY_ATTEMPTS == 2
    assert dj_settings.LLM_TRANSIENT_RETRY_BASE_SECONDS == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run cli test -- radis/core/tests/test_llm_settings.py -v`
Expected: FAIL with `AttributeError` (e.g. `LLM_REQUEST_TIMEOUT_SECONDS`).

- [ ] **Step 3: Add the settings**

In `radis/settings/base.py`, immediately after the `LLM_SERVICE_URL = ...` line, insert:

```python
LLM_REQUEST_TIMEOUT_SECONDS = env.float("LLM_REQUEST_TIMEOUT_SECONDS", default=60.0)
# Provider quirks (e.g. Qwen's enable_thinking flag) sent with each extract_data call.
LLM_EXTRA_BODY = env.json(
    "LLM_EXTRA_BODY", default={"chat_template_kwargs": {"enable_thinking": False}}
)
# Rate-limit gate: one per-process backoff window shared by every LLM client.
LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS = env.float(
    "LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS", default=5.0
)
LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS = env.float(
    "LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS", default=120.0
)
LLM_RATE_LIMIT_HEADER_CEILING_SECONDS = env.float(
    "LLM_RATE_LIMIT_HEADER_CEILING_SECONDS", default=3600.0
)
# Per-call wait budgets: long for batch jobs, short for interactive chat.
LLM_RATE_LIMIT_MAX_WAIT_SECONDS = env.float("LLM_RATE_LIMIT_MAX_WAIT_SECONDS", default=300.0)
LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS = env.float(
    "LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS", default=20.0
)
LLM_TRANSIENT_RETRY_ATTEMPTS = env.int("LLM_TRANSIENT_RETRY_ATTEMPTS", default=2)
LLM_TRANSIENT_RETRY_BASE_SECONDS = env.float("LLM_TRANSIENT_RETRY_BASE_SECONDS", default=1.0)
```

- [ ] **Step 4: Document the env vars in example.env**

In `example.env`, after the `LLAMACPP_USE_GPU=false` line, insert:

```bash
# LLM resilience. extract_data passes LLM_EXTRA_BODY (JSON) to the provider; the default
# disables provider "thinking" for cleaner structured output. The rate-limit gate makes every
# LLM caller in a process back off together on a 429. Wait budgets cap how long a single call
# waits out a 429 before deferring (long for batch jobs, short for interactive chat).
LLM_REQUEST_TIMEOUT_SECONDS=60.0
LLM_EXTRA_BODY='{"chat_template_kwargs": {"enable_thinking": false}}'
LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS=5.0
LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS=120.0
LLM_RATE_LIMIT_HEADER_CEILING_SECONDS=3600.0
LLM_RATE_LIMIT_MAX_WAIT_SECONDS=300.0
LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS=20.0
LLM_TRANSIENT_RETRY_ATTEMPTS=2
LLM_TRANSIENT_RETRY_BASE_SECONDS=1.0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run cli test -- radis/core/tests/test_llm_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py example.env radis/core/tests/test_llm_settings.py
git commit -m "feat(core): add LLM rate-limit settings"
```

---

### Task 2: LLM test error builders

**Files:**
- Modify: `radis/chats/utils/testing_helpers.py`
- Test: covered indirectly; verified by an inline check in Step 2.

**Interfaces:**
- Produces:
  - `make_rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError`
  - `make_connection_error() -> openai.APIConnectionError`

- [ ] **Step 1: Add the builders**

Edit `radis/chats/utils/testing_helpers.py`. Add `import httpx` to the imports (keep existing `import asyncio`, `from unittest.mock import MagicMock`, `import openai`, `from faker import Faker`, `from pydantic import BaseModel`). Append at the end of the file:

```python
def make_rate_limit_error(headers: dict[str, str] | None = None) -> openai.RateLimitError:
    """Build a real openai.RateLimitError carrying chosen response headers (e.g. retry-after)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    response = httpx.Response(429, headers=headers or {}, request=request)
    return openai.RateLimitError("rate limited", response=response, body=None)


def make_connection_error() -> openai.APIConnectionError:
    """Build a real openai.APIConnectionError (a transient, non-429 error)."""
    request = httpx.Request("POST", "http://testserver/v1/chat/completions")
    return openai.APIConnectionError(message="connection failed", request=request)
```

- [ ] **Step 2: Verify the builders import and construct**

Run:
```bash
uv run python -c "from radis.chats.utils.testing_helpers import make_rate_limit_error, make_connection_error; e=make_rate_limit_error({'retry-after':'30'}); print(e.response.headers.get('retry-after')); print(type(make_connection_error()).__name__)"
```
Expected output:
```
30
APIConnectionError
```

- [ ] **Step 3: Commit**

```bash
git add radis/chats/utils/testing_helpers.py
git commit -m "test: add LLM rate-limit error builders"
```

---

### Task 3: Rate-limit gate + sync/async run helpers in core

**Files:**
- Create: `radis/core/utils/rate_limit.py`
- Test: `radis/core/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: `make_rate_limit_error`, `make_connection_error` (Task 2).
- Produces:
  - `class RateLimited(Exception)`
  - `class RateLimitGate` with `__init__(base_seconds, fallback_max_seconds, header_ceiling_seconds, now=time.monotonic, sleep=time.sleep, async_sleep=asyncio.sleep)`, `reset()`, `note_success()`, `note_rate_limited(retry_after: float | None) -> float`, `wait_until_open(deadline: float) -> bool`, `async wait_until_open_async(deadline: float) -> bool`
  - `run_through_gate[T](gate, budget: float, fn: Callable[[], T], now=time.monotonic) -> T`
  - `async run_through_gate_async[T](gate, budget: float, fn: Callable[[], Awaitable[T]], now=time.monotonic) -> T`
  - `with_transient_retries[T](fn: Callable[[], T], attempts: int, base: float, sleep=time.sleep) -> T`
  - `async with_transient_retries_async[T](fn: Callable[[], Awaitable[T]], attempts: int, base: float, sleep=asyncio.sleep) -> T`
  - `_parse_retry_after(exc: openai.RateLimitError) -> float | None`

- [ ] **Step 1: Write the failing tests**

Create `radis/core/tests/test_rate_limit.py`:

```python
import asyncio
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_rate_limit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.core.utils.rate_limit'`.

- [ ] **Step 3: Write the implementation**

Create `radis/core/utils/rate_limit.py`:

```python
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
            effective = retry_after if retry_after is not None else pause
            logger.warning("Rate-limited; backing off %.1fs", effective)
            if now() + effective > deadline:
                raise RateLimited() from exc  # can't wait it out; defer


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
            pause = gate.note_rate_limited(retry_after)
            effective = retry_after if retry_after is not None else pause
            logger.warning("Rate-limited; backing off %.1fs", effective)
            if now() + effective > deadline:
                raise RateLimited() from exc


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/core/tests/test_rate_limit.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add radis/core/utils/rate_limit.py radis/core/tests/test_rate_limit.py
git commit -m "feat(core): add rate-limit gate with sync and async helpers"
```

---

### Task 4: LLMClient + AsyncChatClient in core

**Files:**
- Create: `radis/core/utils/llm_client.py`
- Test: `radis/core/tests/test_llm_client.py`

**Interfaces:**
- Consumes: `RateLimitGate`, `run_through_gate`, `run_through_gate_async`, `with_transient_retries`, `with_transient_retries_async` (Task 3); settings from Task 1; `make_rate_limit_error`, `create_openai_client_mock`, `create_async_openai_client_mock` (test helpers).
- Produces:
  - `_LLM_GATE: RateLimitGate` (module global)
  - `class LLMClient` with `extract_data(prompt: str, schema: type[BaseModel], max_wait: float | None = None) -> BaseModel`
  - `class AsyncChatClient` with `async chat(messages: Iterable[ChatCompletionMessageParam], max_completion_tokens: int | None = None, max_wait: float | None = None) -> str`

- [ ] **Step 1: Write the failing tests**

Create `radis/core/tests/test_llm_client.py`:

```python
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from radis.chats.utils.testing_helpers import (
    create_async_openai_client_mock,
    create_openai_client_mock,
    make_rate_limit_error,
)
from radis.core.utils.llm_client import _LLM_GATE, AsyncChatClient, LLMClient
from radis.core.utils.rate_limit import RateLimited


class _Schema(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def reset_gate():
    _LLM_GATE.reset()
    yield
    _LLM_GATE.reset()


def test_llm_client_sets_max_retries_and_timeout(settings):
    settings.LLM_REQUEST_TIMEOUT_SECONDS = 42.0
    with patch("openai.OpenAI") as openai_cls:
        LLMClient()
    kwargs = openai_cls.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == 42.0


def test_extract_data_uses_user_message_and_extra_body(settings):
    settings.LLM_EXTRA_BODY = {"foo": "bar"}
    mock = create_openai_client_mock(_Schema(value="hi"))
    with patch("openai.OpenAI", return_value=mock):
        result = LLMClient().extract_data("the prompt", _Schema)
    assert isinstance(result, _Schema)
    call = mock.beta.chat.completions.parse.call_args.kwargs
    assert call["messages"] == [{"role": "user", "content": "the prompt"}]
    assert call["extra_body"] == {"foo": "bar"}


def test_extract_data_recovers_after_one_rate_limit(settings):
    settings.LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS = 0.0  # no real wait
    mock = create_openai_client_mock(_Schema(value="ok"))
    calls = {"n": 0}
    success_response = mock.beta.chat.completions.parse.return_value

    def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise make_rate_limit_error({"retry-after": "0"})
        return success_response

    mock.beta.chat.completions.parse.side_effect = flaky
    with patch("openai.OpenAI", return_value=mock):
        result = LLMClient().extract_data("p", _Schema)
    assert result.value == "ok"
    assert calls["n"] == 2


def test_extract_data_defers_when_rate_limit_exceeds_budget():
    mock = create_openai_client_mock(_Schema(value="never"))

    def always_429(**kwargs):
        raise make_rate_limit_error({"retry-after": "600"})

    mock.beta.chat.completions.parse.side_effect = always_429
    with patch("openai.OpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            LLMClient().extract_data("p", _Schema, max_wait=300.0)


@pytest.mark.asyncio
async def test_chat_returns_content():
    mock = create_async_openai_client_mock("the answer")
    with patch("openai.AsyncOpenAI", return_value=mock):
        answer = await AsyncChatClient().chat([{"role": "user", "content": "hi"}])
    assert answer == "the answer"


@pytest.mark.asyncio
async def test_chat_defers_when_rate_limit_exceeds_budget():
    mock = create_async_openai_client_mock("never")

    async def always_429(**kwargs):
        raise make_rate_limit_error({"retry-after": "600"})

    mock.chat.completions.create.side_effect = always_429
    with patch("openai.AsyncOpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            await AsyncChatClient().chat([{"role": "user", "content": "hi"}], max_wait=20.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_llm_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'radis.core.utils.llm_client'`.

- [ ] **Step 3: Write the implementation**

Create `radis/core/utils/llm_client.py`:

```python
import logging
from collections.abc import Iterable

import openai
from django.conf import settings
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from radis.core.utils.rate_limit import (
    RateLimitGate,
    run_through_gate,
    run_through_gate_async,
    with_transient_retries,
    with_transient_retries_async,
)

logger = logging.getLogger(__name__)

# Process-global so every LLM caller in this worker/web process shares one backoff window.
_LLM_GATE = RateLimitGate(
    base_seconds=settings.LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    fallback_max_seconds=settings.LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS,
    header_ceiling_seconds=settings.LLM_RATE_LIMIT_HEADER_CEILING_SECONDS,
)


def _get_base_url() -> str:
    base_url = settings.EXTERNAL_LLM_PROVIDER_URL
    if not base_url:
        base_url = settings.LLM_SERVICE_URL
    return base_url


class AsyncChatClient:
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        self._model_name = settings.LLM_MODEL_NAME

    async def chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None = None,
        max_wait: float | None = None,
    ) -> str:
        if max_wait is None:
            max_wait = settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS

        return await run_through_gate_async(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries_async(
                lambda: self._chat(messages, max_completion_tokens),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )

    async def _chat(
        self,
        messages: Iterable[ChatCompletionMessageParam],
        max_completion_tokens: int | None,
    ) -> str:
        logger.debug(f"Sending messages to LLM for chat:\n{messages}")
        request = {"model": self._model_name, "messages": messages}
        if max_completion_tokens is not None:
            request["max_completion_tokens"] = max_completion_tokens

        completion = await self._client.chat.completions.create(**request)
        answer = completion.choices[0].message.content
        assert answer is not None
        logger.debug("Received from LLM: %s", answer)
        return answer


class LLMClient:
    def __init__(self) -> None:
        base_url = _get_base_url()
        api_key = settings.EXTERNAL_LLM_PROVIDER_API_KEY
        # max_retries=0 so the gate fully owns backoff (no hidden SDK retries).
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            max_retries=0,
            timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS,
        )
        self._llm_model_name = settings.LLM_MODEL_NAME
        # Provider quirks (e.g. Qwen's enable_thinking flag) sent with each call.
        self._extra_body: dict = getattr(settings, "LLM_EXTRA_BODY", {}) or {}

    def extract_data(
        self,
        prompt: str,
        schema: type[BaseModel],
        max_wait: float | None = None,
    ) -> BaseModel:
        if max_wait is None:
            max_wait = settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS

        return run_through_gate(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries(
                lambda: self._extract_data(prompt, schema),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )

    def _extract_data(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        logger.debug("Sending prompt and schema to LLM to extract data.")
        logger.debug("Prompt:\n%s", prompt)
        logger.debug("Schema:\n%s", schema.model_json_schema())

        completion = self._client.beta.chat.completions.parse(
            model=self._llm_model_name,
            messages=[{"role": "user", "content": prompt}],
            response_format=schema,
            extra_body=self._extra_body,
        )
        event = completion.choices[0].message.parsed
        assert event
        logger.debug("Received from LLM: %s", event)
        return event
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run cli test -- radis/core/tests/test_llm_client.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add radis/core/utils/llm_client.py radis/core/tests/test_llm_client.py
git commit -m "feat(core): add throttled LLMClient and AsyncChatClient"
```

---

### Task 5: Migrate callers and remove old chat_client

**Files:**
- Modify: `radis/extractions/processors.py:8` (import)
- Modify: `radis/subscriptions/processors.py:9` (import)
- Modify: `radis/chats/views.py` (import + RateLimited handling)
- Modify: `radis/chats/templates/chats/chat.html` (inline error alert)
- Delete: `radis/chats/utils/chat_client.py`

**Interfaces:**
- Consumes: `LLMClient`, `AsyncChatClient` from `radis.core.utils.llm_client`; `RateLimited` from `radis.core.utils.rate_limit` (Task 4).

- [ ] **Step 1: Repoint extractions and subscriptions imports**

In `radis/extractions/processors.py`, replace:
```python
from radis.chats.utils.chat_client import ChatClient
```
with:
```python
from radis.core.utils.llm_client import LLMClient
```
and replace `self.client = ChatClient()` with `self.client = LLMClient()`.

In `radis/subscriptions/processors.py`, make the identical two replacements (`ChatClient` import on line 9, `self.client = ChatClient()` on line 29).

- [ ] **Step 2: Repoint chats/views.py import and add RateLimited handling**

In `radis/chats/views.py`, replace the import on line 22:
```python
from .utils.chat_client import AsyncChatClient
```
with:
```python
from radis.core.utils.llm_client import AsyncChatClient
from radis.core.utils.rate_limit import RateLimited
```

In `chat_create_view`, wrap the two `client.chat(...)` calls. Replace this block:
```python
            client = AsyncChatClient()

            # Generate an answer for the user prompt
            answer = await client.chat(
                [
                    {"role": "system", "content": instructions_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            # Generate a title for the chat
            title_system_prompt = Template(settings.CHAT_GENERATE_TITLE_SYSTEM_PROMPT).substitute(
                {"num_words": 6, "user_prompt": user_prompt, "assistant_response": answer}
            )

            title = await client.chat(
                [
                    {"role": "system", "content": title_system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_completion_tokens=20,
            )
            title = title.strip().rstrip(string.punctuation)[:100]
```
with:
```python
            client = AsyncChatClient()

            try:
                # Generate an answer for the user prompt
                answer = await client.chat(
                    [
                        {"role": "system", "content": instructions_system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )

                # Generate a title for the chat
                title_system_prompt = Template(
                    settings.CHAT_GENERATE_TITLE_SYSTEM_PROMPT
                ).substitute(
                    {"num_words": 6, "user_prompt": user_prompt, "assistant_response": answer}
                )

                title = await client.chat(
                    [
                        {"role": "system", "content": title_system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    max_completion_tokens=20,
                )
            except RateLimited:
                return render(
                    request,
                    "chats/_chat.html",
                    {
                        "chat": None,
                        "report": report,
                        "chat_messages": [],
                        "form": form,
                        "error": "The LLM service is busy. Please try again in a moment.",
                    },
                )
            title = title.strip().rstrip(string.punctuation)[:100]
```

In `chat_update_view`, replace this block:
```python
        client = AsyncChatClient()
        response = await client.chat(messages)

        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=prompt)
        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=response)

        form = PromptForm()
```
with:
```python
        client = AsyncChatClient()
        try:
            response = await client.chat(messages)
        except RateLimited:
            return render(
                request,
                "chats/_chat.html",
                {
                    "chat": chat,
                    "report": chat.report,
                    "chat_messages": [
                        message
                        async for message in chat.messages.exclude(
                            role=ChatRole.SYSTEM
                        ).order_by("id")
                    ],
                    "form": form,
                    "error": "The LLM service is busy. Please try again in a moment.",
                },
            )

        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.USER, content=prompt)
        await ChatMessage.objects.acreate(chat=chat, role=ChatRole.ASSISTANT, content=response)

        form = PromptForm()
```

> Note: in `chat_update_view` the local variable `messages` (the list of `ChatCompletionMessageParam`) shadows the `django.contrib.messages` module, so the error is surfaced via the `error` template context variable rather than the Django messages framework. The HTMX response swaps `#chat-with-form`, and the base-layout messages panel is not part of that swap, so the inline alert (Step 3) is what the user actually sees.

- [ ] **Step 3: Add the inline error alert to chat.html**

In `radis/chats/templates/chats/chat.html`, inside `{% block content %}`, immediately after the opening `<div id="chat-with-form">` line, insert:
```html
        {% if error %}
            <div class="alert alert-warning" role="alert">{{ error }}</div>
        {% endif %}
```

- [ ] **Step 4: Delete the old chat_client module**

Run:
```bash
git rm radis/chats/utils/chat_client.py
```

- [ ] **Step 5: Verify no references to the old module remain**

Run: `grep -rn "chats.utils.chat_client\|utils.chat_client import" radis/`
Expected: no output.

- [ ] **Step 6: Run the affected app test suites**

Run: `uv run cli test -- radis/extractions radis/subscriptions radis/chats radis/core -v`
Expected: PASS (existing extractions processor test that patches `openai.OpenAI` still passes; no import errors).

- [ ] **Step 7: Commit**

```bash
git add radis/extractions/processors.py radis/subscriptions/processors.py radis/chats/views.py radis/chats/templates/chats/chat.html
git commit -m "refactor: use core LLMClient with built-in rate limiting across apps"
```

---

### Task 6: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Lint, format check, and type check**

Run: `uv run cli lint`
Expected: PASS (ruff + djlint + pyright, no errors). If ruff reports formatting, run `uv run cli format-code` and re-run; commit any formatting changes with `git commit -am "style: format"`.

- [ ] **Step 2: Run the full test suite**

Run: `uv run cli test`
Expected: PASS (no failures, no errors). Acceptance tests requiring dev containers may be skipped/deselected as usual.

- [ ] **Step 3: Confirm the diff matches the spec**

Run: `git diff main --stat`
Expected files changed: `radis/settings/base.py`, `example.env`, `radis/chats/utils/testing_helpers.py`, `radis/core/utils/rate_limit.py`, `radis/core/utils/llm_client.py`, `radis/core/tests/test_rate_limit.py`, `radis/core/tests/test_llm_client.py`, `radis/core/tests/test_llm_settings.py`, `radis/extractions/processors.py`, `radis/subscriptions/processors.py`, `radis/chats/views.py`, `radis/chats/templates/chats/chat.html`, and deletion of `radis/chats/utils/chat_client.py`, plus the two design/plan docs.

---

## Acceptance Criteria (from spec)

- `radis/core/utils/llm_client.py` exports `LLMClient` and `AsyncChatClient`, both throttled, sharing `_LLM_GATE`. ✔ Task 4
- `radis/chats/utils/chat_client.py` no longer exists; chats, extractions, subscriptions import from core. ✔ Task 5
- A sustained 429 in one caller backs off other LLM callers in the same process (shared gate). ✔ Tasks 3–4
- Interactive chat gives up after the interactive budget with a friendly message; batch jobs use the long budget. ✔ Tasks 4–5
- `uv run cli lint` and `uv run cli test` pass. ✔ Task 6
