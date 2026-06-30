# Embedding-pipeline logging — design

**Status:** approved (2026-06-30)
**Scope:** Write path (ingest → FTS upsert → defer → embed → store). Read path (`providers.search` / `providers.retrieve` / `EmbeddingClient.embed_query`) is out of scope.

## Motivation

Operators today have no visibility into the embedding pipeline beyond two existing logs (`bulk_index_reports` start, `embed_reports_task` per-offender ERROR). When the embedding service is intermittently flaky, when a backfill is stuck in the queue, or when a long-running task is silently progressing through HTTP calls, the only signal is the absence of NULL embeddings — eventually. We want pipeline-complete logs at the standard `radis` levels so that:

- An operator tailing `docker compose logs llm_worker` can see embedding work start, finish, and degrade in real time.
- A flaky embedding service surfaces as WARNING per retry, not as silence followed by a single ERROR 30 seconds later.
- A backfill (via `embed_pending` or admin action) leaves an audit trail in the same log stream as the worker.
- The existing OTEL pipeline (already wired through `adit_radis_shared.telemetry`) picks the new records up automatically.

## Non-goals

- No logs on the read path. Hybrid-search query logs would fire on every user search and are not what we want today. Existing FTS-fallback WARNINGs in `providers.py` stay as-is.
- No correlation IDs or `batch_id` field. OTEL trace context covers cross-task correlation when telemetry is active; Procrastinate's job ID covers it from the queue side. A homemade ID would partially duplicate both.
- No per-HTTP-chunk INFO inside `embed_reports_task`. A 1000-report subjob produces ~32 HTTP calls; an INFO per chunk would dominate the log stream. The stamina-retry WARNING covers the "something interesting happened mid-task" case.
- No logs inside `EmbeddingClient`. Exceptions raised at this layer carry full context (URL, status, body snippet); logging here would double-log with the task layer.
- No new settings, no log-level changes, no structured-logging (JSON) format change.

## Conventions

Matching existing patterns in `radis.pgsearch.tasks` and `radis.core.processors`:

- `logger = logging.getLogger(__name__)` per module.
- Lazy `%s` formatting in log calls; no f-strings (so disabled levels skip formatting).
- Each message prefixed with the function/task name (e.g. `embed_reports_task: ...`) so operators grep one prefix to pull the whole pipeline.
- Counts in named-field form inside the message body: `reports=42 embedded=40 skipped=2 duration_ms=1234 priority=1 attempt=2`.
- Long ID lists in ERROR/WARNING messages are truncated to 50 entries via a `_truncate_ids` helper, mirroring the `[:10]` truncation in `radis/pgsearch/utils/indexing.py`.

### Level semantics

- **INFO** — lifecycle events an operator wants in the normal flow ("start", "finished", "enqueued", "handler invoked").
- **WARNING** — degraded but recoverable: stamina retry, bisect kicking in, missing inputs that we work around.
- **ERROR** — fatal or skippable: client failure after retries, individual report skipped as too large.

## Coverage map

The write path, walked from ingest to storage:

| # | Surface | File | Action |
|---|---|---|---|
| 1 | `_index_reports` (ingest entry) | `radis/pgsearch/apps.py` | ADD INFO: handler invoked with N reports, sync/async mode |
| 2 | `bulk_upsert_report_search_indexes` | `radis/pgsearch/utils/indexing.py` | NO CHANGE — existing WARNING on missing reports stays |
| 3 | `bulk_index_reports` | `radis/pgsearch/tasks.py` | NO CHANGE — existing "Indexing N reports in bulk" INFO stays |
| 4 | `enqueue_bulk_index_reports` | `radis/pgsearch/tasks.py` | NO CHANGE — existing ERROR on invalid ids stays |
| 5 | `enqueue_embed_reports` | `radis/pgsearch/tasks.py` | ADD INFO: deferred N subjobs for M reports at priority P |
| 6 | `embed_reports_task` | `radis/pgsearch/tasks.py` | ADD INFO start; ADD INFO finish with duration; ADD ERROR on client failure (catch + re-raise); existing WARNING for missing RSI rows + ERROR for skipped offenders stay |
| 7 | `_embed_with_bisect` | `radis/pgsearch/tasks.py` | ADD WARNING on bisect; existing ERROR per-offender stays |
| 8 | `_embed_chunk_with_retry` | `radis/pgsearch/tasks.py` | ADD stamina `on_retry` hook → WARNING per retry |
| 9 | `EmbeddingClient` | `radis/pgsearch/utils/embedding_client.py` | NO CHANGE |
| 10 | `embed_pending` mgmt command | `radis/pgsearch/management/commands/embed_pending.py` | ADD INFO at invoke + done (in addition to existing stdout) |
| 11 | Admin: `enqueue_pending_embeddings` | `radis/pgsearch/admin.py` | ADD INFO with admin username + counts |
| 12 | Admin: `clear_embeddings_for_remodel` | `radis/pgsearch/admin.py` | ADD INFO with admin username + count |

