from adit_radis_shared.accounts.models import Group, User
from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.db import models
from django.db.models.constraints import UniqueConstraint
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.extractions.models import OutputField
from radis.reports.models import Language, Modality, Report


class SubscriptionsAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Subscriptions app settings"


class PatientSexFilter(models.TextChoices):
    ALL = "", "All"
    MALE = "M", "Male"
    FEMALE = "F", "Female"


class Subscription(models.Model):
    name = models.CharField(max_length=100)
    owner_id: int
    owner = models.ForeignKey[User](
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions"
    )

    group = models.ForeignKey[Group](Group, on_delete=models.CASCADE, related_name="+")
    patient_id = models.CharField(max_length=100, blank=True)
    language = models.ForeignKey[Language](
        Language, on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    modalities = models.ManyToManyField(Modality, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1,
        blank=True,
        choices=PatientSexFilter.choices,
        default=PatientSexFilter.ALL,
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_refreshed = models.DateTimeField(auto_now_add=True)
    last_viewed_at = models.DateTimeField(null=True, blank=True)

    filter_questions: models.QuerySet["FilterQuestion"]
    output_fields: models.QuerySet[OutputField]
    items: models.QuerySet["SubscribedItem"]

    send_finished_mail = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]  # Most recent first
        constraints = [
            UniqueConstraint(
                fields=["name", "owner_id"],
                name="unique_subscription_name_per_user",
            )
        ]

    def __str__(self):
        return f"Subscription {self.name} [{self.pk}]"


class FilterQuestion(models.Model):
    class ExpectedAnswer(models.TextChoices):
        YES = "Y", "Yes"
        NO = "N", "No"

    subscription = models.ForeignKey[Subscription](
        Subscription, on_delete=models.CASCADE, related_name="filter_questions"
    )
    question = models.CharField(max_length=300)
    expected_answer = models.CharField(
        max_length=1,
        choices=ExpectedAnswer.choices,
        default=ExpectedAnswer.YES,
    )

    def __str__(self) -> str:
        max_length = 30
        truncated = self.question[:max_length]
        suffix = "..." if len(self.question) > max_length else ""
        return f'Filter Question "{truncated}{suffix}" [{self.pk}]'

    @property
    def expected_answer_bool(self) -> bool:
        return self.expected_answer == self.ExpectedAnswer.YES


class SubscribedItem(models.Model):
    subscription = models.ForeignKey[Subscription](
        Subscription, on_delete=models.CASCADE, related_name="items"
    )
    job = models.ForeignKey(
        "SubscriptionJob", null=True, on_delete=models.SET_NULL, related_name="items"
    )
    report = models.ForeignKey[Report](Report, on_delete=models.CASCADE, related_name="+")
    filter_results = models.JSONField(null=True, blank=True)
    extraction_results = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]  # Most recent first

    def __str__(self):
        return f"SubscribedItem of {self.subscription} [{self.pk}]"


class SubscriptionJob(AnalysisJob):
    default_priority = settings.SUBSCRIPTION_DEFAULT_PRIORITY
    urgent_priority = settings.SUBSCRIPTION_URGENT_PRIORITY
    finished_mail_template = "subscriptions/subscription_job_finished_mail.html"

    queued_job_id: int | None
    queued_job = models.OneToOneField[ProcrastinateJob](
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subscription = models.ForeignKey[Subscription](
        Subscription, on_delete=models.CASCADE, related_name="jobs"
    )

    tasks: models.QuerySet["SubscriptionTask"]
    items: models.QuerySet[SubscribedItem]

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
    job = models.ForeignKey[SubscriptionJob](
        SubscriptionJob, on_delete=models.CASCADE, related_name="tasks"
    )
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
