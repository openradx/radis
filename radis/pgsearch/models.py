from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField
from procrastinate.contrib.django.models import ProcrastinateJob

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
    embedding = VectorField(dimensions=settings.EMBEDDINGS_DIM, null=True)

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
        # Only the single-row path recomputes the tsvector here. The bulk
        # paths (utils.indexing.bulk_upsert_report_search_indexes) bypass
        # save() and duplicate this logic in SQL — keep both in sync.
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)


class LexemeRank(models.Model):
    """One row per (report, lexeme) with the exact ``ts_rank`` value the FTS
    arm computes for a single-term query of that lexeme -- an impact-ordered
    posting list. Serves the single-word fast path behind
    ``HYBRID_FTS_LEXEME_RANK_INDEX``; multi-term queries cannot use it (their
    combined score exists only per query) and keep the ``ts_rank`` path.
    Maintained by a database trigger installed by ``sync_lexeme_ranks``, not
    by the ORM -- rows follow ``ReportSearchIndex.search_vector`` writes."""

    report = models.ForeignKey(Report, on_delete=models.CASCADE, related_name="lexeme_ranks")
    lexeme = models.TextField()
    rank = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "lexeme"], name="pgsearch_lexemerank_uniq"),
        ]
        indexes = [
            # The impact order: (lexeme, rank DESC, report) serves "top N
            # reports for one lexeme" as a bounded index-range walk that
            # matches the fast path's ORDER BY exactly.
            models.Index(fields=["lexeme", "-rank", "report"], name="pgsearch_lexeme_rank_idx"),
        ]

    def __str__(self) -> str:
        return f"Lexeme rank of report {self.report_id}"


class EmbeddingBackfillRun(models.Model):
    """One operator-triggered embedding backfill (`embed_pending` or the
    admin enqueue action). At most one run is active at a time, enforced by
    `tasks.create_backfill_run` rather than a DB constraint ("active"
    involves queue state). Write-path (live-priority) embedding work
    carries no run.

    Progress is counter-based: `embed_reports_task` increments
    `processed_reports` after each successful subjob bulk-write (immune to
    the worker's --delete-jobs policy) and stamps `finished_at` when the
    counter reaches `total_reports`. Failed subjobs never increment."""

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    total_reports = models.PositiveIntegerField()
    processed_reports = models.PositiveIntegerField(default=0)
    triggered_by = models.CharField(max_length=150)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"Embedding backfill run {self.pk} ({self.processed_reports}/{self.total_reports})"

    @property
    def is_active(self) -> bool:
        return self.finished_at is None and self.cancelled_at is None

    @classmethod
    def get_active(cls) -> "EmbeddingBackfillRun | None":
        return (
            cls.objects.filter(finished_at__isnull=True, cancelled_at__isnull=True)
            .order_by("-started_at")
            .first()
        )

    def live_subjob_count(self) -> int:
        """Queued+running subjobs carrying this run's id. Zero while
        `processed < total` means the run is abandoned (dead worker or
        retry exhaustion) — see `tasks.create_backfill_run` and the
        badge's stall marker."""
        return ProcrastinateJob.objects.filter(
            task_name="radis.pgsearch.tasks.embed_reports_task",
            queue_name="embeddings",
            status__in=("todo", "doing"),
            args__run_id=self.pk,
        ).count()
