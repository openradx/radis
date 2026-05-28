import logging

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue

from .models import EmbeddingJob, EmbeddingTask, ReportSearchVector
from .utils.embedding_client import EmbeddingClient, EmbeddingClientError
from .utils.indexing import bulk_upsert_report_search_vectors

logger = logging.getLogger(__name__)


@app.task
def bulk_index_reports(report_ids: list[int]) -> None:
    if not report_ids:
        return
    logger.info("Indexing %s reports in bulk.", len(report_ids))
    bulk_upsert_report_search_vectors(report_ids)


def enqueue_bulk_index_reports(report_ids: list[int]) -> int | None:
    if not report_ids:
        return None
    try:
        payload: list[JSONValue] = [int(report_id) for report_id in report_ids]
    except (TypeError, ValueError) as exc:
        logger.error("Invalid report_id in bulk index request: %s", exc)
        return None
    return app.configure_task(
        "radis.pgsearch.tasks.bulk_index_reports",
        allow_unknown=False,
    ).defer(report_ids=payload)


@app.task(queue="embeddings")
def process_embedding_task(task_id: int) -> None:
    task = EmbeddingTask.objects.get(id=task_id)
    task.status = EmbeddingTask.Status.IN_PROGRESS
    task.started_at = timezone.now()
    task.attempts = task.attempts + 1
    task.save()

    client = EmbeddingClient()
    try:
        report_ids = list(task.reports.values_list("pk", flat=True))
        rsvs = list(
            ReportSearchVector.objects
            .filter(report_id__in=report_ids)
            .select_related("report")
            .only("id", "report_id", "report__body")
        )
        texts = [rsv.report.body for rsv in rsvs]
        vectors = client.embed_documents(texts)
        for rsv, vec in zip(rsvs, vectors, strict=True):
            rsv.embedding = vec
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

        task.status = EmbeddingTask.Status.SUCCESS
    except EmbeddingClientError as exc:
        logger.exception("Embedding task %s failed: %s", task_id, exc)
        task.status = EmbeddingTask.Status.FAILURE
        task.message = str(exc)
        raise
    finally:
        task.ended_at = timezone.now()
        task.queued_job_id = None
        task.save()
        task.job.update_job_state()
        client.close()


def _create_embedding_task(job: EmbeddingJob, report_ids: list[int]) -> EmbeddingTask:
    from radis.reports.models import Report

    task = EmbeddingTask.objects.create(job=job, status=EmbeddingTask.Status.PENDING)
    task.reports.set(Report.objects.filter(pk__in=report_ids))
    return task


@app.task
def process_embedding_job(job_id: int) -> None:
    job = EmbeddingJob.objects.get(id=job_id)
    assert job.status == EmbeddingJob.Status.PREPARING

    if job.tasks.exists():
        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)
    else:
        pending_ids_iter = (
            ReportSearchVector.objects
            .filter(embedding__isnull=True)
            .values_list("report_id", flat=True)
            .iterator(chunk_size=10_000)
        )
        batch: list[int] = []
        for report_id in pending_ids_iter:
            batch.append(int(report_id))
            if len(batch) >= django_settings.EMBEDDING_BATCH_SIZE:
                _create_embedding_task(job, batch)
                batch = []
        if batch:
            _create_embedding_task(job, batch)

        tasks_to_enqueue = job.tasks.filter(status=EmbeddingTask.Status.PENDING)

    job.status = EmbeddingJob.Status.PENDING
    job.queued_job_id = None
    job.save()

    for task in tasks_to_enqueue:
        if not task.is_queued:
            task.delay()


@app.periodic(cron=django_settings.EMBEDDING_DRAIN_CRON)
@app.task(
    queue="default",
    queueing_lock="embedding_launcher",
    pass_context=True,
)
def embedding_launcher(context, timestamp: int) -> None:
    in_flight = EmbeddingJob.objects.filter(
        status__in=[
            EmbeddingJob.Status.PREPARING,
            EmbeddingJob.Status.PENDING,
            EmbeddingJob.Status.IN_PROGRESS,
        ]
    ).exists()
    if in_flight:
        logger.info("EmbeddingJob already in flight; launcher tick is a no-op.")
        return

    has_pending = ReportSearchVector.objects.filter(embedding__isnull=True).exists()
    if not has_pending:
        logger.debug("No reports pending embedding; launcher tick is a no-op.")
        return

    User = get_user_model()
    system_user = User.objects.get(username=django_settings.EMBEDDING_SYSTEM_USERNAME)
    job = EmbeddingJob.objects.create(
        owner=system_user,
        status=EmbeddingJob.Status.PREPARING,
    )
    transaction.on_commit(job.delay)
