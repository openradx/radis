# LLM Rate-Limit & Error Handling for Labeling — Design

- **Date:** 2026-06-24 (revised 2026-06-25)
- **Branch:** `feature/auto-labeling_muhammad`
- **Status:** Approved design, pending implementation plan
- **Scope:** `radis.labels` labeling path + reusable helpers in `radis.chats.utils`

## Context and problem

Auto-labeling classifies reports by calling an external, OpenAI-compatible LLM
provider. On the RADIS staging server (a university-hosted gateway, Qwen-family
model), a labeling backfill triggered HTTP 429 rate-limit errors.

Today the labeling path relies entirely on the OpenAI SDK's default behavior:

- `max_retries=2` (3 attempts), exponential backoff with jitter, ~1.1–1.5s total.
- The SDK **already** honors `Retry-After` when present and `0 < value <= 60`.
- No per-request timeout override → the SDK default of 600s applies.

Structural weaknesses:

1. **Backoff too short and uncoordinated.** The SDK retry is per-call and
   per-thread. `LabelingTaskProcessor` runs a `ThreadPoolExecutor`
   (`LABELING_LLM_CONCURRENCY_LIMIT`); each thread backs off in isolation. When
   one thread is rate-limited, the others keep firing — the worker keeps
   hammering a gateway that is already blocking it.
2. **Long rate-limits are unhandled.** The SDK ignores `Retry-After > 60s`, and
   its ~1.5s total backoff cannot ride out a multi-second/minute limit.
3. **Non-429 errors are equally important and more common.** Connection failures,
   timeouts, and 5xx happen occasionally in normal operation. They must not
   silently lose reports, and the fix for 429 must not make them *worse*.

RADIS is a general product; deployments use different providers with different
rate-limit semantics (some send `Retry-After`, some do not; some cap at >60s).
The solution must be **provider-agnostic** and must **throttle and respect
`Retry-After` rather than hammer until blocked** — while keeping occasional
transient errors resilient.

## Goals

- When the gateway rate-limits, the whole labeling worker backs off **together**.
- Honor `Retry-After` when sent — **in full, up to the per-report budget**,
  including values **> 60s** (which the SDK ignores). No premature probing of a
  window the provider told us is closed.
- When no `Retry-After` is sent, fall back to an adaptive exponential backoff
  that needs no per-provider tuning.
- Keep **occasional non-429 transient errors** (connection/timeout/5xx)
  resilient with a small local retry — restoring the resilience that disabling
  the SDK retry would otherwise remove.
- Recover automatically when the provider is healthy again.
- Bound the worst case so a long outage cannot hang the worker indefinitely.
- No impact on interactive chat UX.

## Non-goals

- Proactive client-side rate limiting (token bucket / configured RPM). Heavier
  and needs per-deployment tuning; deferred.
- Cross-worker/global coordination via a shared store. The default deployment
  runs a single `llm` queue worker; per-process coordination covers it. A
  horizontally-scaled deployment self-throttles per replica (acceptable).
- **Across-run task retry / scan-checkpoint changes.** A report left unlabeled
  (exhausted transient retries, or a rate-limit beyond the budget) is recovered
  by the **next manual backfill** via stale/missing detection
  (`_needs_work_queryset`). The periodic scan does **not** recover such reports
  (window-based scope + checkpoint advanced at job creation). This is an
  **accepted, documented limitation** — kept deliberately simple, no new task
  lifecycle machinery (see "Error handling and recovery").
