import logging
import time

from django.conf import settings
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.types import JSONValue

from radis.core.utils.rate_limit import (
    TRANSIENT_ERRORS,
    RateLimited,
    run_through_gate,
    with_transient_retries,
)

from .models import ReportSearchIndex
from .utils.embedding_client import EMBEDDING_GATE, EmbeddingClient, EmbeddingClientError
from .utils.indexing import bulk_upsert_report_search_indexes

logger = logging.getLogger(__name__)


def _truncate_ids(ids: list[int], limit: int = 50) -> list[int]:
    return list(ids[:limit])


# Procrastinate task-level retry: the outermost failure layer, after the local
# transient retries (brief blips, seconds) and the rate-limit gate (429s, up
# to the batch budget). Exponential spacing (6s, 36s, ~4min, ~22min) covers
# extended provider outages. Scoped to transient classes so misconfiguration
# (auth, bad model name) fails the subjob immediately instead of burning
# retries. A subjob that exhausts these attempts fails permanently —
# `embed_pending` re-enqueues any reports still missing embeddings.
EMBEDDING_TASK_RETRY_STRATEGY = RetryStrategy(
    max_attempts=settings.EMBEDDING_TASK_MAX_ATTEMPTS,
    exponential_wait=settings.EMBEDDING_TASK_EXPONENTIAL_WAIT_SECONDS,
    retry_exceptions={RateLimited, EmbeddingClientError, *TRANSIENT_ERRORS},
)


def _embed_chunk_with_retry(client: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """Single embed call with the same layering as the LLM client: the
    rate-limit gate outermost (429s: wait out the server-reported pause or
    the exponential ladder within one batch budget, then raise RateLimited),
    local transient retries inside (brief blips: connection/timeout/5xx and
    malformed responses raised as EmbeddingClientError). 429 is not in the
    retryable tuple, so it passes straight to the gate; RateLimited and
    exhausted transient errors escape to Procrastinate's task-level retry."""
    return run_through_gate(
        EMBEDDING_GATE,
        settings.EMBEDDING_RATE_LIMIT_MAX_WAIT_SECONDS,
        lambda: with_transient_retries(
            lambda: client.embed_documents(texts),
            settings.EMBEDDING_TRANSIENT_RETRY_ATTEMPTS,
            settings.EMBEDDING_TRANSIENT_RETRY_BASE_SECONDS,
            retryable=(*TRANSIENT_ERRORS, EmbeddingClientError),
        ),
    )


@app.task(retry=RetryStrategy(max_attempts=3, wait=10))
def bulk_index_reports(report_ids: list[int]) -> None:
    """Deferred FTS bulk-indexing for the bulk-upsert path
    (when `PGSEARCH_SYNC_INDEXING=False`).

    Chains into `embed_reports_task` subjobs once ReportSearchIndex rows exist, so the
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
    Returns the number of jobs actually cancelled. Cancelled jobs are
    terminal — continuing the backfill means re-running embed_pending,
    which enqueues the still-NULL reports as fresh subjobs chunked at the
    then-current EMBEDDING_SUBJOB_SIZE."""
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


@app.task(queue="embeddings", retry=EMBEDDING_TASK_RETRY_STRATEGY)
def embed_reports_task(report_ids: list[int]) -> None:
    """Embed the named reports.

    Failure handling, from innermost to outermost:

    * Transient errors (connection, timeout, 5xx, malformed responses):
      retried locally inside `_embed_chunk_with_retry`
      (EMBEDDING_TRANSIENT_RETRY_ATTEMPTS retries with exponential backoff).
    * Gateway 429s: the per-process EMBEDDING_GATE waits out the
      server-reported pause (or an exponential ladder) up to the batch
      budget, then raises RateLimited.
    * Anything that escapes both propagates so EMBEDDING_TASK_RETRY_STRATEGY
      retries the whole subjob (transient classes only).

    Callers must ensure ReportSearchIndex rows exist before deferring this
    task. `bulk_index_reports` chains the defer at the end of its run, and
    `embed_pending` / the admin action filter on existing ReportSearchIndex rows by
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
    if len(rsvs) < len(report_ids):
        missing = sorted(set(report_ids) - {rsv.report.pk for rsv in rsvs})
        logger.warning(
            "embed_reports_task: %d report(s) have no ReportSearchIndex row and are "
            "skipped; ids=%s",
            len(missing),
            _truncate_ids(missing),
        )
        if not rsvs:
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
