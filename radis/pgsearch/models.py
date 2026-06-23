from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField

from radis.reports.models import Report

from .utils.language_utils import code_to_language


class ReportSearchIndex(models.Model):
    """Per-report row that backs every search modality. Holds the FTS
    `search_vector` (tsvector) and the dense `embedding` vector for
    hybrid search; a future trigram column would also live here. Named
    after its role, not after any single field — adding another search
    representation shouldn't force another rename."""

    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="search_index")
    search_vector = SearchVectorField(null=True)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM, null=True)

    class Meta:
        verbose_name = "Report search index"
        verbose_name_plural = "Report search indexes"
        indexes = [
            GinIndex(fields=["search_vector"]),
            HnswIndex(
                name="pgsearch_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"Report {self.report.id} search index"

    def save(self, *args, **kwargs):
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)
