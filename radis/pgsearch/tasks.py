import logging
import time

import openai
import stamina
import stamina.instrumentation
from django.conf import settings
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.types import JSONValue

from .models import ReportSearchIndex
from .utils.embedding_client import EmbeddingClient, EmbeddingClientError
from .utils.indexing import bulk_upsert_report_search_indexes
from .utils.rate_limiter import call_with_429_backoff

logger = logging.getLogger(__name__)


def _truncate_ids(ids: list[int], limit: int = 50) -> list[int]:
    return list(ids[:limit])


# Transient classes we retry. 429 (RateLimitError) is deliberately NOT here —
# `call_with_429_backoff` handles it with the server-reported wait time;
# stamina's fixed backoff would retry too early and silence the 429.
_RETRYABLE_OPENAI_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
)


def _is_retryable_embedding_error(exc: Exception) -> bool:
    """stamina retry predicate. Retry transient failures from either the old
    EmbeddingClientError surface or the typed openai SDK surface. Exclude
    openai.RateLimitError (`call_with_429_backoff` handles it; retrying here
    would silence the 429)."""
    if isinstance(exc, openai.RateLimitError):
        return False
    if isinstance(exc, EmbeddingClientError):
        return True
    return isinstance(exc, _RETRYABLE_OPENAI_ERRORS)


@stamina.retry(
    on=_is_retryable_embedding_error,
    attempts=3,
    timeout=30.0,
    wait_initial=0.5,
    wait_max=8.0,
)
def _embed_chunk_with_retry(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """Single embed call wrapped in stamina-controlled transient retries.

    Layered with Procrastinate's task-level retry: stamina handles brief
    blips (3 attempts within ~30s); Procrastinate handles extended outages
    (whole-task retry on backoff). Gateway 429s are handled inside
    `call_with_429_backoff` with exponential backoff seeded by the
    server-reported wait time."""
    return call_with_429_backoff(lambda: client.embed_documents(texts))


def _log_stamina_retry(details: stamina.instrumentation.RetryDetails) -> None:
    """Stamina on_retry hook. Logs WARNING per retry attempt of the
    embedding HTTP call. Filters by callsite name so this hook stays a
    no-op for any other stamina-decorated function added later."""
    if details.name != "radis.pgsearch.tasks._embed_chunk_with_retry":
        return
    logger.warning(
        "embed_reports_task: embedding HTTP call failed; attempt=%d retrying in %.2fs. Error: %s",
        details.retry_num,
        details.wait_for,
        details.caused_by,
    )


@app.task
def bulk_index_reports(report_ids: list[int]) -> None:
    """Deferred FTS bulk-indexing for the bulk-upsert path
    (when `PGSEARCH_SYNC_INDEXING=False`).

    Chains into `embed_reports_task` subjobs once RSV rows exist, so the
    embeddings worker never reads a missing `report.body` or a stale tsvector.
    """
    if not report_ids:
        return
    logger.info("Indexing %s reports in bulk.", len(report_ids))
    bulk_upsert_report_search_indexes(report_ids)
    enqueue_embed_reports(report_ids)


def enqueue_bulk_index_reports(report_ids: list[int]) -> int | None:
    if not report_ids:
        return None
    try:
        payload: list[JSONValue] = [int(report_id) for report_id in report_ids]
    except (TypeError, ValueError) as exc:
        logger.error("Invalid report_id in bulk index request: %s", exc)
        return None
    return app.configure_task(
        "radis.pgsearch.tasks.bulk_index_reports",
        allow_unknown=False,
    ).defer(report_ids=payload)


def enqueue_embed_reports(
    report_ids: list[int],
    *,
    subjob_size: int | None = None,
    priority: int | None = None,
) -> int:
    """Chunk `report_ids` into subjobs and defer one `embed_reports_task`
    per chunk. Returns the number of subjobs deferred.

    Subjob size defaults to `settings.EMBEDDING_SUBJOB_SIZE` (the
    Procrastinate-task granularity). It's distinct from
    `settings.EMBEDDING_BATCH_SIZE` (the per-HTTP-call size inside one
    task) — each subjob makes ceil(subjob_size / EMBEDDING_BATCH_SIZE)
    HTTP calls. A 1M-report backfill becomes ~1k subjobs; many workers can
    drain in parallel, retries have bounded blast radius, and a stuck
    task can't tie up the worker on the whole queue's worth of work.

    Priority defaults to `settings.EMBEDDING_LIVE_PRIORITY` (write-path).
    `embed_pending` and the admin backfill action override to
    `settings.EMBEDDING_BACKFILL_PRIORITY`, so a million-row backfill
    can't park itself ahead of every subsequent live ingest write.

    Single call site for every place that enqueues embedding work: the
    write-path handler, the FTS chain tail, `embed_pending`, and the
    admin action. Operators read one knob, not several.
    """
    if not report_ids:
        return 0
    size = subjob_size if subjob_size is not None else settings.EMBEDDING_SUBJOB_SIZE
    if priority is None:
        priority = settings.EMBEDDING_LIVE_PRIORITY
    deferrer = app.configure_task(
        "radis.pgsearch.tasks.embed_reports_task",
        allow_unknown=False,
        priority=priority,
    )
    count = 0
    for start in range(0, len(report_ids), size):
        chunk = report_ids[start : start + size]
        deferrer.defer(report_ids=list(chunk))
        count += 1
    logger.info(
        "enqueue_embed_reports: deferred %d subjob(s) for %d report(s) at priority=%d",
        count,
        len(report_ids),
        priority,
    )
    return count


def cancel_backfill_embeddings() -> int:
    """Cancel every queued (todo) backfill-priority embed subjob.

    "The backfill" has no job object of its own — it is exactly the
    embed_reports_task jobs enqueued at EMBEDDING_BACKFILL_PRIORITY
    (embed_pending / admin action), which the live write-path priority
    keeps distinct. Cancellation goes job-by-job through Procrastinate's
    cancel_job_by_id, which is race-safe: a job a worker grabbed between
    our select and the cancel returns False and simply runs to completion.
    Returns the number of jobs actually cancelled. Resume = re-run
    embed_pending (idempotent, embedding IS NULL filter)."""
    job_ids = list(
        ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task",
            queue_name="embeddings",
            status="todo",
            priority=settings.EMBEDDING_BACKFILL_PRIORITY,
        ).values_list("id", flat=True)
    )
    cancelled = sum(1 for job_id in job_ids if app.job_manager.cancel_job_by_id(job_id))
    logger.info(
        "cancel_backfill_embeddings: cancelled %d of %d queued backfill subjob(s)",
        cancelled,
        len(job_ids),
    )
    return cancelled


