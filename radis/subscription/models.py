from datetime import datetime
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
    name = models.CharField(max_length=100)
    owner_id: int
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(app_label)s"
    )
    group = models.ForeignKey(Group, on_delete=models.CASCADE)

    provider = models.CharField(max_length=100)
    query = models.CharField(max_length=200)

    language = models.ForeignKey(Language, on_delete=models.CASCADE, blank=True, null=True)
    modalities = models.ManyToManyField(Modality, blank=True)
    study_date_from = models.DateField(null=True, blank=True)
    study_date_till = models.DateField(null=True, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1, blank=True, choices=[("", "All"), ("M", "Male"), ("F", "Female")]
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)
    patient_id = models.CharField(max_length=100, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_refreshed = models.DateTimeField(auto_now_add=True)

    if TYPE_CHECKING:
        items = RelatedManager["InboxItem"]()

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["name", "owner_id"],
                name="unique_subscription_name_per_user",
            )
        ]


class InboxItem(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE)
    report = models.ForeignKey(Report, on_delete=models.CASCADE)


class SubscriptionJob(AnalysisJob):
    default_priority = settings.SUBSCRIPTION_DEFAULT_PRIORITY
    urgent_priority = settings.SUBSCRIPTION_URGENT_PRIORITY
    continuous_job = False

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.CASCADE, related_name="refresh_jobs"
    )

    if TYPE_CHECKING:
        tasks = RelatedManager["SubscriptionTask"]()

    def __str__(self) -> str:
        return f"SubscriptionJob {self.id}"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscription.tasks.process_subscription_job",
            allow_unknown=False,
            priority=self.urgent_priority if self.urgent else self.default_priority,
        ).defer(job_id=self.id)
        self.queued_job_id = queued_job_id
        self.save()


class SubscriptionTask(AnalysisTask):
    job = models.ForeignKey(SubscriptionJob, on_delete=models.CASCADE)
    reports = models.ManyToManyField(Report, blank=True)

    def __str__(self) -> str:
        return f"SubscriptionTask {self.id} for {self.job.subscription}"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.subscription.tasks.process_subscription_task",
            allow_unknown=False,
            priority=self.job.urgent_priority if self.job.urgent else self.job.default_priority,
        ).defer(task_id=self.id)
        self.queued_job_id = queued_job_id
        self.save()
