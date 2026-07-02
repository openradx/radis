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
            # Partial index backing the admin's pending-embedding count. The
            # HNSW index above can't serve an IS NULL check, so without this
            # that count is a full table scan on every changelist request.
            models.Index(
                fields=["id"],
                condition=models.Q(embedding__isnull=True),
                name="pgsearch_pending_embedding_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Report {self.report.id} search index"

    def save(self, *args, **kwargs):
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)


class EmbeddingBackoffState(models.Model):
    """Singleton (always pk=1) shared reactive-backoff state for the
    embedding gateway. When any process receives a 429 it records the
    server-reported wait here; background embedding traffic in every
    container consults this row before sending, so one process's backoff
    gates them all. The counter makes repeat-429 doubling global. See
    docs/superpowers/specs/2026-07-02-shared-429-backoff-design.md."""

    paused_until = models.DateTimeField(null=True, default=None)
    consecutive_429s = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Embedding backoff state"
        verbose_name_plural = "Embedding backoff states"

    def __str__(self) -> str:
        return f"Embedding backoff state (paused_until={self.paused_until})"
