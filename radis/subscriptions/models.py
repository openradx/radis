from typing import Callable

from adit_radis_shared.accounts.models import Group
from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.db import models
from django.db.models.constraints import UniqueConstraint
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.core.validators import validate_patient_sex
from radis.reports.models import Language, Modality, Report


class SubscriptionAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Subscription app settings"


class Subscription(models.Model):
    name = models.CharField(max_length=100)
    owner_id: int
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )

    provider = models.CharField(max_length=100, blank=True)
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="+")
    patient_id = models.CharField(max_length=100, blank=True)
    query = models.CharField(max_length=200, blank=True)
    language = models.ForeignKey(
        Language, on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    modalities = models.ManyToManyField(Modality, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1,
        blank=True,
        choices=[("", "All"), ("M", "Male"), ("F", "Female")],
        validators=[validate_patient_sex],
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_refreshed = models.DateTimeField(auto_now_add=True)

    items: models.QuerySet["SubscribedItem"]
    questions: models.QuerySet["SubscriptionQuestion"]

    send_finished_mail = models.BooleanField(default=False)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["name", "owner_id"],
                name="unique_subscription_name_per_user",
            )
        ]

    def __str__(self):
        return f"Subscription {self.name} [{self.pk}]"


class Answer(models.TextChoices):
    YES = "Y", "Yes"
    NO = "N", "No"


class SubscriptionQuestion(models.Model):
    question = models.CharField(max_length=500)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="questions"
    )
    accepted_answer = models.CharField(max_length=1, choices=Answer.choices, default=Answer.YES)
    get_accepted_answer_display: Callable[[], str]

    def __str__(self) -> str:
        return f'Question "{self.question}" [{self.pk}]'


class RagResult(models.TextChoices):
    ACCEPTED = "A", "Accepted"
    REJECTED = "R", "Rejected"


class SubscribedItem(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="items")
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"SubscribedItem of {self.subscription} [{self.pk}]"


class SubscriptionJob(AnalysisJob):
    default_priority = settings.SUBSCRIPTION_DEFAULT_PRIORITY
    urgent_priority = settings.SUBSCRIPTION_URGENT_PRIORITY
    continuous_job = False
    finished_mail_template = "subscriptions/mail/finished_mail.html"

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="jobs")

    tasks: models.QuerySet["SubscriptionTask"]

    def get_absolute_url(self) -> str:
        return reverse("subscription_job_detail", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscriptions.tasks.process_subscription_job",
            allow_unknown=False,
            priority=self.urgent_priority if self.urgent else self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()

    def get_mail_context(self) -> dict:
        return {
            "subscription": self.subscription,
            "new_items": self.subscription.items.filter(created_at__gte=self.started_at),
        }


class SubscriptionTask(AnalysisTask):
    job = models.ForeignKey(SubscriptionJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, blank=True)

    def __str__(self) -> str:
        return f"SubscriptionTask of {self.job.subscription} [{self.pk}]"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscriptions.tasks.process_subscription_task",
            allow_unknown=False,
            priority=self.job.urgent_priority if self.job.urgent else self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
