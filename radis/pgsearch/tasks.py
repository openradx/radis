import logging

from channels.db import database_sync_to_async
from django.conf import settings
from procrastinate.contrib.django import app
from procrastinate.types import JSONValue

from .models import ReportSearchVector
from .utils.embedding_client import AsyncEmbeddingClient
from .utils.indexing import bulk_upsert_report_search_vectors

logger = logging.getLogger(__name__)


@app.task
def bulk_index_reports(report_ids: list[int]) -> None:
    """Deferred FTS bulk-indexing for the bulk-upsert path
    (when `PGSEARCH_SYNC_INDEXING=False`).

    Chains into `embed_reports_task` so the embedding step is enqueued
    immediately after FTS rows exist, regardless of whether FTS ran sync inline
    or via this deferred task.
    """
    if not report_ids:
        return
    logger.info("Indexing %s reports in bulk.", len(report_ids))
    bulk_upsert_report_search_vectors(report_ids)
    embed_reports_task.defer(report_ids=list(report_ids))


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
async def embed_reports_task(report_ids: list[int]) -> None:
    """Embed the named reports.

    Raises on `EmbeddingClientError` so Procrastinate's retry policy applies.
    Reports are sent to the embedding service in batches of
    `EMBEDDING_BATCH_SIZE` to bound per-call payload size regardless of how
    many `report_ids` the caller passed.
    """
    if not report_ids:
        return

    @database_sync_to_async
    def _load_rsvs() -> list[ReportSearchVector]:
        return list(
            ReportSearchVector.objects.filter(report_id__in=report_ids)
            .select_related("report")
            .only("id", "report_id", "report__body")
        )

    rsvs = await _load_rsvs()
    if not rsvs:
        logger.warning(
            "embed_reports_task: no ReportSearchVector rows for report ids %s",
            report_ids,
        )
        return

    batch_size = settings.EMBEDDING_BATCH_SIZE
    async with AsyncEmbeddingClient() as client:
        for start in range(0, len(rsvs), batch_size):
            chunk = rsvs[start : start + batch_size]
            vectors = await client.embed_documents([rsv.report.body for rsv in chunk])
            for rsv, vec in zip(chunk, vectors, strict=True):
                rsv.embedding = vec

    @database_sync_to_async
    def _save() -> None:
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

    await _save()
