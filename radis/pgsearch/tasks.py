import logging

from django.conf import settings as django_settings
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue

from .models import ReportSearchVector
from .utils.embedding_client import EmbeddingClient
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
def embed_reports(report_ids: list[int]) -> None:
    """Compute and write embeddings for the given reports. Overwrites any existing
    embedding. Idempotent across re-runs except for the cost of the API call."""
    if not report_ids:
        return

    rsvs = list(
        ReportSearchVector.objects.filter(report_id__in=report_ids)
        .select_related("report")
        .only("id", "report_id", "report__body")
    )
    if not rsvs:
        return

    client = EmbeddingClient()
    batch_size = django_settings.EMBEDDING_BATCH_SIZE

    try:
        for start in range(0, len(rsvs), batch_size):
            chunk = rsvs[start : start + batch_size]
            texts = [rsv.report.body for rsv in chunk]
            vectors = client.embed_documents(texts)
            for rsv, vec in zip(chunk, vectors, strict=True):
                ReportSearchVector.objects.filter(pk=rsv.pk).update(embedding=vec)
    finally:
        client.close()


def enqueue_embed_reports(
    report_ids: list[int],
    priority: int | None = None,
) -> int | None:
    if not report_ids:
        return None
    if priority is None:
        priority = django_settings.EMBEDDING_INDEX_PRIORITY
    payload: list[JSONValue] = [int(rid) for rid in report_ids]
    return app.configure_task(
        "radis.pgsearch.tasks.embed_reports",
        allow_unknown=False,
        priority=priority,
    ).defer(report_ids=payload)
