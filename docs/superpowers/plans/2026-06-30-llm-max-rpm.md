# Proactive LLM_MAX_RPM Request Cap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-process proactive cap of `LLM_MAX_RPM` LLM requests per minute, applied to every LLM request from both `LLMClient` and `AsyncChatClient`, integrated with the existing wait-budget/`RateLimited` flow.

**Architecture:** A new hand-rolled token-bucket `RpmLimiter` is added to `radis/core/utils/rate_limit.py` next to the existing `RateLimitGate`. `run_through_gate` / `run_through_gate_async` gain an optional `rpm` barrier acquired (under the same per-call deadline) after the 429 gate opens and before the request runs. A module-global `_LLM_RPM_LIMITER` in `llm_client.py` is shared by both clients. The cap is per-process (consistent with the existing gate) and disabled by default.

**Tech Stack:** Python 3.12+, Django 5.1+, `environs` for settings, pytest / pytest-asyncio. No new third-party dependency.

## Global Constraints

- Line length 100 (Ruff E/F/I/DJ/UP); Google Python Style Guide; pyright basic mode must pass.
- `LLM_MAX_RPM` caps **requests** per minute (not LLM tokens). Setting name is exactly `LLM_MAX_RPM`, type int, default `0`.
- `LLM_MAX_RPM <= 0` means **disabled**: every acquire is an immediate no-op returning `True`, leaving existing behavior and the current test suite unchanged.
- The bucket allows a **burst of up to `LLM_MAX_RPM`** requests, then refills at `LLM_MAX_RPM / 60` permits/sec.
- `RpmLimiter` uses the same concurrency discipline as `RateLimitGate`: a brief `threading.Lock` for permit math, **never held across a `time.sleep` or `await`**. It takes injectable `now`/`sleep`/`async_sleep` for deterministic tests.
- One permit is consumed per logical request: a 429 retry loops back through `run_through_gate` and consumes a new permit; transient connection/5xx retries inside `with_transient_retries` do not.
- The environment runs tests via `uv run cli test`, which execs `pytest` into the running `radis_dev-web-1` container; host files reach it via Docker Compose **watch sync** (a few seconds' lag). If a run looks stale, wait briefly and re-run; ensure the final run reflects the latest code before committing.

## File Structure

- `radis/settings/base.py` — add `LLM_MAX_RPM` (modify, after the `LLM_TRANSIENT_RETRY_BASE_SECONDS` line).
- `example.env` — document `LLM_MAX_RPM` (modify, after `LLM_TRANSIENT_RETRY_BASE_SECONDS=1.0`).
- `radis/core/utils/rate_limit.py` — add `RpmLimiter`; add optional `rpm` param to `run_through_gate` / `run_through_gate_async` (modify).
- `radis/core/utils/llm_client.py` — add module-global `_LLM_RPM_LIMITER`; pass `rpm=_LLM_RPM_LIMITER` from both clients (modify).
- `radis/core/tests/test_rate_limit.py` — `RpmLimiter` unit tests + gate-integration tests (modify).
- `radis/core/tests/test_llm_settings.py` — assert `LLM_MAX_RPM` default (modify).
- `radis/core/tests/test_llm_client.py` — client-wiring/defer tests (modify).

---

### Task 1: `LLM_MAX_RPM` setting + example.env

**Files:**
- Modify: `radis/settings/base.py` (after `LLM_TRANSIENT_RETRY_BASE_SECONDS = ...`, line ~357)
- Modify: `example.env` (after `LLM_TRANSIENT_RETRY_BASE_SECONDS=1.0`, line ~136)
- Modify: `radis/core/tests/test_llm_settings.py`

**Interfaces:**
- Produces: Django setting `LLM_MAX_RPM: int` (default `0`).

- [ ] **Step 1: Add the failing assertion**

In `radis/core/tests/test_llm_settings.py`, add this line at the end of `test_llm_rate_limit_settings_have_expected_defaults` (the function already asserts the other `LLM_*` defaults):

```python
    assert dj_settings.LLM_MAX_RPM == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run cli test -- radis/core/tests/test_llm_settings.py -v`
Expected: FAIL with `AttributeError: ... LLM_MAX_RPM` (or assertion error).

- [ ] **Step 3: Add the setting**

In `radis/settings/base.py`, immediately after the `LLM_TRANSIENT_RETRY_BASE_SECONDS = env.float(...)` line, insert:

```python
# Proactive cap on LLM requests per minute (per process). 0 disables the cap.
LLM_MAX_RPM = env.int("LLM_MAX_RPM", default=0)
```

- [ ] **Step 4: Document in example.env**

In `example.env`, immediately after the `LLM_TRANSIENT_RETRY_BASE_SECONDS=1.0` line, insert:

```bash
# Proactive cap on the number of LLM requests sent per minute, per process. The single
# llm_worker process governs all batch (extraction/subscription) traffic. 0 disables the cap.
LLM_MAX_RPM=0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run cli test -- radis/core/tests/test_llm_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add radis/settings/base.py example.env radis/core/tests/test_llm_settings.py
git commit -m "feat(core): add LLM_MAX_RPM setting"
```

---

### Task 2: `RpmLimiter` + gate integration

**Files:**
- Modify: `radis/core/utils/rate_limit.py`
- Modify: `radis/core/tests/test_rate_limit.py`

**Interfaces:**
- Consumes: `make_gate`, `FakeClock`, `RateLimited`, `run_through_gate`, `run_through_gate_async` (existing in `test_rate_limit.py` / `rate_limit.py`).
- Produces:
  - `class RpmLimiter` with `__init__(max_rpm: int, now=time.monotonic, sleep=time.sleep, async_sleep=asyncio.sleep)`, `reset()`, `acquire(deadline: float) -> bool`, `acquire_async(deadline: float) -> bool`.
  - `run_through_gate(..., rpm: RpmLimiter | None = None)` and `run_through_gate_async(..., rpm: RpmLimiter | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `radis/core/tests/test_rate_limit.py`. First, add `RpmLimiter` to the existing import from `radis.core.utils.rate_limit` (the import block at the top currently imports `RateLimited, RateLimitGate, _parse_retry_after, run_through_gate, run_through_gate_async, with_transient_retries, with_transient_retries_async` — add `RpmLimiter` to it). Then append these tests at the end of the file:

```python
# --- RpmLimiter ---

def test_rpm_limiter_disabled_is_noop():
    clock = FakeClock()
    limiter = RpmLimiter(0, now=clock.now, sleep=clock.sleep)
    assert limiter.acquire(clock.now() + 1.0) is True
    assert clock.slept == []


def test_rpm_limiter_allows_burst_up_to_capacity():
    clock = FakeClock()
    limiter = RpmLimiter(3, now=clock.now, sleep=clock.sleep)
    # Starts full: 3 permits consumable back-to-back with no wait.
    assert limiter.acquire(clock.now() + 100.0) is True
    assert limiter.acquire(clock.now() + 100.0) is True
    assert limiter.acquire(clock.now() + 100.0) is True
    assert clock.slept == []


def test_rpm_limiter_waits_for_refill_within_budget():
    clock = FakeClock()
    limiter = RpmLimiter(1, now=clock.now, sleep=clock.sleep)  # rate = 1/60 permits/sec
    assert limiter.acquire(clock.now() + 1.0) is True  # consume the starting permit
    # Next permit is 60s away; budget allows it.
    assert limiter.acquire(clock.now() + 120.0) is True
    assert clock.slept == [60.0]


def test_rpm_limiter_defers_when_refill_exceeds_deadline():
    clock = FakeClock()
    limiter = RpmLimiter(1, now=clock.now, sleep=clock.sleep)
    assert limiter.acquire(clock.now() + 1.0) is True  # drain the permit
    # Next permit is 60s away; deadline is only 30s out -> defer without sleeping/consuming.
    assert limiter.acquire(clock.now() + 30.0) is False
    assert clock.slept == []
    # Still nothing consumed beyond the first: a 30s-budget retry still defers.
    assert limiter.acquire(clock.now() + 30.0) is False


def test_rpm_limiter_reset_refills_to_full():
    clock = FakeClock()
    limiter = RpmLimiter(1, now=clock.now, sleep=clock.sleep)
    assert limiter.acquire(clock.now() + 1.0) is True  # drain
    limiter.reset()
    assert limiter.acquire(clock.now() + 1.0) is True  # full again, no wait
    assert clock.slept == []


@pytest.mark.asyncio
async def test_rpm_limiter_async_waits_for_refill_within_budget():
    clock = FakeClock()
    limiter = RpmLimiter(1, now=clock.now, sleep=clock.sleep, async_sleep=clock.async_sleep)
    assert await limiter.acquire_async(clock.now() + 1.0) is True
    assert await limiter.acquire_async(clock.now() + 120.0) is True
    assert clock.slept == [60.0]


@pytest.mark.asyncio
async def test_rpm_limiter_async_defers_when_refill_exceeds_deadline():
    clock = FakeClock()
    limiter = RpmLimiter(1, now=clock.now, sleep=clock.sleep, async_sleep=clock.async_sleep)
    assert await limiter.acquire_async(clock.now() + 1.0) is True
    assert await limiter.acquire_async(clock.now() + 30.0) is False
    assert clock.slept == []


# --- run_through_gate with an RpmLimiter ---

def test_run_through_gate_runs_when_rpm_permit_available():
    clock = FakeClock()
    gate = make_gate(clock)
    rpm = RpmLimiter(60, now=clock.now, sleep=clock.sleep)
    assert run_through_gate(gate, 300.0, lambda: "ok", now=clock.now, rpm=rpm) == "ok"
    assert clock.slept == []


def test_run_through_gate_defers_when_rpm_over_budget():
    clock = FakeClock()
    gate = make_gate(clock)
    rpm = RpmLimiter(1, now=clock.now, sleep=clock.sleep)
    assert rpm.acquire(clock.now() + 1.0) is True  # drain the only permit
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return "ok"

    with pytest.raises(RateLimited):
        run_through_gate(gate, 30.0, fn, now=clock.now, rpm=rpm)  # next permit 60s > 30 budget
    assert calls["n"] == 0  # fn never ran


@pytest.mark.asyncio
async def test_run_through_gate_async_defers_when_rpm_over_budget():
    clock = FakeClock()
    gate = make_gate(clock)
    rpm = RpmLimiter(1, now=clock.now, sleep=clock.sleep, async_sleep=clock.async_sleep)
    assert rpm.acquire(clock.now() + 1.0) is True
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        return "ok"

    with pytest.raises(RateLimited):
        await run_through_gate_async(gate, 30.0, fn, now=clock.now, rpm=rpm)
    assert calls["n"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_rate_limit.py -k "rpm or Rpm" -v`
Expected: FAIL — `ImportError: cannot import name 'RpmLimiter'` (collection error before any test runs).

- [ ] **Step 3: Add the `RpmLimiter` class**

In `radis/core/utils/rate_limit.py`, insert the following class immediately after the `RateLimitGate` class (after its `wait_until_open_async` method ends, i.e. after the current line 98 blank line and before `def _parse_retry_after`):

```python
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
```

- [ ] **Step 4: Add the `rpm` barrier to `run_through_gate`**

In `radis/core/utils/rate_limit.py`, change the `run_through_gate` signature and add the acquire after the gate opens. Replace:

```python
def run_through_gate[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], T],
    now: Callable[[], float] = time.monotonic,
) -> T:
```
with:
```python
def run_through_gate[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], T],
    now: Callable[[], float] = time.monotonic,
    rpm: RpmLimiter | None = None,
) -> T:
```

and replace:
```python
        if not gate.wait_until_open(deadline):
            raise RateLimited()  # an earlier 429 armed a window past our budget
        try:
