# Proactive LLM request-per-minute cap (`LLM_MAX_RPM`)

**Date:** 2026-06-30
**Branch:** `feature/llm-client-rate-limiting` (extends the shared LLM client work)
**Status:** Approved design — ready for implementation planning

## Problem

The shared LLM client added a **reactive** rate-limit gate (`RateLimitGate`): it backs off only *after* the provider returns a 429. That protects against overload once the provider is already complaining, but it cannot stop the client from exceeding a provider's documented **requests-per-minute** quota in the first place. We want a **proactive** cap: never emit more than `LLM_MAX_RPM` LLM requests per minute.

Note on terminology: this caps **requests** per minute (RPM), not LLM tokens per minute (TPM). The "bucket" below holds abstract **request permits** — one permit authorizes one LLM request; permits are unrelated to LLM tokens.

## Goals

- Add a per-process proactive cap of `LLM_MAX_RPM` LLM **requests** per minute, applied to every LLM request from both clients (`LLMClient.extract_data` and `AsyncChatClient.chat`).
- Integrate with the existing per-call wait budget: when no permit is available within the call's budget, defer with `RateLimited` — same as the 429 path.
- Opt-in and non-breaking: disabled by default.

## Decisions (from brainstorming)

1. **Scope: per-process** (in-memory), consistent with the existing `RateLimitGate`. All batch LLM tasks (extraction, subscriptions) run on the single `llm_worker` process (both use `@app.task(queue="llm")`), so a per-process cap on that worker effectively governs the provider-wide batch traffic. Interactive chat runs in the separate web/Daphne process (low volume) and gets its own independent bucket. Cross-process coordination is explicitly out of scope (would need a shared backend; no Redis in the stack).
2. **Hand-rolled token-bucket algorithm**, added to the existing `radis/core/utils/rate_limit.py`. No new dependency (pyrate-limiter's distributed backends are unneeded for a per-process cap), and it matches the module's existing concurrency discipline and deterministic `FakeClock` test style.
3. **Burst allowed**: the bucket may hold up to `LLM_MAX_RPM` permits, so a burst of up to `LLM_MAX_RPM` requests is permitted, then steady refill. (Not strict even spacing.)
4. **Default `LLM_MAX_RPM = 0` = disabled** (unlimited). Existing behavior and the current test suite are unchanged unless the operator opts in.

## Architecture

### New component — `RpmLimiter` (in `radis/core/utils/rate_limit.py`)

A per-process token-bucket limiter sitting alongside `RateLimitGate`:

- Construction: `RpmLimiter(max_rpm, now=time.monotonic, sleep=time.sleep, async_sleep=asyncio.sleep)`.
  - `capacity = max_rpm` permits; `refill_rate = max_rpm / 60.0` permits/sec.
  - When `max_rpm <= 0`, the limiter is **disabled**: every acquire is an immediate no-op returning `True`.
- State (`_permits: float`, `_last_refill: float`) guarded by a brief `threading.Lock`, **never held across a `time.sleep` or `await`** (same discipline as the gate).
- `_refill()` (called under the lock): add `(now - last_refill) * refill_rate` permits, clamped to `capacity`; advance `_last_refill`.
- `acquire(deadline) -> bool` (sync): loop — refill; if `>= 1` permit, consume one and return `True`; else compute the wait until the next permit, and if that wait would pass `deadline` return `False` (do not consume); otherwise release the lock, `sleep` the wait, retry.
- `acquire_async(deadline) -> bool` (async): identical logic using `await async_sleep`.
- `reset()` — refill to full and reset `_last_refill` (for tests sharing a process-global limiter).

### Integration — unified with the existing budget

`run_through_gate` and `run_through_gate_async` gain an optional parameter `rpm: RpmLimiter | None = None`. Inside each loop iteration the order becomes:

1. `gate.wait_until_open(deadline)` — defer (`RateLimited`) if the 429 window exceeds the budget.
2. **if `rpm is not None`: `rpm.acquire(deadline)`** — defer (`RateLimited`) if a permit won't be free within the budget.
3. call `fn()`.

Both barriers share the one `deadline = now() + budget`. One permit is consumed per **logical request** (the `fn` passed by the client). Transient connection/5xx retries inside `with_transient_retries` are retries of that same logical request and do **not** spend additional permits.

### Wiring (in `radis/core/utils/llm_client.py`)

- Module-global `_LLM_RPM_LIMITER = RpmLimiter(settings.LLM_MAX_RPM)`, built once at import (same pattern as `_LLM_GATE`).
- `LLMClient.extract_data` passes `rpm=_LLM_RPM_LIMITER` to `run_through_gate` (batch budget default).
- `AsyncChatClient.chat` passes `rpm=_LLM_RPM_LIMITER` to `run_through_gate_async` (interactive budget default).

### Setting

`LLM_MAX_RPM = env.int("LLM_MAX_RPM", default=0)` in `radis/settings/base.py` (0 = unlimited/disabled). Documented in `example.env`.

## Error handling

Reuses `RateLimited` (which now carries a default message). Batch callers (extraction/subscriptions) let it propagate to the existing task-failure path; the interactive chat view catches it and shows the existing "service is busy" message. A disabled limiter (`max_rpm <= 0`) never raises.

## Testing

New `RpmLimiter` unit tests in `radis/core/tests/test_rate_limit.py`, using the existing `FakeClock`:
- Disabled (`max_rpm=0`): `acquire` is a no-op, no sleeps, always `True`.
- Burst: capacity permits can be consumed back-to-back with no wait, the next one waits.
- Refill math: after consuming, waiting `1/refill_rate` seconds frees exactly one permit.
- Wait-then-acquire within budget: sleeps the right amount, returns `True`.
- Defer past deadline: returns `False` without consuming when the next permit is beyond the deadline.
- Async twins (`acquire_async`) for the wait/defer cases.
- Lock discipline: permit math under the lock, sleeps outside (verified by the deterministic clock — no real waiting).

Integration tests: `run_through_gate` / `run_through_gate_async` with an `rpm` limiter — a request proceeds when a permit is free, and defers with `RateLimited` when the limiter can't grant one within the budget. Confirm the existing gate-only tests still pass when `rpm` is omitted/None.

## Acceptance criteria

- `RpmLimiter` exists in `radis/core/utils/rate_limit.py` with sync + async acquire, disabled when `max_rpm <= 0`.
- Both clients route every LLM request through the shared `_LLM_RPM_LIMITER`; over-budget waits defer with `RateLimited`.
- `LLM_MAX_RPM` setting (default 0) added and documented; with the default, behavior and all existing tests are unchanged.
- `uv run cli lint` and `uv run cli test` pass.
