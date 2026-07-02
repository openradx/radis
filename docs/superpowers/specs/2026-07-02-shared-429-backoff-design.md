# Shared cross-process 429 backoff for the embedding gateway

## Context

Commit `c4be7b27` replaced the proactive sliding-window rate limiter
(`EmbeddingRateLimitEvent` ledger + advisory-lock gating, see
[2026-07-01-embedding-rate-limit-gate-design.md](2026-07-01-embedding-rate-limit-gate-design.md))
with a purely reactive `call_with_429_backoff`. That refactor threw out two
orthogonal properties at once: the proactive sliding-window *model* (intended)
and the cross-process *sharing* of backoff state (not intended). The current
backoff lives in a local variable inside a single function call in a single
process. Prod runs three web replicas, a default worker, an llm worker, and an
embeddings worker with `--concurrency 4` — when the gateway starts returning
429s, only the exact caller that received one slows down; everyone else keeps
hammering, and all callers that do back off wake simultaneously.

This design restores cross-process (and within-process cross-task) backoff
while keeping the reactive shape: no ledger, no capacity accounting, no
proactive admission control. The gateway remains the source of truth about
its own limits; we only remember, globally, what it last told us.

## Requirement

When any process receives a 429 from the embedding gateway, the wait it was
told to observe must be observed by **all** background embedding traffic
across every container sharing the database, and repeated 429s must double
the wait globally, not per-caller.

## Decision: search path tries anyway (flagged for review)

The user was not available when this was decided; chosen as the recommended
option, revisit if wrong.

`embed_query` (the search/retrieval path, a single weight-1 request with a
user waiting on it) does **not** consult the shared pause before sending. It
keeps its own local `call_with_429_backoff` behavior. It **does** record any
429 it receives into the shared state, so background traffic learns from it.

Rationale: this preserves the old design's search-priority spirit. A shared
pause almost always originates from bulk background traffic (1000-text
batches); one tiny query sent during the pause frequently succeeds because
the gateway's sliding window frees per-request capacity continuously. Making
a user search sleep up to 60s to protect a background pipeline inverts the
priority the previous design deliberately established. Asymmetry: search
writes to the shared state but does not read it; background reads and writes.

## Architecture

One new singleton model plus a rework of `rate_limiter.py` internals. The
public seam callers use (`call_with_429_backoff`) keeps its name and shape.

### Model: `EmbeddingBackoffState` (migration 0006)

Single row (enforced by `pk=1` convention, get-or-create):

- `paused_until: DateTimeField` — no background request may be sent before
  this instant. In the past / equal to now ⇒ no pause.
- `consecutive_429s: PositiveIntegerField` — global doubling counter.
- `updated_at: DateTimeField(auto_now=True)` — observability only.

Why a table and not cache/advisory locks: no Redis is configured (default
per-process LocMemCache cannot share), Postgres is already the coordination
backbone (Procrastinate, the old limiter), a row survives restarts, and
`select_for_update` gives exact read-modify-write semantics that the cache
API cannot.

### `rate_limiter.py` functions

- `shared_wait_seconds() -> float` — plain read (no lock): seconds until
  `paused_until`, or `0.0`.
- `record_429(retry_after: float) -> float` — in one short
  `select_for_update` transaction: read the row, compute
  `wait = retry_after * 2**consecutive_429s`, set
  `paused_until = max(paused_until, now + wait)`, increment
  `consecutive_429s`, save. Returns `wait` (what this caller should sleep).
  The `max()` means concurrent 429s extend, never shorten, the pause.
- `record_success()` — reset `consecutive_429s` to 0, but **only issues a
  write if the counter is nonzero** (checked with a cheap read first), so
  the steady-state happy path costs one SELECT per batch, no writes.
- `call_with_429_backoff(fn, max_attempts=3, shared_gate=True)` — the
  existing loop, now:
  1. Before each attempt (including the first), if `shared_gate`, loop
     `while (w := shared_wait_seconds()) > 0: _sleep(w)` — re-checking after
     each sleep because another process may extend the pause meanwhile.
  2. Call `fn()`; on success call `record_success()` (only when
     `shared_gate` — a lone weight-1 search succeeding during a pause says
     little about bulk capacity) and return.
  3. On `openai.RateLimitError`: call `record_429(parse_retry_after(exc))`
     regardless of `shared_gate` (search informs, doesn't obey), then
     re-raise if attempts exhausted. Otherwise, the wait before retrying:
     - `shared_gate=True`: no local sleep — loop back to step 1, which
       sleeps out the shared pause `record_429` just extended. One sleep
       mechanism, and the wait reflects the *global* doubling counter.
     - `shared_gate=False`: sleep `parse_retry_after(exc) * 2**(attempt-1)`
       locally — the pre-existing local doubling. The global counter must
       NOT scale a user-facing search's wait (bulk traffic could have
       driven it high), so `record_429`'s return value is ignored here.
- `parse_retry_after` unchanged.

Doubling moves from the local `2 ** (attempt - 1)` to the global
`consecutive_429s` counter: three processes each hitting one 429 produce
waits of 1×, 2×, 4× — system-wide escalation, which the local version could
never see.

### Callers

- `tasks.py::_embed_chunk_with_retry` — unchanged call,
  `call_with_429_backoff(lambda: client.embed_documents(texts))`; shared
  gate on by default.
- `embedding_client.py::embed_query` — passes `shared_gate=False`.

### What deliberately stays out (YAGNI)

- No wake-up jitter. The herd behind a pause is ≤ ~6 processes plus 4
  worker tasks; the gateway tolerates that burst. Revisit only if 429
  logs show synchronized re-rejection.
- No per-bucket split (search/background). The pause is one global scalar;
  search priority is expressed by *bypassing* it, not by a second bucket.
- No proactive admission control of any kind.

## Error handling

- DB unavailability is not specially handled: every caller of this code
  already requires the database (Procrastinate tasks, Django views), so a
  failing state read/write fails the operation the same way everything else
  would.
- The final 429 still propagates after `max_attempts`, preserving the
  existing layering: stamina retries transient network/5xx errors,
  Procrastinate retries whole tasks on extended outages, 429s are handled
  here and only here (`openai.RateLimitError` stays excluded from the
  stamina predicate).
- `record_429` runs inside the exception handler; its transaction is
  independent of any caller transaction (short, self-contained).

## Testing

Extends `radis/pgsearch/tests/test_rate_limiter.py` (pytest-django, real
DB):

1. `record_429` sets `paused_until` from the server wait; a second
   concurrent-style call extends via `max()` and doubles via the counter.
2. `record_success` resets the counter; issues no UPDATE when already 0
   (assert via `django_assert_num_queries`).
3. `call_with_429_backoff` with `shared_gate=True` sleeps out a
   pre-existing pause before the first attempt (intercept `_sleep`).
4. Pause extension mid-sleep: after the first sleep, state moved forward ⇒
   loops and sleeps again.
5. `shared_gate=False` (search): does not sleep on a pre-existing pause,
   but its 429 updates the shared row.
6. Global doubling: two sequential 429s "from different callers" (two
   separate `call_with_429_backoff` invocations) produce 1× then 2× waits.
   A subsequent `shared_gate=False` 429 retry still waits only the local
   1× server wait, unscaled by the global counter.
7. Existing behavior kept: final 429 propagates; success passes through.

## Migration note

`0006_embeddingbackoffstate` follows `0005_delete_embeddingratelimitevent`.
The prod image currently deployed was built before `0004`, so its next
deploy applies 0004→0005→0006 in one `migrate` run; the 0004/0005
create-then-drop pair is harmless and must stay (branch is pushed).
