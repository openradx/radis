import logging

from django.conf import settings
from django.db.models import QuerySet
from django.utils import timezone
from procrastinate.contrib.django import app

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report

from .models import LabelGroup, LabelingJob, LabelingTask
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
