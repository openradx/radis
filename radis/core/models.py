import logging
from typing import Callable

from django.conf import settings
from django.core.mail import send_mail
from django.db import models
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.utils.model_utils import reset_tasks

logger = logging.getLogger(__name__)


class AnalysisJob(models.Model):
    class Status(models.TextChoices):
        UNVERIFIED = "UV", "Unverified"
        PREPARING = "PR", "Preparing"
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        CANCELING = "CI", "Canceling"
        CANCELED = "CA", "Canceled"
        SUCCESS = "SU", "Success"
        WARNING = "WA", "Warning"
        FAILURE = "FA", "Failure"

    default_priority: int
    urgent_priority: int

    status = models.CharField(max_length=2, choices=Status.choices, default=Status.UNVERIFIED)
    get_status_display: Callable[[], str]
    owner_id: int
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="%(app_label)s_jobs",
    )
    urgent = models.BooleanField(default=False)
    send_finished_mail = models.BooleanField(default=False)
    finished_mail_template: str | None
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    tasks: models.QuerySet["AnalysisTask"]

    class Meta:
        abstract = True
        indexes = [models.Index(fields=["owner", "status"])]
        permissions = [
            (
                "can_analyze_urgently",
                "Can analyze urgently",
            )
        ]

    def __str__(self) -> str:
        return f"{self.__class__.__name__} [{self.pk}]"

    def get_absolute_url(self) -> str: ...

    def delay(self) -> None: ...

    def reset_tasks(self, only_failed=False) -> models.QuerySet["AnalysisTask"]:
        if only_failed:
            tasks = self.tasks.filter(status=AnalysisTask.Status.FAILURE)
        else:
            tasks = self.tasks.all()

        reset_tasks(tasks)

        return tasks

    def update_job_state(self) -> bool:
        """Evaluates all the tasks of this job and sets the job state accordingly.

        Should be called whenever task are updated or manipulated.

        Returns: True if the job (for now) has no more pending tasks left. If it
            is a continuous job there could be added new tasks later on.
        """

        if not self.tasks.exists():
            self.status = AnalysisJob.Status.CANCELED
            self.message = "No tasks remaining."
            self.save()
            return False

        if self.tasks.filter(status=AnalysisTask.Status.PENDING).exists():
            if self.status != AnalysisJob.Status.CANCELING:
                self.status = AnalysisJob.Status.PENDING
                self.save()
            return False

        if self.tasks.filter(status=AnalysisTask.Status.IN_PROGRESS).exists():
            if self.status != AnalysisJob.Status.CANCELING:
                self.status = AnalysisJob.Status.IN_PROGRESS
                self.save()
            return False

        if self.status == AnalysisJob.Status.CANCELING:
            self.status = AnalysisJob.Status.CANCELED
            self.save()
            return False

        # Job is finished and we evaluate its final status
        has_success = self.tasks.filter(status=AnalysisTask.Status.SUCCESS).exists()
        has_warning = self.tasks.filter(status=AnalysisTask.Status.WARNING).exists()
        has_failure = self.tasks.filter(status=AnalysisTask.Status.FAILURE).exists()

        if has_success and not has_warning and not has_failure:
            self.status = AnalysisJob.Status.SUCCESS
            self.message = "All tasks succeeded."
        elif (has_success and has_failure) or (has_warning and has_failure):
            self.status = AnalysisJob.Status.FAILURE
            self.message = "Some tasks failed."
        elif has_success and has_warning:
            self.status = AnalysisJob.Status.WARNING
            self.message = "Some tasks have warnings."
        elif has_warning:
            self.status = AnalysisJob.Status.WARNING
            self.message = "All tasks have warnings."
        elif has_failure:
            self.status = AnalysisJob.Status.FAILURE
            self.message = "All tasks failed."
        else:
            # at least one of success, warnings or failures must be > 0
            raise AssertionError(f"Invalid task status of {self}.")

        self.ended_at = timezone.now()
        self.save()

        if self.send_finished_mail:
            self._send_job_finished_mail()

        return True

    def _send_job_finished_mail(self) -> None:
        if not self.finished_mail_template:
            raise ValueError("No finished mail template for job %s", self)

        logger.info("Sending finished mail for job %s", self)
        html_content = render_to_string(
            self.finished_mail_template,
            {
                "job": self,
                **self.get_mail_context(),
            },
        )
        plain_content = strip_tags(html_content)
        send_mail(
            f"Job {self} finished",
            plain_content,
            settings.DEFAULT_FROM_EMAIL,
            [self.owner.email],
            html_message=html_content,
        )
        logger.info("Finished mail for job %s sent", self)

    def get_mail_context(self) -> dict:
        return {}

    @property
    def is_preparing(self) -> bool:
        return self.status == self.Status.PREPARING

    @property
    def is_deletable(self) -> bool:
        non_pending_tasks = self.tasks.exclude(status=AnalysisTask.Status.PENDING)
        return (
            self.status
            in [
                self.Status.UNVERIFIED,
                self.Status.PREPARING,
                self.Status.PENDING,
            ]
            and non_pending_tasks.count() == 0
        )

    @property
    def is_verified(self) -> bool:
        return self.status != self.Status.UNVERIFIED

    @property
    def is_cancelable(self) -> bool:
        return self.status in [
            self.Status.PENDING,
            self.Status.PREPARING,
            self.Status.IN_PROGRESS,
        ]

    @property
    def is_resumable(self) -> bool:
        return self.status == self.Status.CANCELED

    @property
    def is_retriable(self) -> bool:
        return self.status == self.Status.FAILURE

    @property
    def is_restartable(self) -> bool:
        return self.status in [
            self.Status.CANCELED,
            self.Status.SUCCESS,
            self.Status.WARNING,
            self.Status.FAILURE,
        ]

    @property
    def processed_tasks(self) -> models.QuerySet["AnalysisTask"]:
        non_processed = (
            AnalysisTask.Status.PENDING,
            AnalysisTask.Status.IN_PROGRESS,
        )
        return self.tasks.exclude(status__in=non_processed)


class AnalysisTask(models.Model):
    class Status(models.TextChoices):
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        CANCELED = "CA", "Canceled"
        SUCCESS = "SU", "Success"
        WARNING = "WA", "Warning"
        FAILURE = "FA", "Failure"

    job_id: int
    job = models.ForeignKey(AnalysisJob, on_delete=models.CASCADE, related_name="tasks")
    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    status = models.CharField(
        max_length=2,
        choices=Status.choices,
        default=Status.PENDING,
    )
    get_status_display: Callable[[], str]
    attempts = models.PositiveSmallIntegerField(default=0)
    message = models.TextField(blank=True, default="")
    log = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.__class__.__name__} [{self.pk}]"

    def get_absolute_url(self) -> str: ...

    def delay(self) -> None: ...

    @property
    def is_queued(self) -> bool:
        return self.queued_job_id is not None

    @property
    def is_deletable(self) -> bool:
        return self.status == self.Status.PENDING

    @property
    def is_resettable(self) -> bool:
        return self.status in [
            self.Status.CANCELED,
            self.Status.SUCCESS,
            self.Status.WARNING,
            self.Status.FAILURE,
        ]
