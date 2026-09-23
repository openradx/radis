from functools import cached_property
from typing import TYPE_CHECKING

from adit_radis_shared.common.models import AppSettings
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models

if TYPE_CHECKING:
    from radis.labels.models import LabelResult

from radis.core.validators import (
    no_backslash_char_validator,
    no_control_chars_validator,
    no_wildcard_chars_validator,
    validate_patient_sex,
)


class ReportsAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Reports app settings"


class Language(models.Model):
    code = models.CharField(max_length=10, unique=True)

    def __str__(self) -> str:
        return self.code


class Modality(models.Model):
    code = models.CharField(max_length=16, unique=True)
    filterable = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Modalities"

    def __str__(self) -> str:
        return self.code


class ReportQuerySet(models.QuerySet["Report"]):
    def live(self) -> "ReportQuerySet":
        """Reports in circulation. Every user-facing read surface filters on
        this; the admin and ingest paths deliberately use the unfiltered
        default manager (see the withdrawal design spec, decision 4)."""
        return self.filter(withdrawn_at__isnull=True)


class ReportManager(models.Manager["Report"]):
    def get_queryset(self) -> ReportQuerySet:
        return ReportQuerySet(self.model, using=self._db)

    def live(self) -> ReportQuerySet:
        return self.get_queryset().live()


class Report(models.Model):
    document_id = models.CharField(max_length=128, unique=True)
    language = models.ForeignKey(Language, on_delete=models.CASCADE, related_name="reports")
    groups = models.ManyToManyField(
        Group,
        related_name="reports",
    )
    pacs_aet = models.CharField(max_length=16)
    pacs_name = models.CharField(max_length=64)
    pacs_link = models.CharField(max_length=200, blank=True)
    patient_id = models.CharField(
        max_length=64,
        validators=[
            no_backslash_char_validator,
            no_control_chars_validator,
            no_wildcard_chars_validator,
        ],
    )
    patient_birth_date = models.DateField()
    patient_age = models.GeneratedField(
        expression=models.ExpressionWrapper(
            models.Func(
                models.F("study_datetime"), models.F("patient_birth_date"), function="calc_age"
            ),
            output_field=models.IntegerField(),
        ),
        output_field=models.IntegerField(),
        db_persist=True,
    )
    patient_sex = models.CharField(
        max_length=1,
        validators=[validate_patient_sex],
    )
    study_description = models.CharField(blank=True, max_length=64)
    study_datetime = models.DateTimeField()
    study_instance_uid = models.CharField(blank=True, max_length=64)
    accession_number = models.CharField(blank=True, max_length=32)
    modalities = models.ManyToManyField(Modality, related_name="reports")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Withdrawal takes a report out of circulation without deleting it.
    # withdrawn_at IS the state flag: NULL means live. who/why describe only
    # the current withdrawal; restoring blanks all three (the admin log keeps
    # the breadcrumbs).
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    withdrawn_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        # db_index=False: the default FK index would make 0014 a locking
        # full-table index build. ~All rows are NULL here; SET_NULL
        # maintenance on rare user deletion can seq-scan instead.
        db_index=False,
    )
    withdrawal_reason = models.TextField(blank=True, default="")

    objects: ReportManager = ReportManager()

    metadata: models.QuerySet["Metadata"]
    label_results: models.QuerySet["LabelResult"]

    class Meta:
        ordering = ["-created_at", "document_id"]
        indexes = [
            # Partial: only withdrawn rows enter it, so the withdrawn-reports
            # admin listing never scans the archive and live-report writes
            # don't pay for it.
            models.Index(
                fields=["withdrawn_at"],
                condition=models.Q(withdrawn_at__isnull=False),
                name="reports_report_withdrawn_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Report {self.document_id} [{self.pk}]"

    @property
    def modality_codes(self) -> list[str]:
        return [modality.code for modality in self.modalities.all()]

    @property
    def is_withdrawn(self) -> bool:
        return self.withdrawn_at is not None

    @cached_property
    def surfacing_label_results(self):
        """Label results in a surfacing bucket (PRESENT/LIKELY/POSSIBLE), grouped-ready."""
        from radis.labels.models import LabelResult

        return (
            self.label_results.filter(value__in=LabelResult.SURFACING_VALUES)
            .select_related("label", "label__group")
            .order_by("label__group__name", "label__name")
        )


class WithdrawnReport(Report):
    """Withdrawn reports as their own admin entry -- the dedicated listing
    the restore action lives on."""

    class Meta:
        proxy = True
        verbose_name = "Withdrawn report"


class Metadata(models.Model):
    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="metadata")
    key = models.CharField(max_length=64)
    value = models.CharField(max_length=255)

    class Meta:
        verbose_name_plural = "Metadata"
        constraints = [
            models.UniqueConstraint(
                fields=["report", "key"],
                name="unique_key_per_report",
            )
        ]

    def __str__(self) -> str:
        return f"{self.key}: {self.value}"