- Changing chat behavior. Interactive chat keeps failing fast.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Ambition | Coordinated backoff (per-call resilience + shared gate) | True "throttle, don't hammer", not just bigger retries. |
| Coordination width | Per worker process (in-memory) | No new infra; covers the default single-`llm`-worker deployment. |
| Pause behavior | Block-and-wait, **budget-aware** | Short limits are waited out (report succeeds); long limits defer without hanging a worker thread. |
| `Retry-After` ≤ budget | Wait the **full** value once, uncapped | Lets the report succeed now instead of deferring to a manual backfill. No premature probing (which wastes requests and can reset a sliding-window limiter). |
| `Retry-After` > budget | Defer immediately (decision uses the **raw** header) | Can't be waited out; deferring report-by-report would churn. Gate stays armed so the rest of the batch defers instantly. |
| Fallback when no `Retry-After` | Exponential with cap, reset on success | Adapts to severity with zero per-provider tuning. |
| Cap vs ceiling | **Two separate limits** | The exponential **fallback cap** bounds the *guess*; the **header ceiling** is a safety rail so a pathological `Retry-After` can't freeze labeling for days. The fallback cap never clamps a real header. |
| Non-429 transient errors | Small **local** retry (not gate-coordinated), then propagate | Connection/timeout/5xx are usually per-request, not "everyone back off". Restores resilience removed by `max_retries=0`. |
| Non-retryable errors (4xx) | Propagate immediately | 400/401/403/404/422 won't get better on retry. |
| Recovery of leftover reports | Next **manual backfill** (stale/missing) | Simple; reuses existing scope logic. Scan does not recover them (documented). |
| SDK retries on labeling client | Disabled (`max_retries=0`) | Surfaces 429 immediately so the gate arms before other threads keep firing. Non-429 resilience is re-added by our own local retry. |
| Applies to | Labeling path only | Block-and-wait is wrong for interactive chat; built reusable so chat could opt in later with a fail-fast policy. |

## Architecture

### Naming note

`RateLimitGate` is a request barrier (open/closed) and is **unrelated to the
`LabelGroup` "gate"** (the Yes/No applicability screen in labeling). To avoid
overloading "gate" inside the labels app, the client adapter is named
`ThrottledChatClient`.

### Components and file layout

- `radis/chats/utils/rate_limit.py` (reusable mechanism, no labels coupling):
  - `RateLimitGate` — the per-process coordinator.
  - `run_through_gate(gate, budget, fn)` — per-call wrapper for 429/backoff.
  - `with_transient_retries(fn, attempts, base)` — small local retry for
    non-429 transient errors.
  - `RateLimited` — small sentinel exception raised when a report cannot be
    labeled within its budget (the no-probe defer path). Caught by `_safe_label`
    like any other failure → task `WARNING`.
  - `_parse_retry_after(exc)` — reads `Retry-After` from a `RateLimitError`.
- `radis/labels/` (labeling policy):
  - `ThrottledChatClient` — same `extract_data` surface as `ChatClient`,
    routed through the transient retry and the gate.
  - `_LABELING_GATE` — process-global `RateLimitGate` singleton.

### `RateLimitGate`

State, protected by a `threading.Lock`:

- `_blocked_until: float` — a `monotonic()` deadline; the gate is **open** when
  `now() >= _blocked_until`. Stored as a deadline (not a boolean) so any thread
  can check expiry by comparison, with no background timer.
- `_consecutive_429: int` — consecutive **header-less** 429s; drives the
  exponential fallback.

Two distinct limits (the key refinement):

- `fallback_max` — caps the **exponential guess only** (no `Retry-After`).
- `header_ceiling` — safety rail on how long a **single `Retry-After`** may arm
  the gate. Large enough never to clamp a realistic value (default 1h); guards
  against a pathological header (e.g. `86400`) freezing labeling.

Testability: the gate takes injectable `now`/`sleep` callables (default
`time.monotonic`/`time.sleep`) so unit tests drive a fake clock with no real
waiting.

```python
def note_success(self) -> None:
    with self._lock:
        self._consecutive_429 = 0                 # provider healthy -> reset ladder

def note_rate_limited(self, retry_after: float | None) -> float:
    # Arm the shared window. Returns the pause actually used for arming.
    with self._lock:
        if retry_after is not None:
            pause = min(retry_after, self._header_ceiling)   # real value; ceiling only as a rail
        else:
            self._consecutive_429 += 1
            pause = min(self._base * 2 ** (self._consecutive_429 - 1), self._fallback_max)
        self._blocked_until = max(self._blocked_until, self._now() + pause)  # extend, never shrink
        return pause

def wait_until_open(self, deadline: float) -> bool:
    # Block until the window opens. Return False (defer, NO probe) if the window
    # clears after `deadline` — never sleep past the caller's give-up budget.
    while True:
        with self._lock:
            open_at = self._blocked_until
        if open_at <= self._now():
            return True
        if open_at > deadline:
            return False
        self._sleep(open_at - self._now())
```

