from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector, SearchVectorField
from django.db import models
from pgvector.django import HnswIndex, VectorField
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob

from radis.core.models import AnalysisJob, AnalysisTask
from radis.reports.models import Report

from .utils.language_utils import code_to_language


class ReportSearchVector(models.Model):
    report = models.OneToOneField(Report, on_delete=models.CASCADE, related_name="search_vector")
    search_vector = SearchVectorField(null=True)
    embedding = VectorField(dimensions=settings.EMBEDDING_DIM, null=True)

    class Meta:
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
        return f"Report {self.report.id} search vector"

    def save(self, *args, **kwargs):
        body = self.report.body if self.report else ""
        language = code_to_language(self.report.language.code)
        self.search_vector = SearchVector(models.Value(body), config=language)
        super().save(*args, **kwargs)


class EmbeddingJob(AnalysisJob):
    default_priority = settings.EMBEDDING_INDEX_PRIORITY
    urgent_priority = settings.EMBEDDING_INDEX_PRIORITY
    finished_mail_template = None

    queued_job_id: int | None
    queued_job = models.OneToOneField(
        ProcrastinateJob, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    tasks: models.QuerySet["EmbeddingTask"]

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"EmbeddingJob [{self.pk}]"

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_job",
            allow_unknown=False,
            priority=self.default_priority,
        ).defer(job_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()


class EmbeddingTask(AnalysisTask):
    job = models.ForeignKey(
        EmbeddingJob, on_delete=models.CASCADE, related_name="tasks"
    )
    reports = models.ManyToManyField(Report, related_name="embedding_tasks")

    def delay(self) -> None:
        queued_job_id = app.configure_task(
            "radis.pgsearch.tasks.process_embedding_task",
            allow_unknown=False,
            priority=settings.EMBEDDING_INDEX_PRIORITY,
        ).defer(task_id=self.pk)
        self.queued_job_id = queued_job_id
        self.save()
