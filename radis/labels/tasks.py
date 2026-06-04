import logging
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report

from .models import Label, LabelGroup, LabelingJob, LabelingScanCheckpoint, LabelingTask
from .scope import _needs_work_queryset

logger = logging.getLogger(__name__)


def _scope_queryset(job: LabelingJob) -> QuerySet:
    if job.scan_from is not None:  # SCAN job: recent window
        return Report.objects.filter(created_at__gte=job.scan_from).order_by("pk")
    active_group_count = LabelGroup.objects.filter(labels__active=True).distinct().count()
    return _needs_work_queryset(active_group_count).order_by("pk")


def _flush_task(job: LabelingJob, report_ids: list[int]) -> None:
    task = LabelingTask.objects.create(job=job, status=AnalysisTask.Status.PENDING)
    task.reports.add(*report_ids)


def _create_labeling_tasks_streaming(job: LabelingJob) -> None:
    batch: list[int] = []
    for report_id in (
        _scope_queryset(job)
        .values_list("pk", flat=True)
        .iterator(chunk_size=settings.LABELING_TASK_BATCH_SIZE)
    ):
        batch.append(report_id)
        if len(batch) >= settings.LABELING_TASK_BATCH_SIZE:
            _flush_task(job, batch)
            batch = []
    if batch:
        _flush_task(job, batch)


@app.task
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)

    # Valid entry states are PENDING (fresh defer) and PREPARING (retry after a crash mid-prep).
    # Bail on any other state so a spurious re-fire on an already-running or finished job cannot
    # wipe its in-flight tasks via the delete-at-top below.
    if job.status not in (AnalysisJob.Status.PENDING, AnalysisJob.Status.PREPARING):
        logger.warning(
            "process_labeling_job called for job %s in status %s; ignoring.", job.pk, job.status
        )
        return

    logger.info("Preparing labeling job %s (trigger=%s).", job.pk, job.trigger)
    job.tasks.all().delete()  # wipe partial rows from any prior crashed attempt (idempotent)
    job.status = AnalysisJob.Status.PREPARING
    if not job.started_at:  # preserve the original start time across retries
        job.started_at = timezone.now()
    job.save()

    _create_labeling_tasks_streaming(job)

    job.status = AnalysisJob.Status.PENDING
    job.queued_job_id = None
    job.save()

    # Only now (PENDING) may tasks be enqueued.
    for task in job.tasks.filter(status=AnalysisTask.Status.PENDING):
        if not task.is_queued:
            task.delay()


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    from .processors import LabelingTaskProcessor

    task = LabelingTask.objects.get(id=task_id)
    LabelingTaskProcessor(task).start()

    task = LabelingTask.objects.get(id=task_id)
    task.queued_job_id = None
    task.save()


@app.periodic(cron=settings.LABELING_SCAN_CRON)
@app.task()
def incremental_label_scan(timestamp: int) -> None:
    # Procrastinate passes the scheduled tick time; use it so the scan window tracks the cron.
    now = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
    checkpoint, _ = LabelingScanCheckpoint.objects.get_or_create(pk=1)

    if LabelingJob.objects.filter(status__in=LabelingJob.ACTIVE_STATUSES).exists():
        logger.info("Active LabelingJob found, skipping scan tick (checkpoint unchanged).")
        return

    if checkpoint.last_scanned_at is None:
        checkpoint.last_scanned_at = now  # first run: existing reports belong to a manual backfill
        checkpoint.save()
        return

    if not Label.objects.filter(active=True).exists():
        # No labels to apply. Return WITHOUT advancing so the checkpoint stays frozen; when labels
        # are (re)activated, the next tick's scan_from still covers everything ingested meanwhile.
        return

    if Report.objects.filter(created_at__gte=checkpoint.last_scanned_at).exists():
        job = LabelingJob.objects.create(
            trigger=LabelingJob.Trigger.SCAN,
            scan_from=checkpoint.last_scanned_at,
            status=AnalysisJob.Status.PENDING,
        )
        job.delay()
        logger.info("Created scan LabelingJob %s (scan_from=%s).", job.pk, job.scan_from)

    checkpoint.last_scanned_at = now
    checkpoint.save()