Properties: coordinated (one 429 arms the window for all threads),
provider-agnostic (header or exponential fallback), self-healing (reset on
success), bounded (budget-aware wait + ceiling cap).

### `run_through_gate` (per-call wrapper, with give-up budget)

```python
def run_through_gate(gate, budget, fn, now=time.monotonic):
    deadline = now() + budget
    while True:
        if not gate.wait_until_open(deadline):         # inherited window > budget -> defer, no probe
            raise RateLimited()
        try:
            result = fn()
            gate.note_success()
            return result
        except openai.RateLimitError as exc:
            retry_after = _parse_retry_after(exc)
            pause = gate.note_rate_limited(retry_after)        # ARM FIRST: throttles other reports
            effective = retry_after if retry_after is not None else pause
            logger.warning("Labeling rate-limited; backing off %.1fs", effective)
            if now() + effective > deadline:                   # can't wait it out within budget
                raise                                          # defer THIS report; gate stays armed
            # else loop: wait_until_open() waits out the (<=budget) window, then retries
```

`deadline` is the absolute give-up moment for this one call: `now()` plus the
budget (`LABELING_RATE_LIMIT_MAX_WAIT_SECONDS`). Computing it once turns every
later "should I keep waiting?" check into a simple comparison.

Three points that matter:

1. **Arm before deferring.** `note_rate_limited` runs *before* the give-up
   `raise`, so deferring one report leaves the throttle armed for the others —
   they then defer instantly at `wait_until_open` (no hammering).
2. **Give-up uses the raw `Retry-After`.** A `Retry-After: 600` with budget 300
   defers immediately (`600 > 300`) with no wasted boundary probe, even though
   the gate arms only for the ceiling-bounded duration.
3. **`Retry-After` ≤ budget waits the full value once** — `wait_until_open`
   sleeps until the (uncapped) window opens, then retries → success.

Non-429 exceptions are not caught here.

### `with_transient_retries` (non-429 resilience)

```python
TRANSIENT_ERRORS = (openai.APIConnectionError, openai.InternalServerError)  # incl. timeouts, 5xx

def with_transient_retries(fn, attempts, base, sleep=time.sleep):
    """Small per-call retry for transient non-429 errors. NOT gate-coordinated:
    these are usually per-request, not a 'whole provider is overloaded' signal."""
    for attempt in range(attempts + 1):
        try:
            return fn()
        except TRANSIENT_ERRORS:
            if attempt == attempts:
                raise                          # exhausted -> _safe_label False -> manual-backfill recovery
            sleep(base * 2 ** attempt)         # 1s, 2s, ...
```

- Does **not** arm the gate or touch `_consecutive_429`.
- **A 429 mid-retry stops the transient loop immediately — it never hammers.**
  `RateLimitError` is not in `TRANSIENT_ERRORS`, so if a 429 surfaces on *any*
  attempt (including after a connection error on an earlier attempt), it is not
  caught here: the loop exits at once and the error propagates to
  `run_through_gate`, which arms the gate and coordinates the worker-wide
  backoff. The transient retry can therefore never keep firing in the face of a
  rate-limit; the moment the provider says 429, control passes to the gate.
- Non-retryable errors (400/401/403/404/422 …) are not in `TRANSIENT_ERRORS`, so
  they propagate immediately.

### Integration into the labeling path

`label_report()` makes several LLM calls per report (gate-batch calls and one
per passing group via `_run_label_set`), all through a single `client`. Wrap the
client once so every call goes through both layers:

```python
_LABELING_GATE = RateLimitGate(
    base_seconds=settings.LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS,
    fallback_max_seconds=settings.LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS,
    header_ceiling_seconds=settings.LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS,
)

class ThrottledChatClient:
    def __init__(self, client: ChatClient):
        self._client = client

    def extract_data(self, prompt, schema):
        call = lambda: self._client.extract_data(prompt, schema)
        return run_through_gate(
            _LABELING_GATE,
            settings.LABELING_RATE_LIMIT_MAX_WAIT_SECONDS,
            lambda: with_transient_retries(
                call,
                settings.LABELING_TRANSIENT_RETRY_ATTEMPTS,
                settings.LABELING_TRANSIENT_RETRY_BASE_SECONDS,
            ),
        )
```

