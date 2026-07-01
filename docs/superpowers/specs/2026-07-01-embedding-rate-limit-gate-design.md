# Embedding rate-limit gate — design

## Motivation

The embedding gateway (`the internal embedding gateway`, OpenAI-compatible) enforces a request-rate
limit that RADIS does not currently respect proactively. Today, a 429
(`openai.RateLimitError`) is deliberately excluded from stamina's retry predicate
(`_is_retryable_embedding_error` in `radis/pgsearch/tasks.py`) with a comment noting
it should reach "the rate-limit gate" — but that gate was never built. A 429
propagates raw and uncaught: it isn't an `EmbeddingClientError` subclass, so
`embed_reports_task`'s own error handling doesn't catch it either, and it falls
through to Procrastinate's generic task retry, which simply retries the whole
subjob later at the same size — no smarter than doing nothing.

This became concrete while deciding on a concurrency setting for
`embeddings_worker` (`./manage.py bg_worker -q embeddings --concurrency N`):
increasing concurrency or batch size without understanding the real limit risks
hitting sustained 429s with no graceful handling.

## Scope

- **Embeddings only.** Chat/extraction traffic (`radis.chats`, `radis.extractions`)
  currently shares the same gateway host and API key as embeddings
  (`EXTERNAL_LLM_PROVIDER_API_KEY` == `EMBEDDING_PROVIDER_API_KEY` today), but a
  separate API key for LLM/chat traffic is planned operationally. This gate does
  not coordinate with chat/extraction traffic.
- **Cross-process, not single-process.** Exactly two call sites in the codebase
  ever issue a real embedding HTTP request (confirmed by exhaustively grepping
  every call to `embed_documents`/`EmbeddingClient(`):
  - `_embed_chunk_with_retry` in `radis/pgsearch/tasks.py` — the background bulk
    tier. Reached from `embed_reports_task`, which is itself the sole path for
    *every* write-path embedding trigger (single-report save, bulk upsert, the
    async FTS chain, `embed_pending`, and the admin action all just call
    `enqueue_embed_reports()` to defer to this task — none call
    `embed_documents` directly). Runs in the `embeddings_worker` process.
  - `embed_query()` in `radis/pgsearch/utils/embedding_client.py` — the
    search/retrieval tier. Reached from `radis/pgsearch/providers.py`, which is
    called by live search (`web`), the extraction job wizard's search step
    (`web`), and `radis/subscriptions/tasks.py`'s `retrieval_provider.retrieve()`
    call, which runs on `@app.task(queue="llm")` — i.e. the `llm_worker`
    process. Gating `embed_query()` itself automatically covers all three
    current callers, and any future caller that does retrieval, without needing
    per-caller changes.

  Because these two call sites run in three different OS processes (`web`,
  `llm_worker`, `embeddings_worker`), the gate needs shared state visible to
  all three. There's no Redis in this stack; Postgres (already the backbone for
  everything else, including Procrastinate's own queue) is the natural choice.

