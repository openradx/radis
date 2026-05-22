import logging
import time

from django.utils import timezone
from procrastinate.contrib.django import app

from radis.core.models import AnalysisJob, AnalysisTask

from .models import LabelingJob, LabelingTask
from .processors import LabelingTaskProcessor
from .services import create_labeling_tasks_streaming, label_reports_in_parallel

logger = logging.getLogger("radis.labels")


@app.task(queue="llm")
def label_report_batch(report_ids: list[int]) -> None:
    started = time.monotonic()
    logger.info("labels.batch.start size=%d", len(report_ids))
    success, failure = label_reports_in_parallel(report_ids)
    duration = time.monotonic() - started
    logger.info(
        "labels.batch.done size=%d success=%d failure=%d duration=%.1fs",
        len(report_ids),
        success,
        failure,
        duration,
    )


@app.task(queue="llm")
def process_labeling_task(task_id: int) -> None:
    task = LabelingTask.objects.get(id=task_id)
    LabelingTaskProcessor(task).start()
    task.queued_job_id = None
    task.save()


@app.task()
def process_labeling_job(job_id: int) -> None:
    job = LabelingJob.objects.get(id=job_id)
    job.tasks.all().delete()  # restart-safe under retry
    job.status = AnalysisJob.Status.PREPARING
    job.started_at = timezone.now()
    job.save()

    create_labeling_tasks_streaming(job)

    job.refresh_from_db()
    if job.status == AnalysisJob.Status.CANCELING:
        # PREPARING was cancelled mid-stream. Tear down partially-created tasks
        # and finalize the job to CANCELED so the singleton index is released.
        job.tasks.all().delete()
        job.status = AnalysisJob.Status.CANCELED
        job.ended_at = timezone.now()
        job.save()
        return

    job.status = AnalysisJob.Status.PENDING
    job.save()

    enqueue_all_pending_tasks(job)


def enqueue_all_pending_tasks(job: LabelingJob) -> None:
    pending = job.tasks.filter(status=AnalysisTask.Status.PENDING)
    deferrer = app.configure_task(
        "radis.labels.tasks.process_labeling_task",
        allow_unknown=False,
        priority=job.default_priority,
    )
    for task in pending.iterator(chunk_size=500):
        queued_job_id = deferrer.defer(task_id=task.pk)
        task.queued_job_id = queued_job_id
        task.save(update_fields=["queued_job_id"])
