import logging
import time

from django.conf import settings
from django.db.models import F
from django.db.models.functions import Now
from django.utils import timezone
from procrastinate import RetryStrategy
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.types import JSONValue

from radis.core.utils.embedding_client import (
    EMBEDDING_GATE,
    PERMANENT_EMBEDDING_ERRORS,
    EmbeddingClient,
    EmbeddingClientError,
)
from radis.core.utils.rate_limit import (
    TRANSIENT_ERRORS,
    RateLimited,
    run_through_gate,
    with_transient_retries,
)

from .models import EmbeddingBackfillRun, ReportSearchIndex
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
    max_attempts=settings.EMBEDDINGS_TASK_MAX_ATTEMPTS,
    exponential_wait=settings.EMBEDDINGS_TASK_EXPONENTIAL_WAIT_SECONDS,
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
        settings.EMBEDDINGS_RATE_LIMIT_MAX_WAIT_SECONDS,
        lambda: with_transient_retries(
            lambda: client.embed_documents(texts),
            settings.EMBEDDINGS_TRANSIENT_RETRY_ATTEMPTS,
            settings.EMBEDDINGS_TRANSIENT_RETRY_BASE_SECONDS,
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


class ActiveBackfillError(Exception):
    """Raised when starting a backfill while another is still active."""


def create_backfill_run(total_reports: int, triggered_by: str) -> EmbeddingBackfillRun:
    """Create the run row for a backfill, enforcing single-active (§6.8).

    Refuses while a run with live subjobs is active. An active run with NO
    live subjobs and an unfinished counter is abandoned (jobs lost to retry
    exhaustion or a dead worker): auto-close it and proceed, so a wedged
    run can never block future backfills. Small check-then-act race window
    is acceptable for operator tooling."""
    active = EmbeddingBackfillRun.get_active()
    if active is not None:
        if active.live_subjob_count() > 0:
            raise ActiveBackfillError(
                f"Backfill already active (run {active.pk}: "
                f"{active.processed_reports}/{active.total_reports} reports processed). "
                f"Cancel it first with `embed_cancel` or the admin button."
            )
        active.cancelled_at = timezone.now()
        active.save(update_fields=["cancelled_at"])
        logger.warning(
            "create_backfill_run: auto-closed abandoned run %d (%d/%d processed, no live subjobs)",
            active.pk,
            active.processed_reports,
            active.total_reports,
        )
    return EmbeddingBackfillRun.objects.create(
        total_reports=total_reports, triggered_by=triggered_by
    )


def enqueue_embed_reports(
    report_ids: list[int],
    *,
    subjob_size: int | None = None,
    priority: int | None = None,
    run_id: int | None = None,
) -> int:
    """Chunk `report_ids` into subjobs and defer one `embed_reports_task`
    per chunk. Returns the number of subjobs deferred.

    Subjob size defaults to `settings.EMBEDDINGS_SUBJOB_SIZE` (the
    Procrastinate-task granularity). It's distinct from
    `settings.EMBEDDINGS_BATCH_SIZE` (the per-HTTP-call size inside one
    task) — each subjob makes ceil(subjob_size / EMBEDDINGS_BATCH_SIZE)
    HTTP calls. A 1M-report backfill becomes ~1k subjobs; many workers can
    drain in parallel, retries have bounded blast radius, and a stuck
    task can't tie up the worker on the whole queue's worth of work.

    Priority defaults to `settings.EMBEDDINGS_LIVE_PRIORITY` (write-path).
    `embed_pending` and the admin backfill action override to
    `settings.EMBEDDINGS_BACKFILL_PRIORITY`, so a million-row backfill
    can't park itself ahead of every subsequent live ingest write.

    Single call site for every place that enqueues embedding work: the
    write-path handler, the FTS chain tail, `embed_pending`, and the
    admin action. Operators read one knob, not several.

    `run_id` ties backfill subjobs to their `EmbeddingBackfillRun`;
    write-path enqueues leave it None.
    """
    if not report_ids:
        return 0
    if settings.EMBEDDINGS_MODEL is None:
        # FTS-only/unconfigured deployment: enqueuing embedding subjobs here would
        # only create Procrastinate jobs doomed to fail at client construction.
        # Skip them; search already runs FTS-only.
        logger.info(
            "enqueue_embed_reports: EMBEDDINGS_MODEL not configured; "
            "skipping embedding of %d report(s) (FTS-only deployment)",
            len(report_ids),
        )
        return 0
    size = subjob_size if subjob_size is not None else settings.EMBEDDINGS_SUBJOB_SIZE
    if priority is None:
        priority = settings.EMBEDDINGS_LIVE_PRIORITY
    deferrer = app.configure_task(
        "radis.pgsearch.tasks.embed_reports_task",
        allow_unknown=False,
        priority=priority,
    )
    count = 0
    for start in range(0, len(report_ids), size):
        chunk = report_ids[start : start + size]
        kwargs: dict[str, JSONValue] = {"report_ids": list(chunk)}
        if run_id is not None:
            kwargs["run_id"] = run_id
        deferrer.defer(**kwargs)
        count += 1
    logger.info(
        "enqueue_embed_reports: deferred %d subjob(s) for %d report(s) at priority=%d",
        count,
        len(report_ids),
        priority,
    )
    return count


def active_backfill_run_ids() -> list[int]:
    """Pks of currently-active `EmbeddingBackfillRun` rows (both end
    timestamps NULL). At most one in steady state (enforced by
    `create_backfill_run`); the check-then-act race window documented
    there can transiently allow more, so callers treat this as a list."""
    return list(
        EmbeddingBackfillRun.objects.filter(
            finished_at__isnull=True, cancelled_at__isnull=True
        ).values_list("pk", flat=True)
    )


def cancel_backfill_embeddings() -> int:
    """Cancel every queued (todo) subjob belonging to an active backfill run.

    Run-scoped, not priority-scoped: `./manage.py retry_stalled_jobs` (run
    at stack start by both compose files) requeues stalled `doing` jobs at
    a fixed priority, which can promote a backfill subjob above
    EMBEDDINGS_BACKFILL_PRIORITY — a priority-based cancel would then miss
    it while `EmbeddingBackfillRun.live_subjob_count()` (run_id-scoped)
    still counts it as live. "The backfill's jobs" are therefore identified
    by the active runs' `run_id` in job args, which survives that
    re-prioritization. Write-path jobs carry no run_id and are never
    cancelled. Cancellation goes job-by-job through Procrastinate's
    cancel_job_by_id, which is race-safe: a job a worker grabbed between
    our select and the cancel returns False and simply runs to completion.
    Returns the number of jobs actually cancelled. Cancelled jobs are
    terminal — continuing the backfill means re-running embed_pending,
    which enqueues the still-NULL reports as fresh subjobs chunked at the
    then-current EMBEDDINGS_SUBJOB_SIZE."""
    run_ids = active_backfill_run_ids()
    job_ids = (
        list(
            ProcrastinateJob.objects.filter(
                task_name="radis.pgsearch.tasks.embed_reports_task",
                queue_name="embeddings",
                status="todo",
                args__run_id__in=run_ids,
            ).values_list("id", flat=True)
        )
        if run_ids
        else []
    )
    cancelled = sum(1 for job_id in job_ids if app.job_manager.cancel_job_by_id(job_id))
    closed_runs = EmbeddingBackfillRun.objects.filter(
        finished_at__isnull=True, cancelled_at__isnull=True
    ).update(cancelled_at=Now())
    logger.info(
        "cancel_backfill_embeddings: cancelled %d of %d queued run-scoped backfill "
        "subjob(s); closed %d run(s)",
        cancelled,
        len(job_ids),
        closed_runs,
    )
    return cancelled


@app.task(queue="embeddings", retry=EMBEDDING_TASK_RETRY_STRATEGY)
def embed_reports_task(report_ids: list[int], run_id: int | None = None) -> None:
    """Embed the named reports.

    Failure handling, from innermost to outermost:

    * Transient errors (connection, timeout, 5xx, malformed responses):
      retried locally inside `_embed_chunk_with_retry`
      (EMBEDDINGS_TRANSIENT_RETRY_ATTEMPTS retries with exponential backoff).
    * Gateway 429s: the per-process EMBEDDING_GATE waits out the
      server-reported pause (or an exponential ladder) up to the batch
      budget, then raises RateLimited.
    * Anything that escapes both propagates so EMBEDDING_TASK_RETRY_STRATEGY
      retries the whole subjob (transient classes only).

    Callers must ensure ReportSearchIndex rows exist before deferring this
    task. `bulk_index_reports` chains the defer at the end of its run, and
    `embed_pending` / the admin action filter on existing ReportSearchIndex rows by
    construction.

    Increments the backfill run's counter on success; failed subjobs never
    increment, so an abandoned run is detectable as `processed < total` with
    no live subjobs.
    """
    if not report_ids:
        return

    # If this subjob belongs to a cancelled run, no-op. A subjob that was already
    # `doing` at cancel time isn't cancellable via cancel_job_by_id; if it then
    # fails and Procrastinate requeues it, its run is no longer active so future
    # cancels can't see it. Bailing here stops the cancelled backfill from
    # silently continuing. Resuming means re-running embed_pending.
    if run_id is not None:
        run = EmbeddingBackfillRun.objects.filter(pk=run_id).first()
        if run is not None and run.cancelled_at is not None:
            logger.info(
                "embed_reports_task: run %d is cancelled; skipping %d report(s) "
                "(re-run embed_pending to resume the remaining work)",
                run_id,
                len(report_ids),
            )
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

    batch_size = settings.EMBEDDINGS_BATCH_SIZE
    embedded: list[ReportSearchIndex] = []
    try:
        with EmbeddingClient() as client:
            for start in range(0, len(rsvs), batch_size):
                chunk = rsvs[start : start + batch_size]
                vectors = _embed_chunk_with_retry(client, [rsv.report.body for rsv in chunk])
                for rsv, vec in zip(chunk, vectors, strict=True):
                    rsv.embedding = vec
                    embedded.append(rsv)
    except PERMANENT_EMBEDDING_ERRORS as exc:
        # Reachable but misconfigured (bad key/permission, wrong model or
        # endpoint). Retrying won't help — it is excluded from the retry set, so
        # this fails fast. Log clearly; the reports stay unembedded (FTS-only)
        # until the config is fixed and embed_pending is re-run.
        logger.error(
            "embed_reports_task: embedding config looks wrong (%s: %s); reports left "
            "unembedded (FTS-only). Check EMBEDDINGS_BASE_URL, EMBEDDINGS_API_KEY and "
            "EMBEDDINGS_MODEL. report_ids=%s",
            type(exc).__name__,
            exc,
            _truncate_ids(report_ids),
        )
        raise
    except EmbeddingClientError as exc:
        logger.error(
            "embed_reports_task: embedding client failure after retries; "
            "report_ids=%s. Will be retried by Procrastinate. Error: %s",
            _truncate_ids(report_ids),
            exc,
        )
        raise

    if embedded:
        # Count genuine NULL->non-NULL transitions BEFORE the write so progress
        # accounting is idempotent under at-least-once reruns: a rerun of this
        # subjob finds the reports already embedded, counts 0, and does not
        # double-increment the run (which could otherwise flip finished_at while
        # distinct reports remain unembedded). Also dedupes overlap with any
        # other subjob that embedded the same report.
        newly_embedded = ReportSearchIndex.objects.filter(
            report_id__in=[rsv.report.pk for rsv in embedded], embedding__isnull=True
        ).count()
        ReportSearchIndex.objects.bulk_update(embedded, fields=["embedding"])

        if run_id is not None:
            if newly_embedded:
                EmbeddingBackfillRun.objects.filter(pk=run_id).update(
                    processed_reports=F("processed_reports") + newly_embedded
                )
            # Flip finished_at exactly once, and never on a cancelled run.
            EmbeddingBackfillRun.objects.filter(
                pk=run_id,
                finished_at__isnull=True,
                cancelled_at__isnull=True,
                processed_reports__gte=F("total_reports"),
            ).update(finished_at=Now())

    duration_ms = int((time.perf_counter() - start_t) * 1000)
    logger.info(
        "embed_reports_task: finished; embedded=%d duration_ms=%d",
        len(embedded),
        duration_ms,
    )
