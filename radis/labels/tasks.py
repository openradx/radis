from __future__ import annotations

import logging
from itertools import batched

from django.conf import settings
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
            _maybe_finalize(backfill_job_id)
            return

    group = LabelGroup.objects.get(id=label_group_id)
    processor = LabelGroupProcessor(group)
    processor.process_reports(report_ids, overwrite_existing=overwrite_existing)

    # After processing, check whether the backfill is now complete.
    if backfill_job_id is not None:
        _maybe_finalize(backfill_job_id)


def _maybe_finalize(backfill_job_id: int) -> None:
    """Finalize the backfill if no work remains.

    Progress is derived from the actual ``ReportLabel`` rows rather than from
    a hand-maintained counter, so this check is correct even when batches
    crash, retry, or when reports are deleted mid-backfill.

    The terminal status is written via a conditional ``UPDATE`` so a concurrent
    cancel cannot be silently overwritten.
    """
    try:
        backfill_job = LabelBackfillJob.objects.get(id=backfill_job_id)
    except LabelBackfillJob.DoesNotExist:
        return

    if backfill_job.status not in (
        LabelBackfillJob.Status.IN_PROGRESS,
        LabelBackfillJob.Status.CANCELING,
    ):
        return

    if backfill_job.label_group.missing_reports().exists():
        return  # Still work outstanding for this group.

    # Two precise conditional UPDATEs so a concurrent cancel cannot be
    # silently overwritten: the WHERE clause picks the right branch at SQL
    # time rather than relying on the (potentially stale) in-memory status.
    now = timezone.now()
    LabelBackfillJob.objects.filter(
        id=backfill_job_id,
        status=LabelBackfillJob.Status.IN_PROGRESS,
    ).update(
        status=LabelBackfillJob.Status.SUCCESS,
        ended_at=now,
        processed_reports=backfill_job.total_reports,
    )
    LabelBackfillJob.objects.filter(
        id=backfill_job_id,
        status=LabelBackfillJob.Status.CANCELING,
    ).update(
        status=LabelBackfillJob.Status.CANCELED,
        ended_at=now,
        processed_reports=backfill_job.total_reports,
    )


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

    # Snapshot total for display continuity. The live "remaining" count is
    # computed on demand by ``LabelGroup.missing_reports`` and is what the
    # finalization check uses.
    total_reports = Report.objects.count()
    backfill_job.status = LabelBackfillJob.Status.IN_PROGRESS
    backfill_job.started_at = timezone.now()
    backfill_job.ended_at = None
    backfill_job.message = ""
    backfill_job.total_reports = total_reports
    backfill_job.processed_reports = 0
    backfill_job.save()

    if total_reports == 0:
        backfill_job.status = LabelBackfillJob.Status.SUCCESS
        backfill_job.message = "No reports to process."
        backfill_job.ended_at = timezone.now()
        backfill_job.save()
        return

    # Only dispatch reports that don't already have labels for every active
    # question. This makes the backfill naturally resumable: a retry skips
    # everything that's already done and only processes what's missing.
    batch_size = settings.LABELING_TASK_BATCH_SIZE
    current_batch: list[int] = []
    missing_iter = (
        group.missing_reports()
        .order_by("id")
        .values_list("id", flat=True)
        .iterator(chunk_size=batch_size)
    )

    dispatched_any = False
    for report_id in missing_iter:
        current_batch.append(report_id)
        if len(current_batch) >= batch_size:
            process_label_group.defer(
                label_group_id=group.id,
                report_ids=current_batch,
                overwrite_existing=False,
                backfill_job_id=backfill_job.id,
            )
            dispatched_any = True
            current_batch = []

    if current_batch:
        process_label_group.defer(
            label_group_id=group.id,
            report_ids=current_batch,
            overwrite_existing=False,
            backfill_job_id=backfill_job.id,
        )
        dispatched_any = True

    if not dispatched_any:
        # Every report is already labelled — finalize without dispatching work.
        backfill_job.status = LabelBackfillJob.Status.SUCCESS
        backfill_job.message = "All reports already labelled."
        backfill_job.ended_at = timezone.now()
        backfill_job.processed_reports = total_reports
        backfill_job.save()