- **Priority: search/retrieval over background bulk.** Search is low-volume and
  interactive (blocking a live user's search request is a real UX cost);
  background bulk embedding is high-volume and non-interactive (a delay is
  invisible). The design gives search/retrieval a reserved capacity floor it can
  always use, plus the ability to opportunistically borrow background's unused
  capacity. Background can never borrow from search's reserved floor.

## Empirical findings informing the design

Tested directly against the live production gateway (`the internal embedding gateway`) using the
project's own `EmbeddingClient`/`openai` SDK configuration:

1. **The limit is 60 request-equivalents per minute.** Confirmed via the literal
   429 body: `"Limit 60/min exceeded. Wait Xs."`.

2. **It is a true sliding window keyed to send time, not a fixed window, not
   concurrency/completion-based, and not a continuous per-second refill.**
   Confirmed by three independent, decisive tests:
   - A single request taking ~80s to complete still had its slot free up ~60s
     after it was *sent*, while still in flight (checked at t=62s/68s/75s, all
     succeeding before the slow request finished at t=82s) — rules out
     concurrency/in-flight or completion-time models.
   - Immediately after exhausting the budget via a fast sequential burst, the
     next request reported a ~50–55s wait, decreasing linearly and resolving at
     almost exactly the predicted time — a continuous 1/sec refill would
     predict a ~1s wait instead, which rules that model out.
   - A two-wave test (Wave A: 40 requests sent at t≈0.5–2.4s; Wave B: 19 more at
     t≈30.5–31.4s before rejection) produced two distinct, correctly-sized,
     correctly-timed recovery events: a clump of 41 (Wave A's 40 plus one extra
     probe from the pre-test clean-window check) at t≈60.5s, and a clump of
     exactly 19 at t≈90.5s — matching "Wave A's oldest + 60s" and "Wave B's
     oldest + 60s" almost exactly. This is only explainable by each admitted
     request carrying its own independent 60-second expiry, i.e. a genuine
     sliding window over individual timestamps.

3. **Cost may be weighted by request size, but the exact formula is
   unconfirmed.** Two clean sequential tests (500-item batch → 1 fewer quick
   request fit than a flat-cost model predicts; 2000-item batch → 5 fewer)
   suggest cost scales with size, but two data points aren't enough to fit
   whether it's linear in item count, token count, or something else. A third,
   earlier data point from a *concurrent*-burst test didn't fit the same trend,
   most likely because concurrent dispatch introduced noise into the gateway's
   own accounting rather than reflecting a different relationship — that
   result should not be used for calibration.

4. **The 429 response includes a specific "Wait Xs" value**, parseable and
   authoritative — more trustworthy than anything this gate could compute
   internally, especially given point 3's unresolved uncertainty.

Given point 3's uncertainty, this design deliberately does **not** attempt to
model per-item/per-token cost precisely in v1. Every request is recorded with
weight 1 in the proactive ledger; the reactive layer (below) is the safety net
for whatever the proactive estimate misses. If 429s turn out to be frequent in
production despite the gate, that's the signal to revisit the weight model with
production data rather than more synthetic probing.

## Architecture

### Data model

A new model in `radis.pgsearch`:

```python
class EmbeddingRateLimitEvent(models.Model):
    bucket = models.CharField(max_length=32)
    sent_at = models.DateTimeField()
    weight = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [models.Index(fields=["bucket", "sent_at"])]
```

This is an append-only sliding-window ledger, not a counter row — matching the
confirmed per-request-independent-expiry behavior. Two logical bucket names:
`"embedding_search"` and `"embedding_background"`. Capacity per bucket is a
Django setting, not stored data — the table only holds the event log.
Real traffic is bounded to ~60 units/minute, so old rows (pruned opportunistically
on every acquisition, see below) keep this table small automatically; no
separate cleanup job is needed.

### Settings

New settings in `radis/settings/base.py`, following the existing `EMBEDDING_*`
naming convention:

- `EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE = 10`
- `EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE = 50`

Defaults sum to 60 (the discovered ceiling) with a 10/50 split favoring
background throughput, since search is low-volume. Both values, and the split
itself, are operator-tunable — a starting point, not a fixed decision baked
into code. Operators should leave headroom below the true ceiling given the
unresolved cost-weighting uncertainty (point 3 above).

### Module layout

All of the following — `acquire_token`, `try_acquire_immediate`,
`acquire_search_priority_token`, `call_with_rate_limit`, and
`parse_retry_after` — live in a new module,
`radis/pgsearch/utils/rate_limiter.py`. `configured_capacity(bucket)` in that
module maps bucket names to settings directly:
`"embedding_search"` → `settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE`,
`"embedding_background"` → `settings.EMBEDDING_BACKGROUND_RATE_LIMIT_PER_MINUTE`.

### `acquire_token(bucket, weight=1)` — blocking, proactive

```
loop:
    take a Postgres advisory transaction lock keyed on the bucket name
    (serializes concurrent acquisition across all three processes, since
     it's a database-wide lock, not per-process/per-thread)

    now = current time
    delete rows for this bucket with sent_at older than (now - 60s)   # opportunistic cleanup
    current_weight = sum(weight) over remaining rows for this bucket

    if current_weight + weight <= configured_capacity(bucket):
        insert a new row (bucket, now, weight)
        commit (releases the lock); return                            # caller proceeds

    else:
        oldest = the oldest remaining row for this bucket
        wait_for = 60 - (now - oldest.sent_at)
        commit (releases the lock)
        sleep(wait_for)
        retry the loop
```

### `try_acquire_immediate(bucket, weight=1) -> bool` — non-blocking variant

Same as above but returns `False` immediately instead of computing a wait and
sleeping, when there isn't room. Used by the search-priority spillover logic
below.

### Search-priority spillover

```python
def acquire_search_priority_token(weight=1):
    if try_acquire_immediate("embedding_search", weight):
        return
    if try_acquire_immediate("embedding_background", weight):
        return
    # both exhausted right now; wait on the bucket reserved for us
    acquire_token("embedding_search", weight)
```

Background's own acquisition path only ever calls `acquire_token("embedding_background", weight)`
directly — it has no code path that references `"embedding_search"`, so it
structurally cannot borrow from search's reserved floor.

### Reactive layer: honoring the server's own 429

```python
def call_with_rate_limit(acquire_fn, fn, max_attempts=3):
    for attempt in range(max_attempts):
        acquire_fn()
        try:
            return fn()
        except openai.RateLimitError as exc:
            wait = parse_retry_after(exc)  # HTTP Retry-After header first, else
                                            # regex `Wait (\d+)s` on the body
            logger.warning(
                "embedding rate-limit gate: got 429 despite internal gating; "
                "honoring server-reported wait=%ss (attempt %d/%d)",
                wait, attempt + 1, max_attempts,
            )
            time.sleep(wait)
    raise  # exhausted; let Procrastinate's task-level retry take over
```

The proactive ledger entry inserted before the failed attempt is deliberately
**not** rolled back or corrected — a 429 means the real gateway is more
constrained than our estimate, so removing our own record would only make
future estimates more optimistic, the wrong direction. The ledger stays a
best-effort heuristic; the reactive layer is the authoritative backstop.

`max_attempts=3` matches the existing stamina retry budget convention
elsewhere in `tasks.py`.

### Call sites

- `_embed_chunk_with_retry` (`tasks.py`): wrap the existing
  `client.embed_documents(texts)` call with
  `call_with_rate_limit(lambda: acquire_token("embedding_background"), lambda: client.embed_documents(texts))`.
  No other change to the existing stamina/bisect logic.
- `embed_query()` (`embedding_client.py`): wrap `self.embed_documents([prefixed])`
  with `call_with_rate_limit(acquire_search_priority_token, lambda: self.embed_documents([prefixed]))`.

## Error handling

- Postgres errors during `acquire_token` (e.g. a connection blip) are not
  special-cased — they propagate the same as any other DB error elsewhere in
  the app, handled by the existing Procrastinate task-retry policy for the
  background tier.
- If `call_with_rate_limit` exhausts `max_attempts` still hitting 429s, it
  re-raises `openai.RateLimitError`. For the background tier this propagates to
  Procrastinate's task-level retry (existing behavior, unchanged). For the
  search tier (a live request in `web`), this is an accepted rare-edge-case:
  given the reserved floor, exhausting 3 attempts should be uncommon, and no
  new graceful-degradation UX is in scope here.

## Testing

- `acquire_token`: unit tests against the real test DB, with an injectable
  clock (matching the existing pattern of fixtures like `stamina_active` /
  `caplog_tasks` in `test_embed_reports_task.py`) covering: capacity
  enforcement, independent per-row expiry (two waves recover independently,
  mirroring the empirical test), and opportunistic pruning of old rows.
- `acquire_search_priority_token`: unit tests for spillover — search bucket
  exhausted + background has room → borrows from background; both exhausted →
  waits on search specifically (not background).
- `call_with_rate_limit`: unit tests mocking `openai.RateLimitError` with a
  `"Wait Xs"` body, asserting it sleeps that exact duration (via a
  monkeypatched `time.sleep`, not a real sleep) and retries; asserts it raises
  after `max_attempts`.
- Existing `embed_reports_task`/bisect tests: the new gate wraps the existing
  call, so those tests need the gate mocked/patched to a passthrough (a new
  fixture, consistent with how `stamina_active` is scoped) so they don't
  exercise real DB-backed gating or slow down with real sleeps.

## Migration

One new Django migration in `radis.pgsearch` adding `EmbeddingRateLimitEvent`.
No data migration needed — the table starts empty and is purely a runtime
ledger.
