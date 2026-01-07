from typing import Callable

from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.db.models import Q
from django.urls import reverse
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Language, Modality, Report


class ExtractionsAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Extractions app settings"


class PatientSexFilter(models.TextChoices):
    ALL = "", "All"
    MALE = "M", "Male"
    FEMALE = "F", "Female"


class ExtractionJob(AnalysisJob):
    default_priority = settings.EXTRACTION_DEFAULT_PRIORITY
    urgent_priority = settings.EXTRACTION_URGENT_PRIORITY
    finished_mail_template = "extractions/extraction_job_finished_mail.html"

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    title = models.CharField(max_length=100)
    group = models.ForeignKey[Group](Group, on_delete=models.CASCADE)
    query = models.CharField(max_length=200)
    language = models.ForeignKey[Language](Language, on_delete=models.CASCADE)
    modalities = models.ManyToManyField(Modality, blank=True)
    study_date_from = models.DateField(null=True, blank=True)
    study_date_till = models.DateField(null=True, blank=True)
    study_description = models.CharField(max_length=200, blank=True)
    patient_sex = models.CharField(
        max_length=1,
        blank=True,
        choices=PatientSexFilter.choices,
        default=PatientSexFilter.ALL,
    )
    age_from = models.IntegerField(null=True, blank=True)
    age_till = models.IntegerField(null=True, blank=True)

    output_fields: models.QuerySet["OutputField"]
    tasks: models.QuerySet["ExtractionTask"]

    class Meta:
        ordering = ["-created_at", "title"]

    def __str__(self) -> str:
        return f"ExtractionJob [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("extraction_job_detail", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.extractions.tasks.process_extraction_job",
            allow_unknown=False,
            priority=self.urgent_priority if self.urgent else self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class OutputType(models.TextChoices):
    TEXT = "T", "Text"
    NUMERIC = "N", "Numeric"
    BOOLEAN = "B", "Boolean"
    SELECTION = "S", "Selection"


class OutputField(models.Model):
    name = models.CharField(max_length=30)
    description = models.CharField(max_length=300)
    output_type = models.CharField(
        max_length=1, choices=OutputType.choices, default=OutputType.TEXT
    )
    get_output_type_display: Callable[[], str]
    selection_options = models.JSONField(default=list, blank=True)
    is_array = models.BooleanField(default=False)
    job = models.ForeignKey[ExtractionJob](
        ExtractionJob, null=True, blank=True, on_delete=models.CASCADE, related_name="output_fields"
    )
    subscription = models.ForeignKey(
        "subscriptions.Subscription",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="output_fields",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["name", "job_id"],
                condition=Q(job__isnull=False),
                name="unique_output_field_name_per_job",
            ),
            models.UniqueConstraint(
                fields=["name", "subscription_id"],
                condition=Q(subscription__isnull=False),
                name="unique_output_field_name_per_subscription",
            ),
            models.CheckConstraint(
                condition=(
                    Q(job__isnull=False, subscription__isnull=True)
                    | Q(job__isnull=True, subscription__isnull=False)
                ),
                name="output_field_exactly_one_parent",
            ),
        ]

    def __str__(self) -> str:
        return f'Output Field "{self.name}" [{self.pk}]'

    def clean(self) -> None:
        from django.core.exceptions import ValidationError

        from radis.extractions.utils.validation import validate_selection_options

        super().clean()

        if self.output_type == OutputType.SELECTION:
            if not self.selection_options:
                raise ValidationError({"selection_options": "Add at least one selection option."})
            try:
                self.selection_options = validate_selection_options(self.selection_options)
            except ValidationError as e:
                raise ValidationError({"selection_options": e.message})
        else:
            if self.selection_options:
                raise ValidationError(
                    {"selection_options": "Selections are only allowed for the Selection type."}
                )
            self.selection_options = []


class ExtractionTask(AnalysisTask):
    job = models.ForeignKey[ExtractionJob](
        ExtractionJob, on_delete=models.CASCADE, related_name="tasks"
    )
    instances: models.QuerySet["ExtractionInstance"]

    def get_absolute_url(self) -> str:
        return reverse("extraction_task_detail", args=[self.pk])

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.extractions.tasks.process_extraction_task",
            allow_unknown=False,
            priority=self.job.urgent_priority if self.job.urgent else self.job.default_priority,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class ExtractionInstance(models.Model):
    task = models.ForeignKey[ExtractionTask](
        ExtractionTask, on_delete=models.CASCADE, related_name="instances"
    )
    report = models.ForeignKey[Report](Report, on_delete=models.CASCADE, related_name="+")
    text = models.TextField()
    is_processed = models.BooleanField(default=False)
    output = models.JSONField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Extraction Instance [{self.pk}]"

    def get_absolute_url(self) -> str:
        return reverse("extraction_instance_detail", args=[self.task.pk, self.pk])