Only `labeling.py` line ~40 changes:

```python
client = ThrottledChatClient(
    ChatClient(max_retries=0, timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS)
)
```

`ChatClient`/`AsyncChatClient` gain optional `max_retries` and `timeout`
constructor params, defaulting to current SDK behavior so chat is unaffected.
`max_retries=0` is passed explicitly (and only) for the labeling client.

### Why `max_retries=0` matters (and why we re-add non-429 retries)

If the SDK keeps retrying a 429, the first thread waits **inside** the SDK call
(per-thread, uncoordinated) and only raises after exhausting its retries. During
that silent wait the gate is not yet armed, so the other threads keep hammering.
`max_retries=0` surfaces the 429 to `run_through_gate` immediately, so the gate
arms at once.

But the SDK's `max_retries` is a single coarse switch — it governs **all**
retryable errors (429, connection, timeout, 5xx). Setting it to 0 (correct for
the 429 goal) **also** disables the SDK's retries for non-429 transient errors.
`with_transient_retries` re-adds that resilience under our own control, so 429s
go to the coordinated gate while connection/timeout/5xx get a small local retry.

## Configuration

Defined in `radis/settings/base.py`. **Every value below is read from an
environment variable of the same name** (defaults shown), so deployments tune
them via `.env` without code changes — matching the existing auto-labeling vars
(`LABELING_TASK_BATCH_SIZE`, `LABELING_GATE_BATCH_SIZE`, …).

| Env var / setting | Default | Purpose |
|---|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | `60.0` | Per-request timeout for the labeling `ChatClient` (time to wait for **one** response). |
| `LABELING_RATE_LIMIT_BACKOFF_BASE_SECONDS` | `5.0` | First exponential pause when no `Retry-After`. |
| `LABELING_RATE_LIMIT_FALLBACK_MAX_SECONDS` | `120.0` | Caps the **exponential guess only**. Never clamps a real `Retry-After`. (Renamed from `…_BACKOFF_MAX_SECONDS`.) |
| `LABELING_RATE_LIMIT_HEADER_CEILING_SECONDS` | `3600.0` | Safety rail on how long a single `Retry-After` may arm the gate. **Blast-radius knob** — see note. Must be ≥ the give-up budget. |
| `LABELING_RATE_LIMIT_MAX_WAIT_SECONDS` | `300.0` | Per-report give-up budget before deferring to a manual backfill. |
| `LABELING_TRANSIENT_RETRY_ATTEMPTS` | `2` | Local retries for non-429 transient errors (connection/timeout/5xx). |
| `LABELING_TRANSIENT_RETRY_BASE_SECONDS` | `1.0` | Base for the transient retry's short backoff (1s, 2s, …). |
| `LABELING_LLM_CONCURRENCY_LIMIT` | `6` → `2` | Lower baseline pressure. Complementary to the gate, not required by it. |

Parsed via `env.float(...)` / `env.int(...)`, matching existing style. All new
vars are added to `example.env`; the `CLAUDE.md` auto-labeling env section is
updated to list them.

**Header-ceiling blast radius.** The gate is process-global to labeling, so while
it is armed *all* labeling work defers (the in-flight job, new backfills, and the
scan) — correct, since the provider is rate-limited regardless of which job
sends requests (interactive chat is unaffected — it does not use the gate). The
ceiling bounds how long one header can hold that freeze. A large ceiling honors a
long `Retry-After` quietly but is slow to notice early recovery; a smaller one
re-probes sooner at the cost of an occasional probe against a still-busy
provider. `3600` honors up to an hour and caps pathological values; deployments
may lower it.

## Error handling and recovery

- **429 with `Retry-After` ≤ budget:** wait the **full** value once (uncapped by
  the fallback max; ceiling only guards absurd values), then retry → succeeds.
