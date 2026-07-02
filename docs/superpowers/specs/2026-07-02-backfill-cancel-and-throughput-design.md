# Embedding backfill cancellation + throughput knobs

## Context

A backfill (`embed_pending` or the admin enqueue action) defers hundreds of
independent `embed_reports_task` subjobs onto the `embeddings` queue. There
is no job object representing "the backfill", so there is currently no way
to stop one short of manual SQL against Procrastinate's tables. Separately,
the embeddings worker's `--concurrency 4` × `EMBEDDING_BATCH_SIZE=1000`
regularly hits the gateway's 60 req-equivalents/min sliding window, and none
of those knobs can be changed without editing code and rebuilding the image.

## Feature 1: `embed_cancel` management command

The backfill is identifiable without new state: backfill subjobs are
enqueued at `EMBEDDING_BACKFILL_PRIORITY` (0), live write-path subjobs at
`EMBEDDING_LIVE_PRIORITY` (1). Cancelling "the backfill" = cancelling queued
(`status="todo"`) `embed_reports_task` jobs at priority 0.

- New helper `cancel_backfill_embeddings() -> int` in
  `radis/pgsearch/tasks.py`: selects matching job ids (task name +
  `queue_name="embeddings"` + `status="todo"` + backfill priority) via the
  read-only `ProcrastinateJob` model (already used by the admin stats
  badge), cancels
  each with `app.job_manager.cancel_job_by_id(job_id)` (sync, race-safe —
  returns False for jobs a worker grabbed between select and cancel),
  returns the number actually cancelled.
- New command `radis/pgsearch/management/commands/embed_cancel.py`: calls
  the helper, prints the cancelled count and a reminder that running
  subjobs (at most the worker's concurrency) finish their current chunk
  and that `embed_pending` re-runs resume where things left off.
- Live-path subjobs (priority 1) are never touched.
- Decided while the user was away (flagged): no admin button (queue-scoped
  cancel doesn't fit row-scoped admin actions; the stats badge already
  shows the queue draining), and no abort of in-flight subjobs (bounded at
  concurrency × subjob_size reports, minutes of work). Revisit on request.

## Feature 2: env-configurable throughput knobs

In `radis/settings/base.py`, convert from hardcoded constants to env reads:

| Setting | Old | New default | Why changed |
|---|---|---|---|
| `EMBEDDING_BATCH_SIZE` | 1000 (const) | `env.int`, **200** | A 429'd/timed-out call wastes its whole payload and retries all texts; 200 bounds the waste and consumes the sliding window in smoother increments. |
| `EMBEDDING_SUBJOB_SIZE` | 1000 (const) | `env.int`, 1000 | Unchanged value; tunable for ops. |
| `EMBEDDING_REQUEST_TIMEOUT` | 30 (const) | `env.int`, 30 | Unchanged value; tunable when batch size changes. |

In `docker-compose.prod.yml`, the embeddings worker command becomes
`--concurrency ${EMBEDDINGS_WORKER_CONCURRENCY:-2}` (was hardcoded 4):
halves the burst the workers fire together when a shared 429 pause expires,
and is tunable per deployment without an image rebuild (stack redeploy
picks up .env). `example.env` documents all four knobs in the embedding
section.

The shared 429 backoff (see
[2026-07-02-shared-429-backoff-design.md](2026-07-02-shared-429-backoff-design.md))
remains the pacing mechanism; these knobs only shape burst size and waste
per rejection. No proactive rate limiting returns.

## Testing

- `cancel_backfill_embeddings`: defer real jobs at both priorities via
  `enqueue_embed_reports` against the test DB (Procrastinate's Django
  contrib uses the same connection), assert only priority-0 todo jobs
  flip to cancelled and the count is right; `embed_cancel` command smoke
  test via `call_command` asserting output.
- Settings and compose changes are configuration: env-backedness is
  established by the `env.int(...)` reads themselves and verified by
  `docker compose config` parsing plus code review, not pytest.
