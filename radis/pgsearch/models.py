from __future__ import annotations

from typing import Callable

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField

from radis.reports.models import Report

from .utils.language_utils import code_to_language


class ReportSearchVector(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="search_vector")
    search_vector = SearchVectorField(null=True)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIMENSIONS, null=True)

    class Meta:
        indexes = [
            GinIndex(fields=["search_vector"]),
            HnswIndex(
                fields=["embedding"],
                name="pgsearch_re_embedding_hnsw",
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"Report {self.report.id} search vector"

    def save(self, *args, **kwargs):
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)


class EmbeddingBackfillJob(models.Model):
    id: int

    class Status(models.TextChoices):
        PENDING = "PE", "Pending"
        IN_PROGRESS = "IP", "In Progress"
        CANCELING = "CI", "Canceling"
        CANCELED = "CA", "Canceled"
        SUCCESS = "SU", "Success"
        FAILURE = "FA", "Failure"

    status = models.CharField(max_length=2, choices=Status.choices, default=Status.PENDING)
    get_status_display: Callable[[], str]
    total_reports = models.PositiveIntegerField(default=0)
    processed_reports = models.PositiveIntegerField(default=0)
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EmbeddingBackfillJob [{self.pk}]"

    @property
    def is_cancelable(self) -> bool:
        return self.status in [
            self.Status.PENDING,
            self.Status.IN_PROGRESS,
        ]

    @property
    def is_active(self) -> bool:
        return self.status in [
            self.Status.PENDING,
            self.Status.IN_PROGRESS,
        ]

    @property
    def progress_percent(self) -> int:
        if self.total_reports == 0:
            return 0
        return min(int((self.processed_reports / self.total_reports) * 100), 100)
