from typing import TYPE_CHECKING

from adit_radis_shared.accounts.models import Group
from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.db import models
from django.db.models.constraints import UniqueConstraint
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Language, Modality, Report

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


class SubscriptionAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Subscription app settings"


class Subscription(models.Model):
    id: int
    name = models.CharField(max_length=100)
    owner_id: int
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )

    provider = models.CharField(max_length=100)
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="+")
    patient_id = models.CharField(max_length=100, blank=True)
    query = models.CharField(max_length=200, blank=True)
    language = models.ForeignKey(
        Language, on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    modalities = models.ManyToManyField(Modality, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1, blank=True, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_refreshed = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        items = RelatedManager["SubscribedItem"]()

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["name", "owner_id"],
                name="unique_subscription_name_per_user",
            )
        ]

    def __str__(self):
        return f"Subscription {self.id} [{self.name}]"


class SubscribedItem(models.Model):
    id: int
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="items")
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="+")

    def __str__(self):
        return f"SubscribedItem {self.id} [Subscription {self.subscription.id}]"


class SubscriptionJob(AnalysisJob):
    default_priority = settings.SUBSCRIPTION_DEFAULT_PRIORITY
    urgent_priority = settings.SUBSCRIPTION_URGENT_PRIORITY
    continuous_job = False

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="jobs")

    if TYPE_CHECKING:
        tasks = RelatedManager["SubscriptionTask"]()

    def __str__(self) -> str:
        return f"SubscriptionJob {self.id}"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscriptions.tasks.process_subscription_job",
            allow_unknown=False,
            priority=self.urgent_priority if self.urgent else self.default_priority,
        ).defer(job_id=self.id)
        self.queued_job_id = queued_job_id
        self.save()


class SubscriptionTask(AnalysisTask):
    job = models.ForeignKey(SubscriptionJob, on_delete=models.CASCADE, related_name="tasks")
    reports = models.ManyToManyField(Report, blank=True)

    def __str__(self) -> str:
        return f"SubscriptionTask {self.id} for {self.job.subscription}"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscriptions.tasks.process_subscription_task",
            allow_unknown=False,
            priority=self.job.urgent_priority if self.job.urgent else self.job.default_priority,
        ).defer(task_id=self.id)
        self.queued_job_id = queued_job_id
        self.save()
