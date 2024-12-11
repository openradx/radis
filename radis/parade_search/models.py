from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models

from radis.reports.models import Report

from .utils.language_utils import code_to_language


class ReportSearchVectorNew(models.Model):
    report = models.OneToOneField(
        Report, on_delete=models.CASCADE, related_name="parade_search_vector"
    )
    search_vector_new = SearchVectorField(null=True)

    class Meta:
        indexes = [GinIndex(fields=["search_vector_new"])]

    def __str__(self) -> str:
        return f"Report {self.report.document_id} search vector"

    def save(self, *args, **kwargs):
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector_new = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)
