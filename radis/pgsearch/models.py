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


class EmbeddingRateLimitEvent(models.Model):
    """Sliding-window ledger for the embedding gateway's rate limit.

    Confirmed empirically against the production gateway: a genuine sliding
    window of ~60 request-equivalents/minute, where each admitted request
    independently expires exactly 60 seconds after *it* was recorded — not a
    fixed window, not a continuous per-second refill, not tied to request
    completion. See the design doc for how this was confirmed
    (docs/superpowers/specs/2026-07-01-embedding-rate-limit-gate-design.md).

    Rows older than the 60s window are pruned opportunistically by every
    acquisition attempt in `radis.pgsearch.utils.rate_limiter`, so this table
    stays small automatically — no separate cleanup job needed.
    """

    bucket = models.CharField(max_length=32)
    sent_at = models.DateTimeField()
    weight = models.PositiveIntegerField(default=1)

    class Meta:
        indexes = [
            models.Index(
                fields=["bucket", "sent_at"],
                name="pgsearch_ratelimit_bucket_idx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.bucket} @ {self.sent_at} (weight={self.weight})"
