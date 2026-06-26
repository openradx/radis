from typing import TYPE_CHECKING

from django.db import models
from procrastinate.contrib.django import app

if TYPE_CHECKING:
    from ..models import AnalysisJob, AnalysisTask


def reset_tasks(tasks: models.QuerySet["AnalysisTask"]) -> None:
    tasks.update(
        status=tasks.model.Status.PENDING,
        queued_job_id=None,
        attempts=0,
        message="",
        log="",
        started_at=None,
        ended_at=None,
    )


def cancel_job(job: "AnalysisJob") -> None:
    """Cancel a job: revoke pending tasks' queued jobs and mark them canceled.

    A task already in progress is left to finish its batch; the job then settles
    to CANCELED via update_job_state. The job moves to CANCELING while any task is
    still running, else straight to CANCELED.
    """
    task_status = job.tasks.model.Status
    pending = job.tasks.filter(status=task_status.PENDING)
    for task in pending.only("queued_job_id"):
        if task.queued_job_id is not None:
            queued_job_id = task.queued_job_id
            task.queued_job_id = None
            task.save(update_fields=["queued_job_id"])
            app.job_manager.cancel_job_by_id(queued_job_id, delete_job=True)
    pending.update(status=task_status.CANCELED)

    if job.tasks.filter(status=task_status.IN_PROGRESS).exists():
        job.status = job.Status.CANCELING
    else:
        job.status = job.Status.CANCELED
    job.save()
