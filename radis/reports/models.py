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

    # body_en = models.TextField(blank=True)
    # body_de = models.TextField(blank=True)

    metadata: models.QuerySet["Metadata"]

    # def __str__(self) -> str:
    #    return f"Report {self.document_id} [{self.pk}]"

    # def save(self, *args, **kwargs):
    #     body_language_field = f"body_{self.language.code}"
    #     if not getattr(self, body_language_field):
    #         setattr(self, body_language_field, self.body)
    #     super().save(*args, **kwargs)

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