```
with:
```python
        if not gate.wait_until_open(deadline):
            raise RateLimited()  # an earlier 429 armed a window past our budget
        if rpm is not None and not rpm.acquire(deadline):
            raise RateLimited()  # RPM cap can't grant a permit within the budget
        try:
```

- [ ] **Step 5: Add the `rpm` barrier to `run_through_gate_async`**

In the same file, replace:
```python
async def run_through_gate_async[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], Awaitable[T]],
    now: Callable[[], float] = time.monotonic,
) -> T:
```
with:
```python
async def run_through_gate_async[T](
    gate: RateLimitGate,
    budget: float,
    fn: Callable[[], Awaitable[T]],
    now: Callable[[], float] = time.monotonic,
    rpm: RpmLimiter | None = None,
) -> T:
```

and replace:
```python
        if not await gate.wait_until_open_async(deadline):
            raise RateLimited()
        try:
```
with:
```python
        if not await gate.wait_until_open_async(deadline):
            raise RateLimited()
        if rpm is not None and not await rpm.acquire_async(deadline):
            raise RateLimited()  # RPM cap can't grant a permit within the budget
        try:
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run cli test -- radis/core/tests/test_rate_limit.py -v`
Expected: PASS (all tests — the new RpmLimiter/integration tests plus all pre-existing gate tests, which still pass because `rpm` defaults to `None`).

- [ ] **Step 7: Commit**

```bash
git add radis/core/utils/rate_limit.py radis/core/tests/test_rate_limit.py
git commit -m "feat(core): add RpmLimiter and wire it into run_through_gate"
```

---

### Task 3: Wire the limiter into both clients

**Files:**
- Modify: `radis/core/utils/llm_client.py`
- Modify: `radis/core/tests/test_llm_client.py`

**Interfaces:**
- Consumes: `RpmLimiter` (Task 2); `_LLM_RPM_LIMITER` module global; `RateLimited`; `LLM_MAX_RPM` setting (Task 1).
- Produces: module global `_LLM_RPM_LIMITER: RpmLimiter`; both clients route through it.

- [ ] **Step 1: Write the failing tests**

In `radis/core/tests/test_llm_client.py`:

First, extend the imports. The file currently imports `_LLM_GATE, AsyncChatClient, LLMClient` from `radis.core.utils.llm_client` and `RateLimited` from `radis.core.utils.rate_limit`. Add `_LLM_RPM_LIMITER` to the llm_client import, add `RpmLimiter` to the rate_limit import, and add `from radis.core.utils import llm_client` (module import, for monkeypatching the global). The import section becomes:

```python
from radis.core.utils import llm_client
from radis.core.utils.llm_client import _LLM_GATE, _LLM_RPM_LIMITER, AsyncChatClient, LLMClient
from radis.core.utils.rate_limit import RateLimited, RpmLimiter
```

Then extend the existing autouse `reset_gate` fixture to also reset the RPM limiter (it currently resets only `_LLM_GATE`):

```python
@pytest.fixture(autouse=True)
def reset_gate():
    _LLM_GATE.reset()
    _LLM_RPM_LIMITER.reset()
    yield
    _LLM_GATE.reset()
    _LLM_RPM_LIMITER.reset()
