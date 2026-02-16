from __future__ import annotations

import logging
from itertools import batched

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.reports.models import Report

from .models import LabelBackfillJob, LabelGroup
from .processors import LabelGroupProcessor

logger = logging.getLogger(__name__)


@app.task(queue="llm")
def process_label_group(
    label_group_id: int,
    report_ids: list[int],
    overwrite_existing: bool = False,
    backfill_job_id: int | None = None,
) -> None:
    # If this is part of a backfill, check cancellation status before doing any work
    if backfill_job_id is not None:
        try:
            backfill_job = LabelBackfillJob.objects.get(id=backfill_job_id)
        except LabelBackfillJob.DoesNotExist:
            logger.warning("Backfill job %s not found, skipping batch.", backfill_job_id)
            return

        if backfill_job.status in (
            LabelBackfillJob.Status.CANCELING,
            LabelBackfillJob.Status.CANCELED,
        ):
            logger.info(
                "Backfill job %s is %s, skipping batch.",
                backfill_job,
                backfill_job.get_status_display(),
            )
            _increment_and_maybe_finalize(backfill_job_id, len(report_ids))
            return

    group = LabelGroup.objects.get(id=label_group_id)
    processor = LabelGroupProcessor(group)
    processor.process_reports(report_ids, overwrite_existing=overwrite_existing)

    # After processing, update backfill progress
    if backfill_job_id is not None:
        _increment_and_maybe_finalize(backfill_job_id, len(report_ids))


def _increment_and_maybe_finalize(backfill_job_id: int, count: int) -> None:
    """Atomically increment processed_reports and check for completion."""
    LabelBackfillJob.objects.filter(id=backfill_job_id).update(
        processed_reports=F("processed_reports") + count
    )

    try:
        backfill_job = LabelBackfillJob.objects.get(id=backfill_job_id)
    except LabelBackfillJob.DoesNotExist:
        return

    if backfill_job.processed_reports >= backfill_job.total_reports:
        if backfill_job.status == LabelBackfillJob.Status.CANCELING:
            backfill_job.status = LabelBackfillJob.Status.CANCELED
        elif backfill_job.status == LabelBackfillJob.Status.IN_PROGRESS:
            backfill_job.status = LabelBackfillJob.Status.SUCCESS
        backfill_job.ended_at = timezone.now()
        backfill_job.save()


def enqueue_labeling_for_reports(
    report_ids: list[int],
    groups: list[LabelGroup] | None = None,
    overwrite_existing: bool = False,
) -> None:
    if not report_ids:
        return

    if groups is None:
        active_groups = list(LabelGroup.objects.filter(is_active=True))
    else:
        active_groups = [group for group in groups if group.is_active]
    if not active_groups:
        logger.info("No active label groups found, skipping labeling for reports %s", report_ids)
        return

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    for group in active_groups:
        for report_batch in batched(report_ids, batch_size):
            process_label_group.defer(
                label_group_id=group.id,
                report_ids=list(report_batch),
                overwrite_existing=overwrite_existing,
            )


@app.task
def enqueue_label_group_backfill(label_group_id: int, backfill_job_id: int) -> None:
    group = LabelGroup.objects.get(id=label_group_id)
    if not group.is_active:
        logger.info("Label group %s is inactive. Skipping backfill.", group)
        try:
            backfill_job = LabelBackfillJob.objects.get(id=backfill_job_id)
            backfill_job.status = LabelBackfillJob.Status.CANCELED
            backfill_job.message = "Label group is inactive."
            backfill_job.ended_at = timezone.now()
            backfill_job.save()
        except LabelBackfillJob.DoesNotExist:
            pass
        return

    try:
        backfill_job = LabelBackfillJob.objects.get(id=backfill_job_id)
    except LabelBackfillJob.DoesNotExist:
        logger.warning("Backfill job %s not found, aborting.", backfill_job_id)
        return

    # Count total reports
    total_reports = Report.objects.count()
    backfill_job.status = LabelBackfillJob.Status.IN_PROGRESS
    backfill_job.started_at = timezone.now()
    backfill_job.total_reports = total_reports
    backfill_job.save()

    if total_reports == 0:
        backfill_job.status = LabelBackfillJob.Status.SUCCESS
        backfill_job.message = "No reports to process."
        backfill_job.ended_at = timezone.now()
        backfill_job.save()
        return

    batch_size = settings.LABELING_TASK_BATCH_SIZE
    current_batch: list[int] = []
    report_ids = (
        Report.objects.order_by("id").values_list("id", flat=True).iterator(chunk_size=batch_size)
    )

    for report_id in report_ids:
        current_batch.append(report_id)
        if len(current_batch) >= batch_size:
            process_label_group.defer(
                label_group_id=group.id,
                report_ids=current_batch,
                overwrite_existing=False,
                backfill_job_id=backfill_job.id,
            )
            current_batch = []

    if current_batch:
        process_label_group.defer(
            label_group_id=group.id,
            report_ids=current_batch,
            overwrite_existing=False,
            backfill_job_id=backfill_job.id,
        )