## Concrete log lines

All new and kept message templates, listed by file.

### `radis/pgsearch/apps.py` — `_index_reports`

```python
logger.info(
    "pgsearch.index_reports: handler invoked; reports=%d mode=%s",
    len(reports),
    "sync" if settings.PGSEARCH_SYNC_INDEXING else "async",
)
```

### `radis/pgsearch/tasks.py` — `enqueue_embed_reports`

After the chunking loop, before `return count`:

```python
logger.info(
    "enqueue_embed_reports: deferred %d subjob(s) for %d report(s) at priority=%d",
    count,
    len(report_ids),
    priority,
)
```

### `radis/pgsearch/tasks.py` — stamina retry hook

Module-level callback, registered in `PgSearchConfig.ready()`:

```python
def _log_stamina_retry(details: "stamina.instrumentation.RetryDetails") -> None:
    if details.name != "radis.pgsearch.tasks._embed_chunk_with_retry":
        return
    logger.warning(
        "embed_reports_task: embedding HTTP call failed; attempt=%d "
        "retrying in %.2fs. Error: %s",
        details.retry_num,
        details.wait_for,
        details.caused_by,
    )
```

The name filter keeps the hook a no-op for any future stamina-decorated callsite that wants its own logging.

### `radis/pgsearch/tasks.py` — `_embed_with_bisect`

Before the recursive split (only fires when `len(rsvs) > 1`):

```python
logger.warning(
    "embed_reports_task: chunk of %d report(s) rejected as too large; "
    "bisecting to isolate offender(s).",
    len(rsvs),
)
```

Existing per-offender ERROR is unchanged:

```python
logger.error(
    "embed_reports_task: report_id=%s body_chars=%d rejected by embedding "
    "service as too large; skipping. Backend error: %s",
    offender.report.pk,
    len(offender.report.body),
    exc,
)
```

### `radis/pgsearch/tasks.py` — `embed_reports_task`

At the start, after the empty-input early return:

```python
logger.info("embed_reports_task: start; reports=%d", len(report_ids))
start_t = time.perf_counter()
```

Existing missing-RSI WARNING unchanged.

Around the embedding loop, replacing the bare `with EmbeddingClient()` block:

```python
try:
    with EmbeddingClient() as client:
        for start in range(0, len(rsvs), batch_size):
            chunk = rsvs[start : start + batch_size]
            _embed_with_bisect(client, chunk, embedded, skipped)
except EmbeddingClientError as exc:
    logger.error(
        "embed_reports_task: embedding client failure after retries; "
        "report_ids=%s. Will be retried by Procrastinate. Error: %s",
        _truncate_ids(report_ids),
        exc,
    )
    raise
```

Existing skipped-as-too-large ERROR stays, with the ID list passed through `_truncate_ids` for consistency:

```python
logger.error(
    "embed_reports_task: %d report(s) skipped as too large for the embedding "
    "model; report_ids=%s. Fix the upstream report or raise the model context "
    "limit; their RSI rows stay NULL until embedded.",
    len(skipped),
    _truncate_ids([rsv.report.pk for rsv in skipped]),
)
```

At the end:

```python
duration_ms = int((time.perf_counter() - start_t) * 1000)
logger.info(
    "embed_reports_task: finished; embedded=%d skipped=%d duration_ms=%d",
    len(embedded),
    len(skipped),
    duration_ms,
)
```

### `radis/pgsearch/tasks.py` — helper

```python
def _truncate_ids(ids: list[int], limit: int = 50) -> list[int]:
    return list(ids[:limit])
```

### `radis/pgsearch/management/commands/embed_pending.py`

Module-level `logger = logging.getLogger(__name__)`. In `handle()`:

```python
logger.info(
    "embed_pending: command invoked; subjob_size=%d limit=%s",
    subjob_size,
    opts["limit"],
)
# ... existing stdout + enqueue_embed_reports call ...
logger.info(
    "embed_pending: done; reports=%d subjobs=%d",
    len(ids),
    subjob_count,
)
```

