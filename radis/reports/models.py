from django.contrib.auth.models import Group
from django.contrib.postgres.fields import ArrayField
from django.db import models

from radis.core.models import AppSettings
from radis.core.validators import (
    no_backslash_char_validator,
    no_control_chars_validator,
    no_wildcard_chars_validator,
    validate_metadata,
    validate_patient_sex,
)


class ReportsAppSettings(AppSettings):
    class Meta:
        verbose_name_plural = "Reports app settings"


class Report(models.Model):
    id: int
    document_id = models.CharField(max_length=128, unique=True)
    language = models.CharField(max_length=10)
    groups = models.ManyToManyField(
        Group,
        related_name="reports",
    )
    pacs_aet = models.CharField(max_length=16)
    pacs_name = models.CharField(max_length=64)
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
    modalities_in_study = ArrayField(models.CharField(max_length=16))
    links = ArrayField(models.CharField(max_length=200))
    body = models.TextField()
    metadata = models.JSONField(
        default=dict,
        validators=[validate_metadata],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Report {self.id} [{self.document_id}]"
