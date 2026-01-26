from adit_radis_shared.common.models import AppSettings
from django.contrib.auth.models import Group
from django.db import models

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

    metadata: models.QuerySet["Metadata"]

    class Meta:
        ordering = ["-created_at", "document_id"]

    def __str__(self) -> str:
        return f"Report {self.document_id} [{self.pk}]"

    @property
    def modality_codes(self) -> list[str]:
        return [modality.code for modality in self.modalities.all()]


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


class ReportOverviewTotal(models.Model):
    group = models.OneToOneField(
        Group,
        on_delete=models.CASCADE,
        related_name="report_overview_total",
    )
    total_count = models.PositiveIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.group_id} total={self.total_count}"


class ReportYearStat(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="report_year_stats")
    year = models.PositiveIntegerField()
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "year"], name="unique_report_year_stat")
        ]

    def __str__(self) -> str:
        return f"{self.group_id} {self.year}={self.count}"


class ReportModalityStat(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="report_modality_stats")
    modality_code = models.CharField(max_length=16)
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "modality_code"], name="unique_report_modality_stat"
            )
        ]

    def __str__(self) -> str:
        return f"{self.group_id} {self.modality_code}={self.count}"


class ReportLanguageStat(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="report_language_stats")
    language_code = models.CharField(max_length=10)
    count = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["group", "language_code"], name="unique_report_language_stat"
            )
        ]

    def __str__(self) -> str:
        return f"{self.group_id} {self.language_code}={self.count}"
