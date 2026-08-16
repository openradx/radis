import logging
import traceback

from django.utils import timezone

from .models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)


class AnalysisTaskProcessor:
    def __init__(self, task: AnalysisTask) -> None:
        self.task = task

    def start(self) -> None:
        task = self.task
        job = task.job

        logger.info("Start processing task %s", task)

        # Jobs are canceled by the AnalysisJobCancelView and tasks are also revoked there,
        # but it could happen that the task was already picked up by a worker or under rare
        # circumstances will nevertheless get picked up by a worker (e.g. the worker crashes
        # and forgot its revoked tasks). We then just ignore that task.
        if (
            job.status == AnalysisJob.Status.CANCELING
            or job.status == AnalysisJob.Status.CANCELED
            or task.status == AnalysisTask.Status.CANCELED
        ):
            task.status = task.Status.CANCELED
            task.started_at = timezone.now()
            task.ended_at = timezone.now()
            task.save()
            job.update_job_state()
            return

        # Claim the task in one UPDATE: it succeeds only while the task is still PENDING, so
        # if the same task is delivered twice only the first delivery runs it. A task left
        # IN_PROGRESS by a killed worker is reset by the sweep in recovery.py, not here.
        now = timezone.now()
        task_model = type(task)
        claimed = task_model.objects.filter(pk=task.pk, status=AnalysisTask.Status.PENDING).update(
            status=AnalysisTask.Status.IN_PROGRESS, started_at=now
        )
        if not claimed:
            logger.warning("Task %s was not PENDING, skipping.", task)
            return
        # Keep the in-memory task in sync with the UPDATE above.
        task.status = AnalysisTask.Status.IN_PROGRESS
        task.started_at = now

        # When the first task is going to be processed then the
        # status of the job switches from PENDING to IN_PROGRESS
        if job.status == job.Status.PENDING:
            job.status = job.Status.IN_PROGRESS
            job.started_at = timezone.now()
            job.save()

        assert job.status == job.Status.IN_PROGRESS

        try:
            self.process_task(task)

            # If the overwritten process_task method changes the status of the
            # task itself then we leave it as it is. Otherwise if the status is
            # still in progress we set it to success.
            if task.status == AnalysisTask.Status.IN_PROGRESS:
                task.status = AnalysisTask.Status.SUCCESS
        except Exception as err:
            logger.exception("Task %s failed.", task)

            task.status = AnalysisTask.Status.FAILURE
            task.message = str(err)
            if task.log:
                task.log += "\n---\n"
            task.log += traceback.format_exc()
        finally:
            logger.info("Task %s ended", task)
            task.ended_at = timezone.now()
            task.save()
            job.update_job_state()

    def process_task(self, task: AnalysisTask) -> None:
        """The derived class should process the task here."""
        ...
