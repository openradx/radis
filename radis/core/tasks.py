import logging
import traceback

from celery import Task as CeleryTask
from celery import shared_task
from celery.exceptions import Retry
from django.conf import settings
from django.core.mail import send_mail
from django.core.management import call_command
from django.utils import timezone

from radis.accounts.models import User
from radis.core.models import AnalysisJob, AnalysisTask

logger = logging.getLogger(__name__)


@shared_task
def broadcast_mail(subject: str, message: str):
    recipients = []
    for user in User.objects.all():
        if user.email:
            recipients.append(user.email)

    send_mail(subject, message, settings.SUPPORT_EMAIL, recipients)

    logger.info("Successfully sent an Email to %d recipients.", len(recipients))


@shared_task
def backup_db():
    call_command("backup_db")


class ProcessAnalysisTask(CeleryTask):
    analysis_task_class: type[AnalysisTask]

    def run(self, task_id: int) -> None:
        task = self.analysis_task_class.objects.get(id=task_id)
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

        assert task.status == task.Status.PENDING

        # When the first task is going to be processed then the
        # status of the job switches from PENDING to IN_PROGRESS
        if job.status == job.Status.PENDING:
            job.status = job.Status.IN_PROGRESS
            job.started_at = timezone.now()
            job.save()

        assert job.status == job.Status.IN_PROGRESS

        # Prepare the task itself
        task.status = AnalysisTask.Status.IN_PROGRESS
        task.started_at = timezone.now()
        task.save()

        try:
            self.process_task(task)

            # If the overwritten process_task method changes the status of the
            # task itself then we leave it as it is. Otherwise if the status is
            # still in progress we set it to success.
            if task.status == AnalysisTask.Status.IN_PROGRESS:
                task.status = AnalysisTask.Status.SUCCESS
        except Retry as err:
            # Subclasses can raise Retry to indicate that the task should be retried.
            # This must be passed through to the Celery worker.

            # TODO: How do we handle max retries?!
            if task.status != AnalysisTask.Status.PENDING:
                task.status = AnalysisTask.Status.PENDING
            raise err
        except Exception as err:
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


class ProcessAnalysisJob(CeleryTask):
    analysis_job_class: type[AnalysisJob]
    process_analysis_task: ProcessAnalysisTask
    task_queue: str

    def run(self, job_id: int) -> None:
        job = self.analysis_job_class.objects.get(id=job_id)
        logger.info("Start processing job %s", job)
        assert job.status == AnalysisJob.Status.PREPARING

        priority = job.default_priority
        if job.urgent:
            priority = job.urgent_priority

        logger.debug("Collecting tasks for job %s", job)
        tasks: list[AnalysisTask] = []
        for task in self.collect_tasks(job):
            assert task.status == task.Status.PENDING
            tasks.append(task)

        logger.debug("Found %d tasks for job %s", len(tasks), job)

        job.status = AnalysisJob.Status.PENDING
        job.save()

        for task in tasks:
            result = (
                self.process_analysis_task.s(task_id=task.id)
                .set(priority=priority)
                .apply_async(queue=self.task_queue)
            )
            # Save Celery task ID to analysis task (for revoking it later if necessary).
            # Only works when not in eager mode (which is used to debug Celery stuff).
            if not getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
                task.celery_task_id = result.id
                task.save()

    def collect_tasks(self, job: AnalysisJob) -> list[AnalysisTask]:
        """The derived class should collect the tasks to process here."""
        ...