```

Then append these tests:

```python
def test_default_rpm_limiter_is_disabled():
    # Default LLM_MAX_RPM is 0, so the process-global limiter is a no-op.
    assert _LLM_RPM_LIMITER.acquire(deadline=0.0) is True


def test_extract_data_defers_when_rpm_exhausted(monkeypatch):
    # A drained 1-rpm limiter: the next permit is 60s away, beyond a 30s budget,
    # so the limiter (reached via the client) defers without ever calling the LLM.
    limiter = RpmLimiter(1)
    assert limiter.acquire(deadline=0.0) is True  # consumes the starting permit (no wait)
    monkeypatch.setattr(llm_client, "_LLM_RPM_LIMITER", limiter)

    mock = create_openai_client_mock(_Schema(value="never"))
    with patch("openai.OpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            LLMClient().extract_data("p", _Schema, max_wait=30.0)
    cast(MagicMock, mock).beta.chat.completions.parse.assert_not_called()


@pytest.mark.asyncio
async def test_chat_defers_when_rpm_exhausted(monkeypatch):
    limiter = RpmLimiter(1)
    assert limiter.acquire(deadline=0.0) is True  # consumes the starting permit (no wait)
    monkeypatch.setattr(llm_client, "_LLM_RPM_LIMITER", limiter)

    mock = create_async_openai_client_mock("never")
    with patch("openai.AsyncOpenAI", return_value=mock):
        with pytest.raises(RateLimited):
            await AsyncChatClient().chat([{"role": "user", "content": "hi"}], max_wait=30.0)
    cast(MagicMock, mock).chat.completions.create.assert_not_called()
```

Note: these defer tests use the real `time.monotonic` clock but never sleep — `acquire` returns `False` *before* sleeping when the next permit (60s out) exceeds the 30s budget. The drain call's `acquire(now+1.0)` consumes the single starting permit immediately (no wait). `cast` and `MagicMock` are already imported in this file from Task 4 of the previous plan; if not present, add `from unittest.mock import MagicMock, patch` and `from typing import cast`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run cli test -- radis/core/tests/test_llm_client.py -v`
Expected: FAIL — `ImportError: cannot import name '_LLM_RPM_LIMITER'` (collection error).

- [ ] **Step 3: Add the module-global limiter**

In `radis/core/utils/llm_client.py`, add `RpmLimiter` to the import from `radis.core.utils.rate_limit`:

```python
from radis.core.utils.rate_limit import (
    RateLimitGate,
    RpmLimiter,
    run_through_gate,
    run_through_gate_async,
    with_transient_retries,
    with_transient_retries_async,
)
```

Then, immediately after the `_LLM_GATE = RateLimitGate(...)` block, add:

```python
# Process-global proactive cap on LLM requests/minute. Disabled when LLM_MAX_RPM <= 0.
_LLM_RPM_LIMITER = RpmLimiter(settings.LLM_MAX_RPM)
```

- [ ] **Step 4: Pass the limiter from both clients**

In `AsyncChatClient.chat`, change the `run_through_gate_async(...)` call to pass `rpm`:

```python
        return await run_through_gate_async(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries_async(
                lambda: self._chat(messages, max_completion_tokens),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
            rpm=_LLM_RPM_LIMITER,
        )
```

In `LLMClient.extract_data`, change the `run_through_gate(...)` call to pass `rpm`:

```python
        return run_through_gate(
            _LLM_GATE,
            max_wait,
            lambda: with_transient_retries(
                lambda: self._extract_data(prompt, schema),
                settings.LLM_TRANSIENT_RETRY_ATTEMPTS,
                settings.LLM_TRANSIENT_RETRY_BASE_SECONDS,
            ),
            rpm=_LLM_RPM_LIMITER,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run cli test -- radis/core/tests/test_llm_client.py -v`
Expected: PASS (the new RPM defer tests plus the pre-existing client tests, which still pass because the default process-global limiter is disabled).

- [ ] **Step 6: Commit**

```bash
git add radis/core/utils/llm_client.py radis/core/tests/test_llm_client.py
git commit -m "feat(core): cap LLM requests per minute via shared RpmLimiter"
```

---

### Task 4: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Lint, format, and type check**

Run: `uv run cli lint`
Expected: ruff + pyright clean. (djlint reports ~1615 pre-existing errors only in generated `site/` docs — unrelated to this change; no source `.py`/template here is a template anyway.) If ruff reports formatting, run `uv run cli format-code`, then `git commit -am "style: format"`.

- [ ] **Step 2: Run the full test suite**

Run: `uv run cli test -- -q`
Expected: PASS, no failures (the prior baseline was 265 passed; this adds the new RpmLimiter/wiring tests).

- [ ] **Step 3: Confirm the diff matches the spec**

Run: `git diff --stat 279c39fa HEAD`
Expected files changed: `radis/settings/base.py`, `example.env`, `radis/core/utils/rate_limit.py`, `radis/core/utils/llm_client.py`, `radis/core/tests/test_rate_limit.py`, `radis/core/tests/test_llm_settings.py`, `radis/core/tests/test_llm_client.py` (plus this plan/spec doc).

---

## Acceptance Criteria (from spec)

- `RpmLimiter` exists in `radis/core/utils/rate_limit.py` with sync + async acquire, disabled when `max_rpm <= 0`. ✔ Task 2
- Both clients route every LLM request through the shared `_LLM_RPM_LIMITER`; over-budget waits defer with `RateLimited`. ✔ Task 3
- `LLM_MAX_RPM` setting (default 0) added and documented; with the default, behavior and all existing tests are unchanged. ✔ Tasks 1, 4
- `uv run cli lint` and `uv run cli test` pass. ✔ Task 4