Existing stdout writes stay (they're the CLI-UX feedback to the operator running the command interactively; the logs serve aggregated log search).

### `radis/pgsearch/admin.py`

Module-level `logger = logging.getLogger(__name__)`. Inside `enqueue_pending_embeddings`, after the deferral:

```python
logger.info(
    "admin.enqueue_pending_embeddings: user=%s enqueued %d report(s) "
    "across %d subjob(s)",
    request.user.username,
    len(report_ids),
    subjob_count,
)
```

Inside `clear_embeddings_for_remodel`, after the update:

```python
logger.info(
    "admin.clear_embeddings_for_remodel: user=%s cleared %d embedding(s)",
    request.user.username,
    cleared,
)
```

## Stamina hook registration

Add to `radis/pgsearch/apps.py` `PgSearchConfig.ready()`, after the signals import:

```python
def ready(self):
    from . import signals as signals  # noqa: F401
    from .tasks import _log_stamina_retry

    import stamina.instrumentation
    stamina.instrumentation.set_on_retry_hooks([_log_stamina_retry])

    register_app()
```

`set_on_retry_hooks` replaces stamina's default hook set (which includes its own structlog/logging instrumentation if installed). We want a single deliberate hook with our message format. If a future PR adds another stamina-decorated callsite that wants logging too, that callsite extends the hook list rather than registering independently.

## Tests

All new tests follow the existing pattern in `test_embed_reports_task.py`: the `radis` logger has `propagate=False`, so the test attaches `caplog.handler` directly to the named logger for the duration of the test.

New tests to add in `radis/pgsearch/tests/test_embed_reports_task.py`:

- `test_logs_start_and_finish_with_duration` — calls `embed_reports_task` with a small batch under a mocked client, asserts INFO records with `start;` and `finished;` substrings and that the finish record contains `duration_ms=`.
- `test_logs_warning_on_stamina_retry` — uses the existing `stamina_active` fixture; mocks `embed_documents` to raise then succeed; asserts a WARNING with `attempt=1` and `retrying in` substrings.
- `test_logs_error_on_client_failure_and_reraises` — mocks `embed_documents` to raise `EmbeddingClientError` repeatedly; asserts the ERROR log fires once with `client failure after retries` and that the exception still propagates (preserving Procrastinate retry).

New tests to add elsewhere:

- `radis/pgsearch/tests/test_apps_checks.py` (or a new sibling file) — assert `_log_stamina_retry` is registered as a stamina on-retry hook after app ready, and that calling it with a `RetryDetails` whose `name` matches emits the WARNING.
- `radis/pgsearch/tests/test_embed_pending_command.py` — assert the two new INFO lines fire when the command runs against a populated queryset.
- `radis/pgsearch/tests/test_admin.py` — assert the admin-action INFOs fire with the expected username and counts.

Existing test `test_bisects_on_too_large_and_isolates_offender` still passes — its `any(...)` assertions accept any matching record among the captured ERRORs, and the new finish-time INFO doesn't affect ERROR filtering.

## Disposition of partial edits already on disk

Before this brainstorm started, exploratory edits in `radis/pgsearch/tasks.py` added five logs. The implementation plan reconciles them with the design:

| Already on disk | Disposition |
|---|---|
| INFO `embed_reports_task: start; reports=%d` | Keep — matches design verbatim. |
| INFO `embed_reports_task: finished; embedded=%d skipped=%d` | Revise — add `duration_ms=%d` field per design. |
| ERROR catch block on `EmbeddingClientError` with re-raise | Revise — wording `service unreachable after retries` → `embedding client failure after retries` to cover misconfig + outage uniformly. |
| WARNING bisect message | Keep — matches design verbatim. |
| INFO `Enqueued %d embedding subjob(s) for %d report(s) at priority=%d` in `enqueue_embed_reports` | Revise — message prefix `Enqueued` → `enqueue_embed_reports:` for prefix-consistency with the rest of the pipeline. |

## Acceptance criteria

- `uv run cli lint` passes with no new findings.
- The pgsearch test suite passes, including the existing log-assertion test and all new log-assertion tests.
- A manual smoke (start the dev stack, ingest a small batch via API or factories, watch `docker compose logs llm_worker`) shows INFO start / finish lines and an INFO line from `enqueue_embed_reports`.
- Stopping the embedding service mid-run produces WARNING per stamina attempt and an ERROR on retry exhaustion.
- Running `embed_pending` produces invoke / done INFO lines visible in worker logs (not just the operator's terminal).
