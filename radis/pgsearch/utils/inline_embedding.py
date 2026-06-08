"""Inline async embedding for ADRF report views.

Called right after a report write (single-create / update / bulk-upsert). The
FTS path has already created the `ReportSearchVector` row with `embedding=NULL`
(via the `Report` post_save signal for single rows, or via
`bulk_upsert_report_search_vectors` for the bulk path), so this function just
needs to fill in the `embedding` column.

Failure policy: never raises. If the embedding service is unreachable or
returns malformed data, log a WARNING and return — the RSV row keeps its
NULL embedding and the report remains searchable via the FTS half of hybrid
search.
"""
from __future__ import annotations

import logging

from channels.db import database_sync_to_async

from ..models import ReportSearchVector
from .embedding_client import AsyncEmbeddingClient, EmbeddingClientError

logger = logging.getLogger(__name__)


async def embed_reports_inline(report_ids: list[int]) -> None:
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
            "Inline embedding: no ReportSearchVector rows found for report ids %s",
            report_ids,
        )
        return

    try:
        async with AsyncEmbeddingClient() as client:
            vectors = await client.embed_documents([rsv.report.body for rsv in rsvs])
    except EmbeddingClientError as exc:
        logger.warning(
            "Inline embedding failed for %d report(s); leaving embedding=NULL: %s",
            len(rsvs),
            exc,
        )
        return

    for rsv, vec in zip(rsvs, vectors, strict=True):
        rsv.embedding = vec

    @database_sync_to_async
    def _save_embeddings() -> None:
        ReportSearchVector.objects.bulk_update(rsvs, fields=["embedding"])

    await _save_embeddings()
