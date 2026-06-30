# Shared LLM client with built-in rate limiting

**Date:** 2026-06-30
**Branch:** `feature/llm-client-rate-limiting` (off `main`)
**Status:** Approved design — ready for implementation planning

## Problem

LLM access is split across apps with inconsistent resilience:

- `radis/chats/utils/chat_client.py` (on `main`) holds two clients:
  - `AsyncChatClient` — async, `chat(messages, max_completion_tokens) -> str`. Used by `chats/views.py`.
  - `ChatClient` — sync, `extract_data(prompt, schema) -> BaseModel`. Used by `extractions/processors.py` and `subscriptions/processors.py`.
- Neither client has provider-wide rate-limit handling. On a 429 they rely only on the OpenAI SDK's default retries, and each app that calls the LLM can independently hammer a provider that is already blocking.

A rate-limit gate was developed on the `feature/auto-labeling_muhammad` branch (`radis/chats/utils/rate_limit.py` + `radis/labels/throttled_client.py`), but it is labeling-only and lives only on that branch. We want that resilience available to **every** app that talks to the LLM, applied to **both** the sync and async clients, with the client code living in `core`.

## Goals

- Move the LLM client code into `radis/core` so all apps share one home for it.
- Rename the sync `ChatClient` to `LLMClient`; **keep** `AsyncChatClient`'s name.
- Apply rate limiting to **both** clients, always-on (built in, no opt-in wrapper).
- All apps currently using the client (chats, extractions, subscriptions) use the core clients.

## Non-goals / out of scope

- **Labels app** — not present on `main`; untouched here. It adopts the core `LLMClient` later when this branch merges with the labeling work.
- **Cross-process** rate-limit coordination. The gate is per-process, matching the original design. Interactive chat runs in the web/Daphne process; extractions and subscriptions run in worker processes; each process has its own gate. A truly global gate (shared state in Postgres/Redis) is a separate, larger effort.
- The current `feature/auto-labeling_muhammad` branch is left completely untouched.

## Decisions (from brainstorming)

1. **One global shared gate** per process for all LLM traffic — a 429 from any caller backs off every caller in that process. Config is generic (`LLM_RATE_LIMIT_*`).
2. **Unified gate, two wait methods** — a single `RateLimitGate` holds the shared backoff state; it exposes a sync `wait_until_open()` and an async `wait_until_open_async()`. Sync and async callers share one in-process backoff window.
3. **Built-in, always-on throttling** — the gate + transient-retry are folded directly into the clients. No separate `ThrottledChatClient` wrapper.
4. **Two wait budgets** — `chat` (interactive) defaults to a short budget; `extract_data` (batch) defaults to a long budget. Either is overridable per call. The budget is a per-call value and does not affect the shared gate state.

## Architecture

### New files in `core`

**`radis/core/utils/rate_limit.py`** — pure mechanism, ported from the labeling branch and extended:

- `RateLimited(Exception)` — a call could not complete within its wait budget.
- `RateLimitGate` — per-process barrier. State (`_blocked_until`, `_consecutive_429`) guarded by a `threading.Lock` held only for brief reads/writes, **never across a sleep or await**.
  - Existing sync: `reset()`, `note_success()`, `note_rate_limited(retry_after) -> float`, `wait_until_open(deadline) -> bool`.
  - **New:** `async def wait_until_open_async(deadline) -> bool` using `asyncio.sleep`. Reads the same `_blocked_until`; identical "open / deferred-past-deadline" semantics.
- `_parse_retry_after(exc) -> float | None` — unchanged (`retry-after-ms`, seconds, HTTP-date).
- `run_through_gate(gate, budget, fn, now=time.monotonic) -> T` — unchanged.
- `with_transient_retries(fn, attempts, base, sleep=time.sleep) -> T` — unchanged.
- **New** `async def run_through_gate_async(gate, budget, fn, now=time.monotonic) -> T` — async twin of `run_through_gate`; `fn` is an async callable; waits via `wait_until_open_async`.
- **New** `async def with_transient_retries_async(fn, attempts, base, sleep=asyncio.sleep) -> T` — async twin; `fn` is an async callable.

`TRANSIENT_ERRORS = (openai.APIConnectionError, openai.InternalServerError)` is shared by sync and async retry helpers.

**`radis/core/utils/llm_client.py`** — the clients plus the module-global gate:

- `_get_base_url()` — unchanged from `main`.
- Module-global `_LLM_GATE = RateLimitGate(base, fallback_max, header_ceiling)` constructed once at import from settings. Shared by both clients in the process.
- `AsyncChatClient` (**name kept**):
  - `__init__` sets `max_retries=0` and `timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS` on the `AsyncOpenAI` client so the gate owns backoff.
  - `chat(messages, max_completion_tokens=None, max_wait=None) -> str`. `max_wait` defaults to `settings.LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS`. Body wrapped in `run_through_gate_async(..., with_transient_retries_async(...))`.
- `LLMClient` (renamed from `ChatClient`):
  - `__init__` sets `max_retries=0` and `timeout=settings.LLM_REQUEST_TIMEOUT_SECONDS` on the `OpenAI` client.
  - `__init__` reads `self._extra_body = getattr(settings, "LLM_EXTRA_BODY", {}) or {}`.
  - `extract_data(prompt, schema, max_wait=None) -> BaseModel`. `max_wait` defaults to `settings.LLM_RATE_LIMIT_MAX_WAIT_SECONDS`. Sends the prompt as a **user** message (not `system`) and passes `extra_body=self._extra_body`. Body wrapped in `run_through_gate(..., with_transient_retries(...))`.