- **429 with `Retry-After` > budget:** defer immediately (give-up uses the raw
  header). The gate stays armed (ceiling-bounded), so the rest of the batch
  defers instantly with no churn.
- **429 without `Retry-After`:** exponential ladder `base, 2×, 4×, …` capped at
  the fallback max; reset on first success; defer when the next pause would
  exceed the budget.
- **Non-429 transient (connection/timeout/5xx):** `with_transient_retries` gives
  a small local retry; the gate is **not** armed (other threads unaffected). On
  exhaustion the error propagates to the existing failure path.
- **Non-retryable (4xx):** propagates immediately.

**Leftover reports** — any report left unlabeled (exhausted transient retries, or
a rate-limit beyond the budget) → `_safe_label` returns `False` → task ends
`WARNING` → report stays unlabeled. It is re-found and labeled by the **next
manual backfill**, whose scope (`_needs_work_queryset`) selects reports with a
missing/stale gate or missing/stale label result.

> **Accepted limitation:** the periodic scan does **not** recover these reports.
> Its scope is window-based (`created_at >= scan_from`) and it advances its
> checkpoint at job creation, so a report a scan job failed to label falls behind
> the checkpoint permanently. Recovery is therefore via a manual backfill only.
> This keeps the design simple (no across-run retry, no checkpoint surgery); the
> frequency of leftover reports is low (transient exhaustion or a long outage).

Partial progress is preserved: results are written per report/group, so a 429 or
transient exhaustion mid-batch leaves already-labeled reports labeled and only
the unprocessed ones for the next backfill.

## Testing plan (test-first)

Unit — `RateLimitGate` (`radis/chats/tests/`), fake clock:

- Honors `Retry-After` ≤ budget in full (e.g. 200 → ~200s window, no clamp to
  the fallback max).
- `Retry-After` > ceiling arms only to the ceiling (4000, ceiling 3600 → 3600).
- Exponential fallback when absent: 5, 10, 20, 40… capped at the fallback max.
- `note_success()` resets the ladder to base.
- Window extends, never shrinks (the `max()` rule).
- `wait_until_open(deadline)`: returns immediately when open; sleeps then returns
  `True` when the window clears before the deadline; returns `False` (no sleep,
  no probe) when the window clears after the deadline.
- Coordination: thread A arms; thread B's `wait_until_open` blocks/defers to the
  same deadline.

Unit — `run_through_gate`:

- Success → `note_success`, no wait.
- 429-then-success (`Retry-After` ≤ budget) → waits once, returns result.
- `Retry-After` > budget → arms the gate, then re-raises immediately (raw-header
  give-up; fake clock confirms no extra probe).
- Persistent header-less 429 → re-raises when the next pause would exceed budget.
- Non-429 error → propagates (gate untouched).
- `_parse_retry_after`: `retry-after-ms`, `retry-after` seconds, HTTP-date,
  missing → `None`.

Unit — `with_transient_retries`:

- Connection error once then success → retried, returns result.
- Persistent connection error → propagates after `attempts` retries.
- `RateLimitError` is **not** caught here (passes through untouched).
- Non-retryable (e.g. `BadRequestError`) propagates immediately.

Integration — labeling path (`radis/labels/tests/`, Django):

- Mock `ChatClient.extract_data` to raise `RateLimitError` once then succeed →
  `label_report` still writes results.
- Persistent `RateLimitError` → report left unlabeled, task `WARNING`.
- Transient `APIConnectionError` once then succeed → results still written.
- Test helper builds a real `openai.RateLimitError` carrying a chosen
  `retry-after` header (extends `chats/utils/testing_helpers.py`).
- Fixture resets `_LABELING_GATE` between tests so process-global state does not
  leak.

## Rollout / operational notes

- Default `LABELING_LLM_CONCURRENCY_LIMIT` drops 6 → 2; deployments can override.
- On staging, capture the real `Retry-After` value (if any) by temporarily
  logging `RateLimitError` response headers, or ask the gateway operators, to
  tune `BASE`/`FALLBACK_MAX`/`HEADER_CEILING` if needed. The design works whether
  or not the header is sent.
- Operators who see persistently unlabeled reports after an outage should run a
  manual backfill — that is the intended recovery path.
