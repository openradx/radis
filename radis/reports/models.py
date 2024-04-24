from typing import TYPE_CHECKING

from adit_radis_shared.common.models import AppSettings
from django.contrib.auth.models import Group
from django.db import models

from radis.core.utils.date_utils import calculate_age
from radis.core.validators import (
    no_backslash_char_validator,
    no_control_chars_validator,
    no_wildcard_chars_validator,
    validate_patient_sex,
)

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


class ReportsAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Reports app settings"


class Language(models.Model):
    id: int
    code = models.CharField(max_length=10, unique=True)

    def __str__(self) -> str:
        return f"Language {self.code}"


class Modality(models.Model):
    id: int
    code = models.CharField(max_length=16, unique=True)
    filterable = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "Modalities"

    def __str__(self) -> str:
        return f"Modality {self.code}"


class Report(models.Model):
    if TYPE_CHECKING:
        metadata = RelatedManager["Metadata"]()

    id: int
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
    patient_sex = models.CharField(
        max_length=1,
        validators=[validate_patient_sex],
    )
    study_description = models.CharField(blank=True, max_length=64)
    study_datetime = models.DateTimeField()
    modalities = models.ManyToManyField(Modality, related_name="reports")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Report {self.id} [{self.document_id}]"

    @property
    def modality_codes(self) -> list[str]:
        return [modality.code for modality in self.modalities.all()]

    @property
    def patient_age(self) -> int:
        """Patient age at study date"""
        return calculate_age(self.patient_birth_date, self.study_datetime)


class Metadata(models.Model):
    id: int
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
        return f"Metadata - {self.key}: {self.value}"
