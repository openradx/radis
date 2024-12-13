from django.db import models

from radis.reports.models import Report


class ParadeDBReport(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="ParadeDBReport")

    body_en = models.TextField(blank=True)
    body_de = models.TextField(blank=True)

    def __str__(self) -> str:
        return f"Report {self.report.document_id} index model"

    def save(self, *args, **kwargs):
        body_language_field = f"body_{self.report.language.code}"

        if not getattr(self, body_language_field):
            setattr(self, body_language_field, self.report.body)
        super().save(*args, **kwargs)