> **Note — two behavior changes carried over from the `feature/auto-labeling_muhammad` branch (not on `main`):**
> 1. `extract_data` sends the prompt as a **user** message. `main` sends it as `system`, which several OpenAI-compatible servers (Qwen/vLLM, llama.cpp) reject with a "no user message" error.
> 2. `extract_data` passes `extra_body` from the new `LLM_EXTRA_BODY` setting (default disables provider "thinking"). Scoped to `extract_data` only — the async `chat()` path does **not** apply `extra_body`. This slightly changes runtime behavior for extractions/subscriptions (they begin sending `enable_thinking: False`), which is the desired behavior for structured output and keeps `core/utils/llm_client.py` identical to what the labeling branch needs for a clean merge.

### Removed

- `radis/chats/utils/chat_client.py` is deleted (moved to core). The current `feature/auto-labeling_muhammad` branch keeps its copy.

### Settings (`radis/settings/base.py`) — new, generic

| Setting | Default | Purpose |
|---|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | `60.0` | Per-request client timeout (both clients). |
| `LLM_EXTRA_BODY` | `{"chat_template_kwargs": {"enable_thinking": false}}` | Provider quirks sent with each `extract_data` call (JSON). |
| `LLM_RATE_LIMIT_BACKOFF_BASE_SECONDS` | `5.0` | First exponential pause when no `Retry-After`. |
| `LLM_RATE_LIMIT_FALLBACK_MAX_SECONDS` | `120.0` | Caps the header-less exponential guess. |
| `LLM_RATE_LIMIT_HEADER_CEILING_SECONDS` | `3600.0` | Safety rail on a single `Retry-After`. |
| `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` | `300.0` | Batch wait budget (`extract_data` default). |
| `LLM_RATE_LIMIT_INTERACTIVE_MAX_WAIT_SECONDS` | `20.0` | Interactive wait budget (`chat` default). |
| `LLM_TRANSIENT_RETRY_ATTEMPTS` | `2` | Local retries for non-429 transient errors. |
| `LLM_TRANSIENT_RETRY_BASE_SECONDS` | `1.0` | Base backoff for transient retries. |

`example.env` documents the new variables.

### Caller migration

- `radis/extractions/processors.py`: import `LLMClient` from `radis.core.utils.llm_client`; `ChatClient()` -> `LLMClient()`. Call site unchanged.
- `radis/subscriptions/processors.py`: same as extractions.
- `radis/chats/views.py`: import `AsyncChatClient` from `radis.core.utils.llm_client`. Wrap the `chat()` calls so a `RateLimited` (interactive budget exhausted on a sustained 429) surfaces as a friendly "LLM service is busy, please try again" message to the user instead of an unhandled 500.

## Data flow

1. App constructs a client (`LLMClient()` or `AsyncChatClient()`). Both share `_LLM_GATE`.
2. A call enters `run_through_gate[_async]` with its budget; the gate blocks until open or the budget deadline passes (-> `RateLimited`).
3. Inside the gate, `with_transient_retries[_async]` runs the actual OpenAI call, retrying connection/5xx errors a few times.
4. On a 429, `note_rate_limited` arms the shared window (extend-never-shrink); all callers in the process back off behind it. On success, `note_success` resets the exponential ladder.

## Error handling

- **429 within budget** — waited out, then retried; call succeeds.
- **429 beyond budget** — `RateLimited` raised. Batch callers (extractions/subscriptions) let it propagate to the existing task-failure path; the interactive chat view catches it and shows a busy message.
- **Transient non-429 (connection/5xx)** — retried up to `LLM_TRANSIENT_RETRY_ATTEMPTS`, then propagates. Not gate-coordinated.
- **Other errors** — propagate unchanged.

## Testing

New `radis/core/tests/` (mirroring the ported labeling tests, generalized):

- `RateLimitGate`: arming/extending the window, header-ceiling clamp, exponential fallback ladder, `note_success` reset, `wait_until_open` returns `False` past deadline.
- `wait_until_open_async`: same open/deferred semantics with a fake async clock/sleep.
- `run_through_gate` / `run_through_gate_async`: success, wait-then-succeed, defer-when-over-budget, non-429 propagation.
- `with_transient_retries` / `with_transient_retries_async`: retry-then-succeed, exhaustion re-raises, 429 passes straight through (not retried here).
- Clients: `LLMClient.extract_data` and `AsyncChatClient.chat` route through the gate (a stubbed 429 triggers backoff; success path returns the parsed/string result).

Tests use injected fake `now`/`sleep` callables (as the existing suite does) so they run without real waiting.

## Acceptance criteria

- `radis/core/utils/llm_client.py` exports `LLMClient` and `AsyncChatClient`, both throttled, sharing one process gate.
- `radis/chats/utils/chat_client.py` no longer exists; chats, extractions, subscriptions import from core.
- A sustained 429 in one app backs off other LLM callers in the same process.
- Interactive chat gives up after the interactive budget with a friendly message; batch jobs use the long budget.
- `uv run cli lint` and `uv run cli test` pass.
