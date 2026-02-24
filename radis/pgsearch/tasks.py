from __future__ import annotations

import logging
from itertools import batched

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.reports.models import Report

from .models import EmbeddingBackfillJob, ReportSearchVector
from .utils.embedding_client import EmbeddingClient, is_embedding_available

logger = logging.getLogger(__name__)


@app.task(queue="llm")
def generate_report_embedding(report_id: int) -> None:
    """Generate embedding for a single report."""
    if not is_embedding_available():
        return

    try:
        report = Report.objects.get(id=report_id)
    except Report.DoesNotExist:
        logger.warning("Report %s not found, skipping embedding generation.", report_id)
        return

    try:
        client = EmbeddingClient()
        embedding = client.embed_single(report.body)
    except Exception:
        logger.exception("Failed to generate embedding for report %s.", report_id)
        return

    ReportSearchVector.objects.filter(report=report).update(embedding=embedding)


@app.task(queue="llm")
def process_embedding_batch(
    report_ids: list[int],
    backfill_job_id: int | None = None,
) -> None:
    """Generate embeddings for a batch of reports."""
    if backfill_job_id is not None:
        try:
            backfill_job = EmbeddingBackfillJob.objects.get(id=backfill_job_id)
        except EmbeddingBackfillJob.DoesNotExist:
            logger.warning("Backfill job %s not found, skipping batch.", backfill_job_id)
            return

        if backfill_job.status in (
            EmbeddingBackfillJob.Status.CANCELING,
            EmbeddingBackfillJob.Status.CANCELED,
        ):
            logger.info(
                "Backfill job %s is %s, skipping batch.",
                backfill_job,
                backfill_job.get_status_display(),
            )
            _increment_and_maybe_finalize(backfill_job_id, len(report_ids))
            return

    reports = list(
        Report.objects.filter(id__in=report_ids).values_list("id", "body").order_by("id")
    )

    if not reports:
        if backfill_job_id is not None:
            _increment_and_maybe_finalize(backfill_job_id, len(report_ids))
        return

    try:
        client = EmbeddingClient()
        texts = [body for _, body in reports]
        embeddings = client.embed(texts)
    except Exception:
        logger.exception("Failed to generate embeddings for batch of %d reports.", len(reports))
        if backfill_job_id is not None:
            _increment_and_maybe_finalize(backfill_job_id, len(report_ids))
        return

    for (report_id, _), embedding in zip(reports, embeddings):
        ReportSearchVector.objects.filter(report_id=report_id).update(embedding=embedding)

    if backfill_job_id is not None:
        _increment_and_maybe_finalize(backfill_job_id, len(report_ids))


def _increment_and_maybe_finalize(backfill_job_id: int, count: int) -> None:
    """Atomically increment processed_reports and check for completion."""
    EmbeddingBackfillJob.objects.filter(id=backfill_job_id).update(
        processed_reports=F("processed_reports") + count
    )

    try:
        backfill_job = EmbeddingBackfillJob.objects.get(id=backfill_job_id)
    except EmbeddingBackfillJob.DoesNotExist:
        return

    if backfill_job.processed_reports >= backfill_job.total_reports:
        if backfill_job.status == EmbeddingBackfillJob.Status.CANCELING:
            backfill_job.status = EmbeddingBackfillJob.Status.CANCELED
        elif backfill_job.status == EmbeddingBackfillJob.Status.IN_PROGRESS:
            backfill_job.status = EmbeddingBackfillJob.Status.SUCCESS
        backfill_job.ended_at = timezone.now()
        backfill_job.save()


@app.task
def enqueue_embedding_backfill(backfill_job_id: int, force: bool = False) -> None:
    """Batch-enqueue embedding generation for all reports missing embeddings."""
    try:
        backfill_job = EmbeddingBackfillJob.objects.get(id=backfill_job_id)
    except EmbeddingBackfillJob.DoesNotExist:
        logger.warning("Backfill job %s not found, aborting.", backfill_job_id)
        return

    if force:
        report_qs = Report.objects.all()
    else:
        report_qs = Report.objects.filter(search_vector__embedding__isnull=True)

    total_reports = report_qs.count()
    backfill_job.status = EmbeddingBackfillJob.Status.IN_PROGRESS
    backfill_job.started_at = timezone.now()
    backfill_job.total_reports = total_reports
    backfill_job.save()

    if total_reports == 0:
        backfill_job.status = EmbeddingBackfillJob.Status.SUCCESS
        backfill_job.message = "No reports to process."
        backfill_job.ended_at = timezone.now()
        backfill_job.save()
        return

    batch_size = settings.EMBEDDING_BACKFILL_TASK_BATCH_SIZE
    report_ids = (
        report_qs.order_by("id").values_list("id", flat=True).iterator(chunk_size=batch_size)
    )

    for report_batch in batched(report_ids, batch_size):
        process_embedding_batch.defer(
            report_ids=list(report_batch),
            backfill_job_id=backfill_job.id,
        )