@app.task(queue="embeddings")
def embed_reports_task(report_ids: list[int]) -> None:
    """Embed the named reports.

    Failure handling, from innermost to outermost:

    * Gateway 429s: `call_with_429_backoff` retries with exponential
      backoff seeded by the server-reported wait time.
    * Transient errors (connection, timeout, 5xx): stamina retries
      (3 attempts, ~30s budget) inside `_embed_chunk_with_retry`.
    * Anything that escapes both propagates so Procrastinate's task-level
      retry policy applies to the whole subjob.

    Callers must ensure ReportSearchIndex rows exist before deferring this
    task. `bulk_index_reports` chains the defer at the end of its run, and
    `embed_pending` / the admin action filter on existing RSV rows by
    construction.
    """
    if not report_ids:
        return

    logger.info("embed_reports_task: start; reports=%d", len(report_ids))
    start_t = time.perf_counter()

    rsvs = list(
        ReportSearchIndex.objects.filter(report_id__in=report_ids)
        .select_related("report")
        .only("id", "report_id", "report__body")
    )
    if not rsvs:
        logger.warning(
            "embed_reports_task: no ReportSearchIndex rows for report ids %s",
            report_ids,
        )
        return

    batch_size = settings.EMBEDDING_BATCH_SIZE
    embedded: list[ReportSearchIndex] = []
    try:
        with EmbeddingClient() as client:
            for start in range(0, len(rsvs), batch_size):
                chunk = rsvs[start : start + batch_size]
                vectors = _embed_chunk_with_retry(client, [rsv.report.body for rsv in chunk])
                for rsv, vec in zip(chunk, vectors, strict=True):
                    rsv.embedding = vec
                    embedded.append(rsv)
    except EmbeddingClientError as exc:
        logger.error(
            "embed_reports_task: embedding client failure after retries; "
            "report_ids=%s. Will be retried by Procrastinate. Error: %s",
            _truncate_ids(report_ids),
            exc,
        )
        raise

    if embedded:
        ReportSearchIndex.objects.bulk_update(embedded, fields=["embedding"])
    duration_ms = int((time.perf_counter() - start_t) * 1000)
    logger.info(
        "embed_reports_task: finished; embedded=%d duration_ms=%d",
        len(embedded),
        duration_ms,
    )
